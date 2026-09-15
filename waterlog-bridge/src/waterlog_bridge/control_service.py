"""Durable local tank-mode executor and independent restoration scheduler."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable

from .control_client import ControlAuthenticationError, ControlClient, ControlProtocolError
from .control_models import ControlCommand, ControlOutlet, RegistryOutlet
from .control_store import ControlStore
from .home_assistant_control import HomeAssistantControl, HomeAssistantControlError
from .http import TransportError

LOGGER = logging.getLogger(__name__)
_RETRY = (1, 2, 5, 10, 30)


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ControlService:
    def __init__(
        self,
        store: ControlStore,
        client: ControlClient,
        ha: HomeAssistantControl,
        installation_id: str,
        inventory: tuple[RegistryOutlet, ...],
        *,
        inventory_provider: Callable[[], tuple[RegistryOutlet, ...]] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
        scheduler_interval: float = 0.2,
    ) -> None:
        self.store = store
        self.client = client
        self.ha = ha
        self.installation_id = installation_id
        self.inventory = inventory
        self.inventory_provider = inventory_provider
        self.clock = clock
        self.monotonic = monotonic
        self.stop = stop_event or threading.Event()
        self.scheduler_interval = scheduler_interval
        self._state_lock = threading.RLock()
        self._last_utc = self.clock()
        self._last_mono = self.monotonic()
        persisted = [float(row["last_utc"]) for row in self.store.sessions()]
        self._persisted_utc = max(persisted) if persisted else None
        self._startup_clock_checked = False

    def _fresh_inventory(self) -> dict[str, RegistryOutlet]:
        if self.inventory_provider is not None:
            try:
                self.inventory = self.inventory_provider()
            except Exception as error:
                raise HomeAssistantControlError(
                    "Home Assistant registry is unavailable"
                ) from error
        return {item.outlet_id: item for item in self.inventory}

    @staticmethod
    def _obligation_identity(item) -> tuple[str, str, str, str, str]:
        return (
            str(item["registry_entry_id"]),
            str(item["platform"]),
            str(item["config_entry_id"]),
            str(item["device_id"]),
            str(item["unique_id"]),
        )

    def _resolve_plan(self, outlets: Iterable[ControlOutlet]) -> dict[str, tuple[ControlOutlet, RegistryOutlet]]:
        inventory = self._fresh_inventory()
        resolved: dict[str, tuple[ControlOutlet, RegistryOutlet]] = {}
        for planned in outlets:
            local = inventory.get(planned.outlet_id)
            if local is None or local.pinned_identity() != planned.pinned_identity():
                raise HomeAssistantControlError("outlet pinned identity changed")
            resolved[planned.outlet_id] = (planned, local)
        return resolved

    def _resolve_obligations(self, tank_id: str):
        inventory = self._fresh_inventory()
        for item in self.store.obligations(tank_id):
            local = inventory.get(str(item["outlet_id"]))
            if local is None or local.pinned_identity() != self._obligation_identity(item):
                raise HomeAssistantControlError("outlet pinned identity changed")
            if local.entity_id != item["entity_id"]:
                self.store.update_obligation_route(tank_id, str(item["outlet_id"]), local.entity_id)
        return list(self.store.obligations(tank_id)), inventory

    @staticmethod
    def _dependency_depths(items: Iterable[object]) -> dict[str, int]:
        parents = {
            str(item.outlet_id if isinstance(item, ControlOutlet) else item["outlet_id"]): (
                item.requires_pump_outlet_id
                if isinstance(item, ControlOutlet)
                else item["requires_pump_outlet_id"]
            )
            for item in items
        }
        depths: dict[str, int] = {}

        def depth(outlet_id: str, path: frozenset[str]) -> int:
            if outlet_id in depths:
                return depths[outlet_id]
            if outlet_id in path:
                raise HomeAssistantControlError("outlet dependency cycle")
            parent = parents.get(outlet_id)
            value = 0 if not parent or str(parent) not in parents else 1 + depth(str(parent), path | {outlet_id})
            depths[outlet_id] = value
            return value

        for outlet_id in parents:
            depth(outlet_id, frozenset())
        return depths

    def _clock_safe(self, now: float) -> bool:
        mono = self.monotonic()
        expected = self._last_utc + (mono - self._last_mono)
        startup_rollback = (
            not self._startup_clock_checked
            and self._persisted_utc is not None
            and now < self._persisted_utc - 5
        )
        safe = not startup_rollback and now >= self._last_utc - 5 and abs(now - expected) <= 5
        self._startup_clock_checked = True
        self._last_utc = now
        self._last_mono = mono
        if not safe:
            for session in self.store.sessions():
                self.store.set_session(session["tank_id"], "restoring", mode="normal", error="clock_uncertain", now=now)
                self._restore(session["tank_id"], now=now, immediate=True)
        else:
            self.store.touch_sessions(now)
        return safe

    def _outlet_reports(self, tank_id: str) -> list[dict[str, object]]:
        try:
            obligations, inventory = self._resolve_obligations(tank_id)
        except HomeAssistantControlError:
            obligations, inventory = list(self.store.obligations(tank_id)), {}
        reports: list[dict[str, object]] = []
        for item in obligations:
            local = inventory.get(str(item["outlet_id"]))
            identity_valid = local is not None and local.pinned_identity() == self._obligation_identity(item)
            try:
                state = self.ha.state(local.entity_id) if identity_valid and local else "unavailable"
            except HomeAssistantControlError:
                state = "unavailable"
            reports.append({
                "outletId": item["outlet_id"],
                "state": state,
                "baselineState": item["baseline"],
                "deadlineAt": _timestamp(float(item["deadline"])),
                "errorCode": item["error_code"] if identity_valid else "outlet_identity_changed",
                "generation": item["generation"],
            })
        return reports

    def _queue_report(
        self,
        *,
        command_id: str,
        tank_id: str,
        session_start_command_id: str,
        command_revision: int,
        config_revision: int,
        mode: str,
        phase: str,
        request_status: str | None,
        error: str | None,
        outlets: list[dict[str, object]],
    ) -> None:
        self.store.queue_report({
            "commandId": command_id,
            "tankId": tank_id,
            "sessionStartCommandId": session_start_command_id,
            "commandRevision": command_revision,
            "configRevision": config_revision,
            "mode": mode,
            "phase": phase,
            "requestStatus": request_status,
            "observedAt": _timestamp(self.clock()),
            "errorCode": error,
            "outlets": outlets,
        }, self.clock())

    def _report(self, tank_id: str, *, request_status: str | None, error: str | None = None) -> None:
        session = next((row for row in self.store.sessions() if row["tank_id"] == tank_id), None)
        if session is None:
            return
        self._queue_report(
            command_id=session["command_id"],
            tank_id=tank_id,
            session_start_command_id=session["session_command_id"],
            command_revision=session["revision"],
            config_revision=session["config_revision"],
            mode=session["mode"],
            phase=session["phase"],
            request_status=request_status,
            error=error or session["error_code"],
            outlets=self._outlet_reports(tank_id),
        )

    def _record_command(self, command: ControlCommand, status: str, now: float) -> None:
        self.store.record_command(
            command_id=command.command_id,
            tank_id=command.tank_id,
            revision=command.command_revision,
            mode=command.mode,
            status=status,
            now=now,
            payload=[],
        )

    def _report_rejected_without_session(self, command: ControlCommand, error: str, now: float) -> None:
        self._record_command(command, "failed", now)
        self._queue_report(
            command_id=command.command_id,
            tank_id=command.tank_id,
            session_start_command_id=command.command_id,
            command_revision=command.command_revision,
            config_revision=command.config_revision,
            mode="normal",
            phase="normal",
            request_status="failed",
            error=error,
            outlets=[],
        )

    def _reject_and_unwind(self, command: ControlCommand, error: str, now: float) -> None:
        session = next((row for row in self.store.sessions() if row["tank_id"] == command.tank_id), None)
        if session is None:
            self._report_rejected_without_session(command, error, now)
            return
        self._record_command(command, "failed", now)
        if session["phase"] == "recovery_required":
            self._queue_report(
                command_id=command.command_id,
                tank_id=command.tank_id,
                session_start_command_id=session["session_command_id"],
                command_revision=command.command_revision,
                config_revision=command.config_revision,
                mode=session["mode"],
                phase="recovery_required",
                request_status="failed",
                error=error,
                outlets=self._outlet_reports(command.tank_id),
            )
            return
        self.store.transition_session_command(
            command.tank_id,
            command_id=command.command_id,
            revision=command.command_revision,
            config_revision=command.config_revision,
            mode="normal",
            phase="restoring",
            error=error,
            now=now,
        )
        self._report(command.tank_id, request_status="failed", error=error)
        self._restore(command.tank_id, now=now, immediate=True)

    def accept(self, command: ControlCommand, server_now: float) -> None:
        with self._state_lock:
            if self.store.accepted(command.command_id):
                return
            now = self.clock()
            if self.store.recovery_required() and command.kind != "finish_manual_recovery":
                self._reject_and_unwind(command, "recovery_required", now)
                return
            if command.config_revision > self.store.config_revision():
                self._reject_and_unwind(command, "config_not_installed", now)
                return
            if command.mode != "normal" and (
                not self._clock_safe(now)
                or command.latest_start_at.timestamp() < server_now
                or command.latest_start_at.timestamp() < now - 1
            ):
                self._reject_and_unwind(command, "start_expired", now)
                return
            if command.kind == "finish_manual_recovery":
                self._finish_manual(command, now)
            elif command.mode == "normal":
                self._normal(command, now)
            else:
                self._temporary(command, now)

    def _check_entry_deadlines(self, tank_id: str) -> None:
        # Entry owns the state lock while waiting for HA. Give other tanks'
        # due restorations a turn between writes instead of blocking for the
        # entire batch of confirmations.
        for session in self.store.sessions():
            other_tank = str(session["tank_id"])
            if other_tank != tank_id and any(
                self.clock() >= float(item["deadline"])
                for item in self.store.obligations(other_tank)
            ):
                self._restore(other_tank, now=self.clock())
        if any(
            self.clock() >= float(item["deadline"])
            for item in self.store.obligations(tank_id)
            if bool(item["active_in_next"])
        ):
            raise HomeAssistantControlError("tank mode deadline expired during entry")

    def _temporary(self, command: ControlCommand, now: float) -> None:
        try:
            resolved = self._resolve_plan(command.outlets)
            states = {outlet_id: self.ha.state(local.entity_id) for outlet_id, (_, local) in resolved.items()}
        except HomeAssistantControlError as error:
            self._reject_and_unwind(command, "outlet_not_allowlisted" if "identity" in str(error) else "home_assistant_unavailable", now)
            return
        if any(value not in {"on", "off"} for value in states.values()):
            self._reject_and_unwind(command, "preflight_unknown", now)
            return
        selected = [outlet for outlet in command.outlets if outlet.off_seconds is not None]
        for outlet in selected:
            if outlet.requires_pump_outlet_id and states[outlet.outlet_id] == "on" and states.get(outlet.requires_pump_outlet_id) != "on":
                self._reject_and_unwind(command, "dependency_baseline_invalid", now)
                return
        ready_at = self.clock()
        if (
            not self._clock_safe(ready_at)
            or ready_at > command.latest_start_at.timestamp()
        ):
            self._reject_and_unwind(command, "start_expired", ready_at)
            return
        now = ready_at
        old = next((row for row in self.store.sessions() if row["tank_id"] == command.tank_id), None)
        old_obligations = {row["outlet_id"]: row for row in self.store.obligations(command.tank_id)}
        if old and old["mode"] == command.mode and old["phase"] == "active":
            self._record_command(command, "completed", now)
            self.store.transition_session_command(
                command.tank_id,
                command_id=command.command_id,
                revision=command.command_revision,
                config_revision=command.config_revision,
                mode=old["mode"],
                phase=old["phase"],
                error=old["error_code"],
                now=now,
            )
            self._report(command.tank_id, request_status="completed")
            return
        selected_ids = {outlet.outlet_id for outlet in selected}
        for outlet in selected:
            prior = old_obligations.get(outlet.outlet_id)
            if prior is not None and self._obligation_identity(prior) != outlet.pinned_identity():
                self._reject_and_unwind(command, "outlet_identity_changed", now)
                return
        obligations: list[dict[str, object]] = []
        for outlet_id, prior in old_obligations.items():
            if outlet_id not in selected_ids:
                obligations.append({
                    "outlet_id": outlet_id,
                    "entity_id": prior["entity_id"],
                    "registry_entry_id": prior["registry_entry_id"],
                    "platform": prior["platform"],
                    "config_entry_id": prior["config_entry_id"],
                    "device_id": prior["device_id"],
                    "unique_id": prior["unique_id"],
                    "baseline": prior["baseline"],
                    "deadline": now,
                    "requires_pump_outlet_id": prior["requires_pump_outlet_id"],
                    "active_in_next": False,
                })
        for outlet in selected:
            prior = old_obligations.get(outlet.outlet_id)
            local = resolved[outlet.outlet_id][1]
            obligations.append({
                "outlet_id": outlet.outlet_id,
                "entity_id": local.entity_id,
                "registry_entry_id": outlet.registry_entry_id,
                "platform": outlet.platform,
                "config_entry_id": outlet.config_entry_id,
                "device_id": outlet.device_id,
                "unique_id": outlet.unique_id,
                "baseline": prior["baseline"] if prior else states[outlet.outlet_id],
                "deadline": now + int(outlet.off_seconds or 0),
                "requires_pump_outlet_id": outlet.requires_pump_outlet_id,
                "active_in_next": True,
            })
        self.store.begin_session(
            tank_id=command.tank_id,
            session_command_id=old["session_command_id"] if old else command.command_id,
            command_id=command.command_id,
            revision=command.command_revision,
            config_revision=command.config_revision,
            mode=command.mode,
            now=now,
            obligations=obligations,
            phase="switching" if old else "starting",
        )
        if old:
            removed = {str(item["outlet_id"]) for item in self.store.obligations(command.tank_id) if not bool(item["active_in_next"])}
            removed_complete, _ = self._restore_items(
                command.tank_id,
                now=now,
                immediate=True,
                only_outlet_ids=removed,
            )
            if removed and not removed_complete:
                self.store.set_command_status(command.command_id, "failed")
                self.store.set_session(command.tank_id, "restoring", mode="normal", error="switch_restore_failed", now=now)
                self._report(command.tank_id, request_status="failed", error="switch_restore_failed")
                self._restore(command.tank_id, now=now, immediate=True)
                return
            self.store.remove_inactive_obligations(command.tank_id)
        try:
            depths = self._dependency_depths(command.outlets)
            for outlet in sorted(
                selected,
                key=lambda item: (-depths[item.outlet_id], item.outlet_id),
            ):
                local = resolved[outlet.outlet_id][1]
                # Confirmation can take long enough for a short mode window to
                # expire. Restore before issuing another entry write.
                self._check_entry_deadlines(command.tank_id)
                if states[outlet.outlet_id] == "off":
                    self.store.mark_result(command.tank_id, outlet.outlet_id, "preserved")
                    continue
                self.store.mark_intent(command.tank_id, outlet.outlet_id, "off")
                if self.ha.set_state(local.entity_id, "off") != "off":
                    raise HomeAssistantControlError("off readback failed")
                self.store.mark_result(command.tank_id, outlet.outlet_id, "off")
            # A final confirmation may consume the entire remaining window;
            # unwind instead of publishing an already-expired active session.
            self._check_entry_deadlines(command.tank_id)
            self.store.set_command_status(command.command_id, "completed")
            self.store.set_session(command.tank_id, "active", mode=command.mode, now=now)
            self._report(command.tank_id, request_status="completed")
        except HomeAssistantControlError as error:
            LOGGER.error(
                "Tank mode entry failed; restoring durable baselines: %s", error
            )
            self.store.set_command_status(command.command_id, "failed")
            self.store.set_session(command.tank_id, "restoring", mode="normal", error="entry_failed", now=now)
            self._report(command.tank_id, request_status="failed", error="entry_failed")
            self._restore(command.tank_id, now=now, immediate=True)

    def _normal(self, command: ControlCommand, now: float) -> None:
        session = next((row for row in self.store.sessions() if row["tank_id"] == command.tank_id), None)
        if not session:
            self._record_command(command, "completed", now)
            self._queue_report(
                command_id=command.command_id,
                tank_id=command.tank_id,
                session_start_command_id=command.command_id,
                command_revision=command.command_revision,
                config_revision=command.config_revision,
                mode="normal",
                phase="normal",
                request_status="completed",
                error=None,
                outlets=[],
            )
            return
        self._record_command(command, "accepted", now)
        self.store.transition_session_command(
            command.tank_id,
            command_id=command.command_id,
            revision=command.command_revision,
            config_revision=command.config_revision,
            mode="normal",
            phase="restoring",
            error=None,
            now=now,
        )
        self._restore(command.tank_id, now=now, immediate=True)

    def _retry(self, item, now: float, error: str) -> None:
        count = min(int(item["retry_count"]) + 1, len(_RETRY))
        self.store.retry_obligation(str(item["tank_id"]), str(item["outlet_id"]), error=error, retry_count=count, retry_at=now + _RETRY[count - 1])

    def _restore_items(self, tank_id: str, *, now: float, immediate: bool, only_outlet_ids: set[str] | None = None) -> tuple[bool, bool]:
        obligations = list(self.store.obligations(tank_id))
        target = [item for item in obligations if only_outlet_ids is None or item["outlet_id"] in only_outlet_ids]
        if not target:
            return True, False
        changed = False
        try:
            obligations, inventory = self._resolve_obligations(tank_id)
            target = [item for item in obligations if only_outlet_ids is None or item["outlet_id"] in only_outlet_ids]
        except HomeAssistantControlError:
            for item in target:
                if item["retry_at"] is None or now >= float(item["retry_at"]):
                    self._retry(item, now, "outlet_identity_changed")
                    changed = True
            return False, changed
        try:
            depths = self._dependency_depths(obligations)
        except HomeAssistantControlError:
            for item in target:
                self._retry(item, now, "dependency_cycle")
            return False, True
        complete = True
        for item in sorted(
            target,
            key=lambda row: (
                depths[str(row["outlet_id"])],
                row["deadline"],
                row["outlet_id"],
            ),
        ):
            if item["result"] == "restored":
                continue
            if not immediate and now < item["deadline"]:
                complete = False
                continue
            if item["retry_at"] is not None and now < float(item["retry_at"]):
                complete = False
                continue
            local = inventory[str(item["outlet_id"])]
            try:
                state = self.ha.state(local.entity_id)
            except HomeAssistantControlError:
                state = "unavailable"
            unresolved_write = (
                (
                    item["result"] is None
                    and item["intent"] is not None
                    and item["intent"] != item["baseline"]
                )
                or item["error_code"] == "restore_failed"
            )
            if state == item["baseline"] and not unresolved_write:
                self.store.mark_result(tank_id, item["outlet_id"], "restored")
                changed = True
                continue
            if item["error_code"] == "external_change":
                complete = False
                continue
            if item["baseline"] == "off":
                self.store.mark_result(
                    tank_id,
                    item["outlet_id"],
                    "external_change",
                    "external_change",
                )
                complete = False
                changed = True
                continue
            if state not in {"on", "off"}:
                complete = False
                self._retry(item, now, "restore_state_unknown")
                changed = True
                continue
            pump_id = item["requires_pump_outlet_id"]
            if pump_id:
                pump_local = inventory.get(str(pump_id))
                try:
                    pump_state = "unavailable" if pump_local is None else self.ha.state(pump_local.entity_id)
                except HomeAssistantControlError:
                    pump_state = "unavailable"
                if pump_state != "on":
                    complete = False
                    self._retry(item, now, "pump_not_restored")
                    changed = True
                    continue
            try:
                self.store.mark_restore_pending(tank_id, item["outlet_id"])
                if self.ha.set_state(local.entity_id, item["baseline"]) != item["baseline"]:
                    raise HomeAssistantControlError("restore readback failed")
                self.store.mark_result(tank_id, item["outlet_id"], "restored")
                changed = True
            except HomeAssistantControlError:
                complete = False
                self._retry(item, now, "restore_failed")
                changed = True
        return complete, changed

    def _restore(self, tank_id: str, *, now: float, immediate: bool = False) -> None:
        before = list(self.store.obligations(tank_id))
        complete, changed = self._restore_items(tank_id, now=now, immediate=immediate)
        if complete:
            session = next((row for row in self.store.sessions() if row["tank_id"] == tank_id), None)
            if session is None:
                return
            status = self.store.command_status(session["command_id"])
            request_status = "failed" if status == "failed" else "completed"
            final_error = session["error_code"] if status == "failed" else None
            if status == "accepted":
                self.store.set_command_status(session["command_id"], "completed")
            self.store.set_session(tank_id, "normal", mode="normal", error=None, now=now)
            self._report(tank_id, request_status=request_status, error=final_error)
            self.store.remove_session(tank_id)
            return
        refreshed = list(self.store.obligations(tank_id))
        recovery = any(item["error_code"] in {"external_change", "outlet_identity_changed"} for item in refreshed)
        error = next((str(item["error_code"]) for item in refreshed if item["error_code"]), None)
        due = immediate or any(now >= item["deadline"] for item in before)
        previous_phase = next(
            (row["phase"] for row in self.store.sessions() if row["tank_id"] == tank_id),
            None,
        )
        if recovery:
            self.store.set_session(tank_id, "recovery_required", error=error, now=now)
        elif due:
            self.store.set_session(tank_id, "restoring", error=error, now=now)
        if recovery or due:
            session = next(row for row in self.store.sessions() if row["tank_id"] == tank_id)
            if changed or session["phase"] != previous_phase:
                self._report(tank_id, request_status="failed" if self.store.command_status(session["command_id"]) == "failed" else None, error=error)

    def _finish_manual(self, command: ControlCommand, now: float) -> None:
        if command.session_start_command_id is None or command.expected_report_sequence is None:
            return
        session = next((row for row in self.store.sessions() if row["tank_id"] == command.tank_id), None)
        if session is None:
            recovery_tanks = self.store.recovery_tanks()
            if command.tank_id not in recovery_tanks and not (
                not recovery_tanks
                and self.store.get_meta("recovery_required") == "1"
            ):
                return
        elif command.session_start_command_id != session["session_command_id"] or command.expected_report_sequence != session["last_report_sequence"]:
            return
        try:
            resolved = self._resolve_plan(command.outlets)
            if session is not None:
                obligations = {
                    str(item["outlet_id"]): item
                    for item in self.store.obligations(command.tank_id)
                }
                plan = {item.outlet_id: item for item in command.outlets}
                if set(obligations) != set(plan) or any(
                    self._obligation_identity(obligations[outlet_id])
                    != item.pinned_identity()
                    for outlet_id, item in plan.items()
                ):
                    raise HomeAssistantControlError("recovery plan changed")
        except HomeAssistantControlError:
            resolved, failure = {}, "outlet_identity_changed"
        else:
            failure = "recovery_state_changed"
        states: dict[str, str] = {}
        for outlet_id, (_, local) in resolved.items():
            try:
                states[outlet_id] = self.ha.state(local.entity_id)
            except HomeAssistantControlError:
                states[outlet_id] = "unavailable"
        expected = {item.outlet_id: item.expected_state for item in command.outlets}
        valid = (
            set(states) == set(expected)
            and all(state in {"on", "off"} for state in states.values())
            and all(
                states[outlet_id] == expected[outlet_id]
                for outlet_id in states
            )
        )
        if session is None:
            status = "completed" if valid else "failed"
            phase = "normal" if valid else "recovery_required"
            error = None if valid else failure
            self._record_command(command, status, now)
            self._queue_report(
                command_id=command.command_id,
                tank_id=command.tank_id,
                session_start_command_id=command.session_start_command_id,
                command_revision=command.command_revision,
                config_revision=command.config_revision,
                mode="normal",
                phase=phase,
                request_status=status,
                error=error,
                outlets=[
                    {
                        "outletId": item.outlet_id,
                        "state": states.get(item.outlet_id, "unavailable"),
                        "baselineState": item.expected_state,
                        "deadlineAt": _timestamp(now),
                        "errorCode": error,
                        "generation": 0,
                    }
                    for item in command.outlets
                ],
            )
            if valid:
                self.store.resolve_recovery_tank(command.tank_id)
            return
        if not valid:
            self._record_command(command, "failed", now)
            self.store.transition_session_command(
                command.tank_id,
                command_id=command.command_id,
                revision=command.command_revision,
                config_revision=command.config_revision,
                mode="normal",
                phase="recovery_required",
                error=failure,
                now=now,
            )
            self._report(command.tank_id, request_status="failed", error=failure)
            return
        self._record_command(command, "completed", now)
        self.store.transition_session_command(
            command.tank_id,
            command_id=command.command_id,
            revision=command.command_revision,
            config_revision=command.config_revision,
            mode="normal",
            phase="normal",
            error=None,
            now=now,
        )
        self._report(command.tank_id, request_status="completed")
        self.store.resolve_recovery_tank(command.tank_id)
        self.store.remove_session(command.tank_id)

    def tick(self) -> None:
        with self._state_lock:
            now = self.clock()
            self._clock_safe(now)
            for original in list(self.store.sessions()):
                session = next((row for row in self.store.sessions() if row["tank_id"] == original["tank_id"]), None)
                if session is None:
                    continue
                if session["phase"] in {"starting", "switching"}:
                    self.store.set_session(
                        session["tank_id"],
                        "restoring",
                        mode="normal",
                        error="restart_during_switch" if session["phase"] == "switching" else "restart_during_entry",
                        now=now,
                    )
                    session = next(row for row in self.store.sessions() if row["tank_id"] == original["tank_id"])
                if session["phase"] == "active":
                    try:
                        obligations, inventory = self._resolve_obligations(session["tank_id"])
                    except HomeAssistantControlError:
                        for item in self.store.obligations(session["tank_id"]):
                            self._retry(item, now, "outlet_identity_changed")
                        self.store.set_session(session["tank_id"], "recovery_required", error="outlet_identity_changed", now=now)
                        self._report(session["tank_id"], request_status="failed", error="outlet_identity_changed")
                        continue
                    for item in obligations:
                        if now < item["deadline"] and item["result"] in {"off", "preserved"}:
                            try:
                                state = self.ha.state(inventory[item["outlet_id"]].entity_id)
                            except HomeAssistantControlError:
                                state = "unavailable"
                            if state == "on":
                                self.store.mark_result(session["tank_id"], item["outlet_id"], "external_change", "external_change")
                                self.store.set_session(session["tank_id"], "recovery_required", error="external_change", now=now)
                                self._report(session["tank_id"], request_status="failed", error="external_change")
                self._restore(
                    session["tank_id"],
                    now=now,
                    immediate=session["phase"] in {"starting", "switching", "recovery_required"} or session["mode"] == "normal",
                )

    def _exchange_payload(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "protocolVersion": 1,
                "installationId": self.installation_id,
                "reportSequence": self.store.report_sequence(),
                "knownConfigRevision": self.store.config_revision(),
                "acknowledgedCommandIds": [],
                "inventory": [outlet.wire() for outlet in self.inventory],
                "results": self.store.reports(),
            }

    def exchange_once(self) -> float:
        response = self.client.exchange(self._exchange_payload())
        with self._state_lock:
            self.store.advance_report_sequence(response["reportSequenceFloor"])
            self.store.acknowledge_reports(response["acknowledgedReportIds"])
            self.store.replace_recovery_tanks(
                response["recoveryTankIds"], self.clock()
            )
            if response["configuration"] is not None:
                self.store.install_config(response["configRevision"], response["configuration"], self.clock())
            server_now = datetime.fromisoformat(response["serverTime"].replace("Z", "+00:00")).timestamp()
            for raw in response["commands"]:
                self.accept(ControlCommand.from_wire(raw), server_now)
        return float(response["retryAfterSeconds"])

    def _scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Local control reconciliation failed; retrying")
            self.stop.wait(self.scheduler_interval)

    def run(self) -> None:
        self.store.backup_health_check()
        scheduler = threading.Thread(target=self._scheduler_loop, name="waterlog-control-scheduler", daemon=True)
        scheduler.start()
        backoff = 2.0
        try:
            while not self.stop.is_set():
                try:
                    delay = self.exchange_once()
                    backoff = 2.0
                except ControlAuthenticationError:
                    LOGGER.error("Control credential rejected; local restoration remains active")
                    delay = 30.0
                except (TransportError, ControlProtocolError, ValueError):
                    delay = backoff
                    backoff = min(30.0, backoff * 2)
                self.stop.wait(max(0.2, min(30.0, delay)))
        finally:
            self.stop.set()
            scheduler.join(timeout=5)
