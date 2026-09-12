from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from waterlog_bridge.control_models import ControlCommand, ControlOutlet, RegistryOutlet
from waterlog_bridge.control_client import ControlAuthenticationError
from waterlog_bridge.control_service import ControlService
from waterlog_bridge.control_store import ControlStore, ControlStoreError
from waterlog_bridge.home_assistant_control import HomeAssistantControlError
from waterlog_bridge.http import TransportError

INSTALLATION = "10000000-0000-4000-8000-000000000001"
TANK = "50000000-0000-4000-8000-000000000001"
SECOND_TANK = "50000000-0000-4000-8000-000000000002"
PUMP = "20000000-0000-4000-8000-000000000001"
HEATER = "20000000-0000-4000-8000-000000000002"


class FakeHA:
    def __init__(self, states: dict[str, str], before_write=None):
        self.states = states
        self.writes = []
        self.before_write = before_write

    def state(self, entity_id: str) -> str:
        return self.states.get(entity_id, "unavailable")

    def set_state(self, entity_id: str, state: str) -> str:
        if self.before_write:
            self.before_write(entity_id, state)
        self.writes.append((entity_id, state))
        self.states[entity_id] = state
        return state


class NoCloud:
    def exchange(self, payload):
        raise ConnectionError("offline")


def inventory():
    return (
        RegistryOutlet(
            PUMP,
            "registry-pump",
            "tplink",
            "config",
            "device-pump",
            "unique-pump",
            "switch.pump",
            "Pump",
        ),
        RegistryOutlet(
            HEATER,
            "registry-heater",
            "tplink",
            "config",
            "device-heater",
            "unique-heater",
            "switch.heater",
            "Heater",
        ),
    )


def planned_outlet(
    outlet_id=PUMP,
    *,
    seconds=60,
    pump_id=None,
    expected_state=None,
):
    source = {item.outlet_id: item for item in inventory()}[outlet_id]
    return ControlOutlet(
        source.outlet_id,
        source.entity_id,
        source.registry_entry_id,
        source.platform,
        source.config_entry_id,
        source.device_id,
        source.unique_id,
        seconds,
        pump_id,
        expected_state,
    )


def command(mode="feed", revision=1, outlets=None, now=1_800_000_000.0):
    return ControlCommand(
        f"40000000-0000-4000-8000-{revision:012d}",
        TANK,
        revision,
        1,
        "set_mode",
        mode,
        datetime.fromtimestamp(now + 15, tz=timezone.utc),
        None,
        None,
        tuple(
            outlets
            if outlets is not None
            else [
                planned_outlet(PUMP, seconds=60),
                planned_outlet(HEATER, seconds=120, pump_id=PUMP),
            ]
        ),
    )


def recovery_command_wire(
    tank_id,
    outlet_id,
    revision,
    expected_report_sequence,
    *,
    expected_state="on",
    now=1_800_000_000.0,
):
    outlet = planned_outlet(
        outlet_id,
        seconds=None,
        expected_state=expected_state,
    )
    return {
        "commandId": f"40000000-0000-4000-8000-{revision:012d}",
        "tankId": tank_id,
        "commandRevision": revision,
        "configRevision": 1,
        "kind": "finish_manual_recovery",
        "mode": "normal",
        "latestStartAt": datetime.fromtimestamp(
            now + 15, tz=timezone.utc
        ).isoformat(),
        "sessionStartCommandId": f"41000000-0000-4000-8000-{revision:012d}",
        "expectedReportSequence": expected_report_sequence,
        "outlets": [
            {
                "outletId": outlet.outlet_id,
                "entityId": outlet.entity_id,
                "registryEntryId": outlet.registry_entry_id,
                "platform": outlet.platform,
                "configEntryId": outlet.config_entry_id,
                "deviceId": outlet.device_id,
                "uniqueId": outlet.unique_id,
                "offSeconds": None,
                "requiresPumpOutletId": None,
                "expectedState": outlet.expected_state,
            }
        ],
    }


class ControlExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, "control.sqlite3")
        self.now = 1_800_000_000.0
        self.store = ControlStore(self.path, INSTALLATION)
        self.store.install_config(1, {"outlets": []}, self.now)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def service(self, ha):
        return ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )

    def test_shared_fixture_parses_active_feed(self):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "tank-mode-protocol-v1.json"
        )
        value = json.loads(fixture.read_text())
        self.assertEqual(value["protocolVersion"], 1)
        self.assertEqual(value["results"][0]["phase"], "active")
        states = json.loads(
            (fixture.parent / "tank-mode-ui-states-v1.json").read_text()
        )
        self.assertIsNone(states["normalUnconfirmed"]["confirmedMode"])
        self.assertEqual(states["partialFailure"]["phase"], "recovery_required")

    def test_plan_is_durable_before_first_ha_write_and_heater_stops_first(self):
        observed = []

        def before(entity, state):
            observed.append((entity, len(self.store.obligations(TANK))))

        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"}, before)
        self.service(ha).accept(command(now=self.now), self.now)
        self.assertEqual(observed[0], ("switch.heater", 2))
        self.assertEqual(ha.writes[1], ("switch.pump", "off"))
        self.assertEqual(self.store.sessions()[0]["phase"], "active")

    def test_already_off_outlet_is_never_turned_on(self):
        ha = FakeHA({"switch.pump": "off"})
        self.service(ha).accept(
            command(
                outlets=[planned_outlet()],
                now=self.now,
            ),
            self.now,
        )
        self.now += 61
        self.service(ha).tick()
        self.assertEqual(ha.writes, [])

    def test_local_deadline_restores_while_cloud_is_unavailable(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                outlets=[planned_outlet()],
                now=self.now,
            ),
            self.now,
        )
        self.assertEqual(ha.states["switch.pump"], "off")
        self.now += 61
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.store.sessions(), [])

    def test_independent_deadlines_restore_pump_before_dependent_heater(self):
        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"})
        service = self.service(ha)
        service.accept(command(now=self.now), self.now)
        self.now += 61
        service.tick()
        self.assertEqual(
            (ha.states["switch.pump"], ha.states["switch.heater"]), ("on", "off")
        )
        self.assertEqual(len(self.store.sessions()), 1)
        self.now += 60
        service.tick()
        self.assertEqual(
            (ha.states["switch.pump"], ha.states["switch.heater"]), ("on", "on")
        )

    def test_pump_restore_failure_blocks_heater_with_reverse_sorting_ids(self):
        reverse_inventory = (
            replace(inventory()[0], outlet_id=HEATER),
            replace(inventory()[1], outlet_id=PUMP),
        )
        pump = ControlOutlet(
            HEATER,
            "switch.pump",
            "registry-pump",
            "tplink",
            "config",
            "device-pump",
            "unique-pump",
            60,
            None,
            None,
        )
        heater = ControlOutlet(
            PUMP,
            "switch.heater",
            "registry-heater",
            "tplink",
            "config",
            "device-heater",
            "unique-heater",
            60,
            HEATER,
            None,
        )

        class PumpRestoreFails(FakeHA):
            def set_state(self, entity_id, state):
                self.writes.append((entity_id, state))
                if entity_id == "switch.pump" and state == "on":
                    raise HomeAssistantControlError("pump restore failed")
                self.states[entity_id] = state
                return state

        ha = PumpRestoreFails({"switch.pump": "on", "switch.heater": "on"})
        service = ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            reverse_inventory,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.accept(command(outlets=[pump, heater], now=self.now), self.now)
        self.now += 61
        service.tick()

        self.assertIn(("switch.pump", "on"), ha.writes)
        self.assertNotIn(("switch.heater", "on"), ha.writes)
        errors = {row["outlet_id"]: row["error_code"] for row in self.store.obligations(TANK)}
        self.assertEqual(errors[HEATER], "restore_failed")
        self.assertEqual(errors[PUMP], "pump_not_restored")

    def test_restart_reconciles_expired_obligation_without_replaying_off(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                outlets=[planned_outlet()],
                now=self.now,
            ),
            self.now,
        )
        self.assertEqual(ha.states["switch.pump"], "off")
        self.store.close()
        self.now += 61
        self.store = ControlStore(self.path, INSTALLATION)
        self.service(ha).tick()
        self.assertEqual(ha.writes, [("switch.pump", "off"), ("switch.pump", "on")])

    def test_switching_modes_preserves_original_baseline(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        outlet = [planned_outlet()]
        service.accept(
            command(mode="feed", revision=1, outlets=outlet, now=self.now), self.now
        )
        service.accept(
            command(mode="water_change", revision=2, outlets=outlet, now=self.now),
            self.now,
        )
        self.assertEqual(self.store.obligations(TANK)[0]["baseline"], "on")
        self.now += 61
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")

    def test_expired_command_never_switches_off(self):
        ha = FakeHA({"switch.pump": "on"})
        expired = command(
            outlets=[planned_outlet()],
            now=self.now - 30,
        )
        self.service(ha).accept(expired, self.now)
        self.assertEqual(ha.writes, [])

    def test_expired_switch_unwinds_existing_override_to_normal(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        service.accept(
            command(
                mode="water_change",
                revision=2,
                outlets=[planned_outlet(PUMP)],
                now=self.now - 30,
            ),
            self.now,
        )

        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "failed")

    def test_unknown_switch_preflight_unwinds_existing_override(self):
        class OneUnknownRead(FakeHA):
            fail_next_read = False

            def state(self, entity_id):
                if self.fail_next_read:
                    self.fail_next_read = False
                    return "unknown"
                return super().state(entity_id)

        ha = OneUnknownRead({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        ha.fail_next_read = True
        service.accept(
            command(
                mode="water_change",
                revision=2,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )

        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.store.sessions(), [])

    def test_unavailable_switch_preflight_unwinds_existing_override(self):
        class OneUnavailableRead(FakeHA):
            fail_next_read = False

            def state(self, entity_id):
                if self.fail_next_read:
                    self.fail_next_read = False
                    return "unavailable"
                return super().state(entity_id)

        ha = OneUnavailableRead({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        ha.fail_next_read = True
        service.accept(
            command(
                mode="water_change",
                revision=2,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )

        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.store.sessions(), [])

    def test_allowlist_failure_during_switch_preserves_recovery_obligation(self):
        ha = FakeHA({"switch.pump": "on"})
        current_inventory = list(inventory())
        service = ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            tuple(current_inventory),
            inventory_provider=lambda: tuple(current_inventory),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        current_inventory.clear()
        service.accept(
            command(
                mode="water_change",
                revision=2,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )

        self.assertEqual(self.store.obligations(TANK)[0]["baseline"], "on")
        self.assertEqual(self.store.sessions()[0]["phase"], "recovery_required")
        self.assertNotIn(("switch.pump", "on"), ha.writes)

    def test_entry_failure_immediately_restores_prior_outlets(self):
        class FailingHA(FakeHA):
            def set_state(self, entity_id, state):
                if self.before_write:
                    self.before_write(entity_id, state)
                self.writes.append((entity_id, state))
                if len(self.writes) == 2:
                    raise HomeAssistantControlError("synthetic")
                self.states[entity_id] = state
                return state

        ha = FailingHA({"switch.pump": "on", "switch.heater": "on"})
        self.service(ha).accept(command(now=self.now), self.now)
        self.assertEqual(ha.states["switch.heater"], "on")

    def test_second_process_cannot_share_store(self):
        with self.assertRaisesRegex(ControlStoreError, "another control executor"):
            ControlStore(self.path, INSTALLATION)

    def test_manual_recovery_is_bound_to_exact_session_and_report(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        entered = command(
            outlets=[planned_outlet()],
            now=self.now,
        )
        service.accept(entered, self.now)
        ha.states["switch.pump"] = "on"
        service.tick()
        session = self.store.sessions()[0]
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[
                    planned_outlet(
                        seconds=None,
                        expected_state="on",
                    )
                ],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id=session["session_command_id"],
            expected_report_sequence=session["last_report_sequence"],
        )
        service.accept(recovery, self.now)
        self.assertEqual(self.store.sessions(), [])

    def test_attended_recovery_can_clear_a_replaced_local_store(self):
        self.store.set_meta("recovery_required", "1")
        self.store.db.commit()
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[
                    planned_outlet(
                        seconds=None,
                        expected_state="on",
                    )
                ],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id="40000000-0000-4000-8000-000000000001",
            expected_report_sequence=7,
        )

        self.service(FakeHA({"switch.pump": "on"})).accept(recovery, self.now)

        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.get_meta("recovery_required"), "0")
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "completed")

    def test_attended_recovery_after_lost_store_rejects_unavailable_state(self):
        self.store.set_meta("recovery_required", "1")
        self.store.db.commit()
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[planned_outlet(seconds=None, expected_state="on")],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id="40000000-0000-4000-8000-000000000001",
            expected_report_sequence=7,
        )

        self.service(FakeHA({"switch.pump": "unavailable"})).accept(
            recovery, self.now
        )

        self.assertEqual(self.store.get_meta("recovery_required"), "1")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "failed")
        self.assertEqual(
            self.store.reports()[-1]["errorCode"], "recovery_state_changed"
        )

    def test_attended_recovery_after_lost_store_rejects_changed_state(self):
        self.store.set_meta("recovery_required", "1")
        self.store.db.commit()
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[planned_outlet(seconds=None, expected_state="on")],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id="40000000-0000-4000-8000-000000000001",
            expected_report_sequence=7,
        )

        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        service.accept(recovery, self.now)

        self.assertEqual(self.store.get_meta("recovery_required"), "1")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "failed")
        self.assertEqual(
            self.store.reports()[-1]["errorCode"], "recovery_state_changed"
        )
        for _ in range(3):
            self.now += 30
            service.tick()
        service.accept(recovery, self.now)
        self.assertEqual(ha.writes, [])
        self.store.close()
        self.store = ControlStore(self.path, INSTALLATION)
        restarted = self.service(ha)
        restarted.tick()
        restarted.accept(recovery, self.now)
        self.assertEqual(ha.writes, [])
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.get_meta("recovery_required"), "1")

    def test_attended_recovery_after_lost_store_rejects_replacement(self):
        self.store.set_meta("recovery_required", "1")
        self.store.db.commit()
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[planned_outlet(seconds=None, expected_state="on")],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id="40000000-0000-4000-8000-000000000001",
            expected_report_sequence=7,
        )
        replaced_inventory = (replace(inventory()[0], unique_id="replacement"),)
        service = ControlService(
            self.store,
            NoCloud(),
            FakeHA({"switch.pump": "on"}),
            INSTALLATION,
            replaced_inventory,
            inventory_provider=lambda: replaced_inventory,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )

        service.accept(recovery, self.now)

        self.assertEqual(self.store.get_meta("recovery_required"), "1")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "failed")
        self.assertEqual(
            self.store.reports()[-1]["errorCode"], "outlet_identity_changed"
        )

    def test_attended_recovery_accepts_current_state_different_from_baseline(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        obligation = self.store.obligations(TANK)[0]
        self.store.mark_result(TANK, PUMP, "external_change", "external_change")
        self.store.set_session(
            TANK,
            "recovery_required",
            error="external_change",
            now=self.now,
        )
        service._report(TANK, request_status=None, error="external_change")
        session = self.store.sessions()[0]
        recovery = replace(
            command(
                mode="normal",
                revision=2,
                outlets=[planned_outlet(seconds=None, expected_state="off")],
                now=self.now,
            ),
            kind="finish_manual_recovery",
            session_start_command_id=session["session_command_id"],
            expected_report_sequence=session["last_report_sequence"],
        )

        service.accept(recovery, self.now)

        self.assertEqual(obligation["baseline"], "on")
        self.assertEqual(ha.writes, [("switch.pump", "off")])
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "completed")

    def test_originally_off_external_change_survives_ticks_and_restart(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        self.assertEqual(ha.writes, [])
        ha.states["switch.pump"] = "on"

        service.tick()
        service.tick()

        self.assertEqual(self.store.sessions()[0]["phase"], "recovery_required")
        self.assertEqual(
            self.store.obligations(TANK)[0]["error_code"], "external_change"
        )
        self.assertEqual(ha.writes, [])
        self.store.close()
        self.store = ControlStore(self.path, INSTALLATION)
        self.service(ha).tick()
        self.assertEqual(self.store.sessions()[0]["phase"], "recovery_required")
        self.assertEqual(ha.writes, [])
        ha.states["switch.pump"] = "off"
        self.service(ha).tick()
        self.assertEqual(self.store.sessions(), [])

    def test_preflight_rechecks_start_window_before_first_write(self):
        extra_ids = [
            f"20000000-0000-4000-8000-{index:012d}" for index in range(10, 14)
        ]
        local_inventory = tuple(
            RegistryOutlet(
                outlet_id,
                f"registry-{index}",
                "tplink",
                "config",
                f"device-{index}",
                f"unique-{index}",
                f"switch.outlet_{index}",
                f"Outlet {index}",
            )
            for index, outlet_id in enumerate(extra_ids)
        )
        outlets = [
            ControlOutlet(
                item.outlet_id,
                item.entity_id,
                item.registry_entry_id,
                item.platform,
                item.config_entry_id,
                item.device_id,
                item.unique_id,
                60,
                None,
                None,
            )
            for item in local_inventory
        ]

        class SlowReads(FakeHA):
            def state(inner_self, entity_id):
                self.now += 4
                return super().state(entity_id)

        ha = SlowReads({item.entity_id: "on" for item in local_inventory})
        service = ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            local_inventory,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )

        service.accept(command(outlets=outlets, now=self.now), self.now)

        self.assertEqual(ha.writes, [])
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["errorCode"], "start_expired")

    def test_same_mode_request_advances_session_and_final_report_revision(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        original = self.store.obligations(TANK)[0]
        service.accept(
            command(
                mode="feed",
                revision=2,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )

        session = self.store.sessions()[0]
        current = self.store.obligations(TANK)[0]
        self.assertEqual(session["revision"], 2)
        self.assertEqual(session["command_id"], command(revision=2).command_id)
        self.assertEqual(current["generation"], original["generation"])
        self.assertEqual(current["deadline"], original["deadline"])
        self.assertEqual(current["baseline"], original["baseline"])
        self.now += 61
        service.tick()
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["phase"], "normal")
        self.assertEqual(self.store.reports()[-1]["commandRevision"], 2)

    def test_failed_switch_restores_removed_outlets_and_new_entry(self):
        class FailHeaterOff(FakeHA):
            def set_state(self, entity_id, state):
                self.writes.append((entity_id, state))
                if entity_id == "switch.heater" and state == "off":
                    raise HomeAssistantControlError("synthetic entry failure")
                self.states[entity_id] = state
                return state

        ha = FailHeaterOff({"switch.pump": "on", "switch.heater": "on"})
        service = self.service(ha)
        service.accept(
            command(
                mode="feed",
                revision=1,
                outlets=[planned_outlet(PUMP)],
                now=self.now,
            ),
            self.now,
        )
        service.accept(
            command(
                mode="water_change",
                revision=2,
                outlets=[
                    planned_outlet(PUMP, seconds=None),
                    planned_outlet(HEATER, pump_id=PUMP),
                ],
                now=self.now,
            ),
            self.now,
        )

        self.assertEqual(ha.states, {"switch.pump": "on", "switch.heater": "on"})
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["requestStatus"], "failed")

    def test_heater_only_mode_observes_dependency_without_switching_pump(self):
        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"})
        service = self.service(ha)
        service.accept(
            command(
                outlets=[
                    planned_outlet(PUMP, seconds=None),
                    planned_outlet(HEATER, seconds=60, pump_id=PUMP),
                ],
                now=self.now,
            ),
            self.now,
        )
        self.assertEqual(ha.writes, [("switch.heater", "off")])
        self.now += 61
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.heater", "on"))
        self.assertEqual(ha.states["switch.pump"], "on")

    def test_replacement_blocks_restore_and_requires_attended_recovery(self):
        ha = FakeHA({"switch.pump": "on"})
        current_inventory = list(inventory())
        service = ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            tuple(current_inventory),
            inventory_provider=lambda: tuple(current_inventory),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        current_inventory[0] = replace(current_inventory[0], unique_id="replacement")
        self.now += 61
        service.tick()

        self.assertNotIn(("switch.pump", "on"), ha.writes)
        self.assertEqual(self.store.sessions()[0]["phase"], "recovery_required")
        self.assertEqual(
            self.store.obligations(TANK)[0]["error_code"],
            "outlet_identity_changed",
        )

    def test_verified_registry_rename_updates_route_before_restore(self):
        ha = FakeHA({"switch.pump": "on", "switch.renamed_pump": "off"})
        current_inventory = list(inventory())
        service = ControlService(
            self.store,
            NoCloud(),
            ha,
            INSTALLATION,
            tuple(current_inventory),
            inventory_provider=lambda: tuple(current_inventory),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        current_inventory[0] = replace(
            current_inventory[0], entity_id="switch.renamed_pump"
        )
        self.now += 61
        service.tick()

        self.assertEqual(ha.states["switch.renamed_pump"], "on")
        self.assertEqual(self.store.sessions(), [])

    def test_startup_clock_rollback_restores_instead_of_extending_deadline(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        self.store.close()
        self.now -= 120
        self.store = ControlStore(self.path, INSTALLATION)
        self.service(ha).tick()

        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.store.sessions(), [])

    def test_restore_failure_queues_report_and_retries_locally(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        initial_reports = len(self.store.reports())
        ha.states["switch.pump"] = "unavailable"
        self.now += 61
        service.tick()

        self.assertGreater(len(self.store.reports()), initial_reports)
        self.assertEqual(
            self.store.reports()[-1]["outlets"][0]["errorCode"],
            "restore_state_unknown",
        )
        self.assertEqual(self.store.sessions()[0]["phase"], "restoring")

    def test_network_exchange_cannot_block_local_deadline_scheduler(self):
        entered = threading.Event()
        released = threading.Event()
        restored = threading.Event()

        class BlockingCloud:
            def exchange(self, payload):
                entered.set()
                released.wait(2)
                raise TransportError("offline")

        def before_write(entity_id, state):
            if entity_id == "switch.pump" and state == "on":
                restored.set()

        ha = FakeHA({"switch.pump": "on"}, before_write)
        service = ControlService(
            self.store,
            BlockingCloud(),
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            scheduler_interval=0.01,
        )
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        worker = threading.Thread(target=service.run)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.now += 61
        self.assertTrue(restored.wait(1))
        service.stop.set()
        released.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_revoked_control_credential_cannot_block_local_deadline_scheduler(self):
        restored = threading.Event()

        class RevokedCloud:
            def exchange(self, payload):
                raise ControlAuthenticationError("revoked")

        def before_write(entity_id, state):
            if entity_id == "switch.pump" and state == "on":
                restored.set()

        ha = FakeHA({"switch.pump": "on"}, before_write)
        service = ControlService(
            self.store,
            RevokedCloud(),
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            scheduler_interval=0.01,
        )
        service.accept(
            command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
        )
        worker = threading.Thread(target=service.run)
        worker.start()
        self.now += 61
        self.assertTrue(restored.wait(1))
        service.stop.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_report_outbox_uses_durable_sequence_for_tied_timestamps(self):
        for command_revision in (2, 1):
            self.store.queue_report(
                {
                    "commandRevision": command_revision,
                    "tankId": TANK,
                },
                self.now,
            )
        reports = self.store.reports()
        self.assertEqual([item["reportSequence"] for item in reports], [1, 2])
        self.assertEqual([item["commandRevision"] for item in reports], [2, 1])

    def test_lost_store_resumes_cloud_sequence_and_recovers_two_tanks_in_one_exchange(self):
        cloud_states = {TANK: "recovery_required", SECOND_TANK: "recovery_required"}

        class RecoveryCloud:
            def __init__(self):
                self.calls = 0

            def exchange(inner_self, payload):
                inner_self.calls += 1
                if inner_self.calls == 1:
                    self.assertEqual(payload["reportSequence"], 0)
                    self.assertEqual(payload["results"], [])
                    commands = [
                        recovery_command_wire(TANK, PUMP, 2, 7, now=self.now),
                        recovery_command_wire(
                            SECOND_TANK, HEATER, 3, 20, now=self.now
                        ),
                    ]
                    recovery_tanks = [TANK, SECOND_TANK]
                    floor = 20
                    acknowledged = []
                elif inner_self.calls == 2:
                    self.assertEqual(payload["reportSequence"], 22)
                    self.assertEqual(
                        [report["reportSequence"] for report in payload["results"]],
                        [21, 22],
                    )
                    for report in payload["results"]:
                        self.assertEqual(report["phase"], "normal")
                        self.assertEqual(report["requestStatus"], "completed")
                        cloud_states[report["tankId"]] = report["phase"]
                    commands = []
                    recovery_tanks = []
                    floor = 22
                    acknowledged = [
                        report["reportId"] for report in payload["results"]
                    ]
                else:
                    self.assertEqual(payload["reportSequence"], 22)
                    self.assertEqual(payload["results"], [])
                    commands = []
                    recovery_tanks = []
                    floor = 22
                    acknowledged = []
                return {
                    "protocolVersion": 1,
                    "serverTime": datetime.fromtimestamp(
                        self.now, tz=timezone.utc
                    ).isoformat(),
                    "acknowledgedReportIds": acknowledged,
                    "retryAfterSeconds": 2,
                    "configRevision": 1,
                    "configuration": None,
                    "commands": commands,
                    "recoveryRequired": bool(recovery_tanks),
                    "recoveryTankIds": recovery_tanks,
                    "reportSequenceFloor": floor,
                }

        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"})
        service = ControlService(
            self.store,
            RecoveryCloud(),
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )

        service.exchange_once()
        self.assertEqual(self.store.report_sequence(), 22)
        self.assertEqual(self.store.recovery_tanks(), set())
        service.exchange_once()
        self.assertEqual(cloud_states, {TANK: "normal", SECOND_TANK: "normal"})
        self.assertEqual(self.store.reports(), [])
        service.exchange_once()
        self.assertEqual(ha.writes, [])

    def test_lost_store_recovery_survives_restart_and_failed_ack_per_tank(self):
        class SequentialRecoveryCloud:
            def __init__(inner_self):
                inner_self.calls = 0

            def exchange(inner_self, payload):
                inner_self.calls += 1
                results = payload["results"]
                if inner_self.calls == 1:
                    floor = 20
                    tanks = [TANK, SECOND_TANK]
                    commands = [
                        recovery_command_wire(TANK, PUMP, 2, 7, now=self.now)
                    ]
                elif inner_self.calls == 2:
                    self.assertEqual(results[0]["reportSequence"], 21)
                    self.assertEqual(results[0]["requestStatus"], "completed")
                    floor = 21
                    tanks = [SECOND_TANK]
                    commands = [
                        recovery_command_wire(
                            SECOND_TANK,
                            HEATER,
                            3,
                            20,
                            expected_state="off",
                            now=self.now,
                        )
                    ]
                elif inner_self.calls == 3:
                    self.assertEqual(results[0]["reportSequence"], 22)
                    self.assertEqual(results[0]["requestStatus"], "failed")
                    floor = 22
                    tanks = [SECOND_TANK]
                    commands = [
                        recovery_command_wire(
                            SECOND_TANK, HEATER, 4, 22, now=self.now
                        )
                    ]
                else:
                    self.assertEqual(results[0]["reportSequence"], 23)
                    self.assertEqual(results[0]["requestStatus"], "completed")
                    floor = 23
                    tanks = []
                    commands = []
                return {
                    "protocolVersion": 1,
                    "serverTime": datetime.fromtimestamp(
                        self.now, tz=timezone.utc
                    ).isoformat(),
                    "acknowledgedReportIds": [
                        report["reportId"] for report in results
                    ],
                    "retryAfterSeconds": 2,
                    "configRevision": 1,
                    "configuration": None,
                    "commands": commands,
                    "recoveryRequired": bool(tanks),
                    "recoveryTankIds": tanks,
                    "reportSequenceFloor": floor,
                }

        cloud = SequentialRecoveryCloud()
        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"})
        service = ControlService(
            self.store,
            cloud,
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.exchange_once()
        self.assertEqual(self.store.recovery_tanks(), {SECOND_TANK})

        self.store.close()
        self.store = ControlStore(self.path, INSTALLATION)
        service = ControlService(
            self.store,
            cloud,
            ha,
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        self.assertEqual(self.store.recovery_tanks(), {SECOND_TANK})
        service.exchange_once()
        self.assertEqual(self.store.recovery_tanks(), {SECOND_TANK})
        service.exchange_once()
        self.assertEqual(self.store.recovery_tanks(), set())
        service.exchange_once()
        self.assertEqual(self.store.reports(), [])
        self.assertEqual(self.store.report_sequence(), 23)
        self.assertEqual(ha.writes, [])

    def test_report_remains_durable_until_cloud_acknowledges_it(self):
        report_id = self.store.queue_report(
            {"commandRevision": 1, "tankId": TANK}, self.now
        )

        class AcknowledgingCloud:
            def __init__(self):
                self.calls = 0

            def exchange(self, payload):
                self.calls += 1
                return {
                    "protocolVersion": 1,
                    "serverTime": datetime.fromtimestamp(
                        self_now, tz=timezone.utc
                    ).isoformat(),
                    "acknowledgedReportIds": [] if self.calls == 1 else [report_id],
                    "retryAfterSeconds": 2,
                    "configRevision": 1,
                    "configuration": None,
                    "commands": [],
                    "recoveryRequired": False,
                    "recoveryTankIds": [],
                    "reportSequenceFloor": 1,
                }

        self_now = self.now
        service = ControlService(
            self.store,
            AcknowledgingCloud(),
            FakeHA({}),
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        service.exchange_once()
        self.assertEqual(self.store.reports()[0]["reportId"], report_id)
        service.exchange_once()
        self.assertEqual(self.store.reports(), [])

    def test_existing_outbox_rows_backfill_sequence_from_durable_payload(self):
        self.store.queue_report(
            {"commandRevision": 1, "tankId": TANK}, self.now
        )
        self.store.close()
        database = sqlite3.connect(self.path)
        database.execute("update report_outbox set sequence=0")
        database.commit()
        database.close()

        self.store = ControlStore(self.path, INSTALLATION)
        self.assertEqual(self.store.reports()[0]["reportSequence"], 1)
        self.assertEqual(
            self.store.db.execute(
                "select sequence from report_outbox"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
