"""Strict v1 tank-mode control wire models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Mode = Literal["normal", "feed", "water_change"]
OutletState = Literal["on", "off", "unknown", "unavailable"]


def utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed


@dataclass(frozen=True, slots=True)
class ControlOutlet:
    outlet_id: str
    entity_id: str
    registry_entry_id: str
    platform: str
    config_entry_id: str
    device_id: str
    unique_id: str
    off_seconds: int | None
    requires_pump_outlet_id: str | None
    expected_state: OutletState | None

    def pinned_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.registry_entry_id,
            self.platform,
            self.config_entry_id,
            self.device_id,
            self.unique_id,
        )

    @classmethod
    def from_wire(cls, value: object) -> "ControlOutlet":
        if not isinstance(value, dict) or set(value) != {
            "outletId",
            "entityId",
            "registryEntryId",
            "platform",
            "configEntryId",
            "deviceId",
            "uniqueId",
            "offSeconds",
            "requiresPumpOutletId",
            "expectedState",
        }:
            raise ValueError("invalid outlet plan")
        seconds = value["offSeconds"]
        if (
            seconds is not None
            and (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or not 60 <= seconds <= 7200
            )
        ):
            raise ValueError("invalid outlet duration")
        expected_state = value["expectedState"]
        if expected_state is not None and expected_state not in {
            "on",
            "off",
            "unknown",
            "unavailable",
        }:
            raise ValueError("invalid expected outlet state")
        return cls(
            str(value["outletId"]),
            str(value["entityId"]),
            str(value["registryEntryId"]),
            str(value["platform"]),
            str(value["configEntryId"]),
            str(value["deviceId"]),
            str(value["uniqueId"]),
            seconds,
            (
                None
                if value["requiresPumpOutletId"] is None
                else str(value["requiresPumpOutletId"])
            ),
            expected_state,
        )


@dataclass(frozen=True, slots=True)
class ControlCommand:
    command_id: str
    tank_id: str
    command_revision: int
    config_revision: int
    kind: Literal["set_mode", "finish_manual_recovery"]
    mode: Mode
    latest_start_at: datetime
    session_start_command_id: str | None
    expected_report_sequence: int | None
    outlets: tuple[ControlOutlet, ...]

    @classmethod
    def from_wire(cls, value: object) -> "ControlCommand":
        required = {
            "commandId",
            "tankId",
            "commandRevision",
            "configRevision",
            "kind",
            "mode",
            "latestStartAt",
            "sessionStartCommandId",
            "expectedReportSequence",
            "outlets",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("invalid command")
        if value["kind"] not in {"set_mode", "finish_manual_recovery"} or value[
            "mode"
        ] not in {"normal", "feed", "water_change"}:
            raise ValueError("invalid command kind or mode")
        outlets = tuple(ControlOutlet.from_wire(item) for item in value["outlets"])
        if len(outlets) > 64 or len({item.outlet_id for item in outlets}) != len(
            outlets
        ):
            raise ValueError("invalid command outlet count")
        if value["kind"] == "set_mode" and value["mode"] != "normal":
            if not outlets or not any(item.off_seconds is not None for item in outlets):
                raise ValueError("temporary command has no selected outlet")
            if any(item.expected_state is not None for item in outlets):
                raise ValueError("temporary command cannot carry recovery state")
        if value["kind"] == "finish_manual_recovery":
            if value["mode"] != "normal" or not outlets:
                raise ValueError("manual recovery requires affected outlets")
            if any(
                item.off_seconds is not None or item.expected_state not in {"on", "off"}
                for item in outlets
            ):
                raise ValueError("manual recovery requires fresh known state context")
        return cls(
            str(value["commandId"]),
            str(value["tankId"]),
            int(value["commandRevision"]),
            int(value["configRevision"]),
            value["kind"],
            value["mode"],
            utc_timestamp(str(value["latestStartAt"])),
            (
                None
                if value["sessionStartCommandId"] is None
                else str(value["sessionStartCommandId"])
            ),
            (
                None
                if value["expectedReportSequence"] is None
                else int(value["expectedReportSequence"])
            ),
            outlets,
        )


@dataclass(frozen=True, slots=True)
class RegistryOutlet:
    outlet_id: str
    registry_entry_id: str
    platform: str
    config_entry_id: str
    device_id: str
    unique_id: str
    entity_id: str
    label: str
    configured_entity_id: str | None = None

    def pinned_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.registry_entry_id,
            self.platform,
            self.config_entry_id,
            self.device_id,
            self.unique_id,
        )

    def wire(self) -> dict[str, str]:
        return {
            "outletId": self.outlet_id,
            "registryEntryId": self.registry_entry_id,
            "platform": self.platform,
            "configEntryId": self.config_entry_id,
            "deviceId": self.device_id,
            "uniqueId": self.unique_id,
            "entityId": self.entity_id,
            "label": self.label,
        }


def require_wire_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid control response")
    return value
