from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from test_control import (
    HEATER,
    INSTALLATION,
    PUMP,
    TANK,
    FakeHA,
    NoCloud,
    command,
    inventory,
    planned_outlet,
)
from waterlog_bridge.control_client import ControlClient, ControlProtocolError
from waterlog_bridge.control_service import ControlService
from waterlog_bridge.control_store import ControlStore
from waterlog_bridge.home_assistant_control import HomeAssistantControlError
from waterlog_bridge.models import HttpResponse
from waterlog_bridge.schedules import parse_schedule_list

NY = ZoneInfo("America/New_York")
MONDAY = date(2026, 11, 9)


def local(day: date, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=NY
    ).timestamp()


def schedules(*items: tuple[str, str, str], weekdays: int = 127):
    return parse_schedule_list(
        [
            {"outletId": outlet_id, "weekdays": weekdays, "onAt": on_at, "offAt": off_at}
            for outlet_id, on_at, off_at in items
        ]
    )


class FlakyHA(FakeHA):
    """Fails the next ``failures`` switch calls without changing the outlet."""

    def __init__(self, states, failures=0):
        super().__init__(states)
        self.failures = failures

    def set_state(self, entity_id, state):
        if self.failures:
            self.failures -= 1
            self.writes.append((entity_id, state))
            raise HomeAssistantControlError("synthetic switch failure")
        return super().set_state(entity_id, state)


class ScheduleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, "control.sqlite3")
        self.now = local(MONDAY, 8)
        self.open_store()
        self.store.install_config(1, {"outlets": []}, self.now)
        self.store.set_time_zone("America/New_York")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.temp.cleanup()

    def open_store(self):
        self.store = ControlStore(self.path, INSTALLATION)

    def restart(self):
        self.store.close()
        self.open_store()

    def service(self, ha, **kwargs):
        return ControlService(
            self.store,
            kwargs.pop("client", NoCloud()),
            ha,
            INSTALLATION,
            kwargs.pop("local_inventory", inventory()),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            **kwargs,
        )

    def at(self, day: date, hour: int, minute: int = 0, second: int = 0):
        self.now = local(day, hour, minute, second)

    def last_edge(self, outlet_id: str):
        row = self.store.schedule(outlet_id)
        return None if row is None else row["last_edge_at"]

    def test_install_applies_current_state_then_an_edge_switches_the_outlet(self):
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)

        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "off")])
        service.tick()
        self.assertEqual(len(ha.writes), 1)

        self.at(MONDAY, 9, 0, 1)
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 9))

    def test_failed_write_retries_and_is_not_marked_applied(self):
        ha = FlakyHA({"switch.pump": "off"}, failures=1)
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        service.tick()
        self.assertEqual(ha.writes, [])

        self.at(MONDAY, 9)
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])
        self.assertEqual(ha.states["switch.pump"], "off")
        self.assertLess(self.last_edge(PUMP), local(MONDAY, 9))

        self.now += 10
        service.tick()
        self.assertEqual(len(ha.writes), 1)

        self.now += 21
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))
        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 9))

    def test_unreadable_outlet_is_retried_without_a_switch_call(self):
        ha = FakeHA({"switch.pump": "unavailable"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        service.tick()
        self.assertEqual(ha.writes, [])
        self.assertIsNone(self.last_edge(PUMP))

        ha.states["switch.pump"] = "on"
        self.now += 31
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "off")])

    def test_manual_change_lasts_until_the_next_edge(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])

        ha.states["switch.pump"] = "off"  # Someone switches it in the Tapo app.
        for hour in (10, 12, 16):
            self.at(MONDAY, hour)
            service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])

        ha.states["switch.pump"] = "on"
        self.at(MONDAY, 17)
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "off"))

    def test_restart_without_a_missed_edge_does_nothing(self):
        ha = FakeHA({"switch.pump": "off"})
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        self.service(ha).tick()
        ha.states["switch.pump"] = "off"

        self.restart()
        self.at(MONDAY, 11)
        self.service(ha).tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])
        self.assertEqual(ha.states["switch.pump"], "off")

    def test_restart_after_missed_edges_applies_only_the_latest(self):
        ha = FakeHA({"switch.pump": "off"})
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        self.service(ha).tick()
        ha.states["switch.pump"] = "off"

        # Down from Monday 11:00 to Tuesday 10:00: Monday's off edge and
        # Tuesday's on edge were missed.
        self.restart()
        tuesday = MONDAY + timedelta(days=1)
        self.at(tuesday, 10)
        service = self.service(ha)
        service.tick()
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on"), ("switch.pump", "on")])
        self.assertEqual(self.last_edge(PUMP), local(tuesday, 9))

    def test_changed_schedule_applies_once_and_unchanged_keeps_progress(self):
        ha = FakeHA({"switch.pump": "off", "switch.heater": "off"})
        service = self.service(ha)
        self.store.install_schedules(
            1, schedules((PUMP, "09:00", "17:00"), (HEATER, "09:00", "17:00")), self.now
        )
        self.at(MONDAY, 9)
        service.tick()
        service.tick()
        self.assertEqual(
            sorted(ha.writes), [("switch.heater", "on"), ("switch.pump", "on")]
        )
        ha.states.update({"switch.pump": "off", "switch.heater": "off"})

        self.at(MONDAY, 12)
        self.store.install_schedules(
            2, schedules((PUMP, "08:00", "18:00"), (HEATER, "09:00", "17:00")), self.now
        )
        self.assertEqual(self.store.schedules_revision(), 2)
        service.tick()
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))
        self.assertEqual(len(ha.writes), 3)
        self.assertEqual(ha.states["switch.heater"], "off")

        # Removing a schedule drops its progress and leaves the outlet alone.
        self.store.install_schedules(3, schedules((HEATER, "09:00", "17:00")), self.now)
        self.assertIsNone(self.store.schedule(PUMP))
        self.at(MONDAY, 18)
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")

    def test_switches_at_most_one_outlet_per_tick(self):
        ha = FakeHA({"switch.pump": "off", "switch.heater": "off"})
        service = self.service(ha)
        self.store.install_schedules(
            1, schedules((PUMP, "09:00", "17:00"), (HEATER, "09:00", "17:00")), self.now
        )
        self.at(MONDAY, 9)
        service.tick()
        self.assertEqual(len(ha.writes), 1)
        service.tick()
        self.assertEqual(len(ha.writes), 2)

    def test_backwards_clock_clamps_and_applies_the_latest_edge_once(self):
        ha = FakeHA({"switch.pump": "off"})
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        self.service(ha).tick()
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 9))

        self.restart()
        self.at(MONDAY, 7)
        service = self.service(ha)
        service.tick()
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on"), ("switch.pump", "off")])
        self.assertEqual(
            self.last_edge(PUMP), local(MONDAY - timedelta(days=1), 17)
        )

    def test_unknown_time_zone_does_nothing(self):
        self.store.set_time_zone("Not/AZone")
        ha = FakeHA({"switch.pump": "on"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        service.tick()
        self.assertEqual(ha.writes, [])

    def test_session_owned_outlet_is_skipped_and_restores_to_its_schedule(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 8, 59, 30)
        service.tick()
        self.assertEqual(ha.writes, [])

        service.accept(
            command(outlets=[planned_outlet(PUMP, seconds=120)], now=self.now), self.now
        )
        self.assertEqual(self.store.obligations(TANK)[0]["result"], "preserved")
        self.at(MONDAY, 9, 0, 10)
        service.tick()
        self.assertEqual(ha.writes, [])
        self.assertEqual(self.store.sessions()[0]["phase"], "active")

        # Water Change ends inside the window: the outlet follows its schedule
        # (on), not its captured off baseline, and the runner does not repeat
        # that change even if someone switches the outlet right afterward.
        self.at(MONDAY, 9, 1, 31)
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 9))
        ha.states["switch.pump"] = "off"
        service.tick()
        self.assertEqual(len(ha.writes), 1)

    def test_early_normal_request_marks_the_scheduled_change_applied(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 8, 59, 30)
        service.tick()
        service.accept(
            command(outlets=[planned_outlet(PUMP, seconds=600)], now=self.now), self.now
        )
        self.at(MONDAY, 9, 1)
        service.accept(
            command(mode="normal", revision=2, outlets=[], now=self.now), self.now
        )
        self.assertEqual(ha.writes, [("switch.pump", "on")])
        self.assertEqual(self.store.sessions(), [])

        ha.states["switch.pump"] = "off"
        service.tick()
        self.assertEqual(ha.writes, [("switch.pump", "on")])

    def test_unconfirmed_entry_write_on_a_scheduled_outlet_is_written_off(self):
        ha = FlakyHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 18)
        service.tick()
        ha.states["switch.pump"] = "on"  # Turned on by hand after hours.
        ha.failures = 1
        with self.assertLogs("waterlog_bridge.control_service", level="ERROR"):
            service.accept(
                command(outlets=[planned_outlet(PUMP)], now=self.now), self.now
            )
        self.assertEqual(ha.states["switch.pump"], "off")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["phase"], "normal")

    def test_feed_ending_after_the_off_time_leaves_the_outlet_off(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")

        self.at(MONDAY, 16, 59, 30)
        service.accept(command(outlets=[planned_outlet(PUMP)], now=self.now), self.now)
        self.assertEqual(ha.states["switch.pump"], "off")
        self.at(MONDAY, 17, 0, 31)
        service.tick()
        service.tick()

        self.assertEqual(ha.states["switch.pump"], "off")
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["phase"], "normal")
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 17))
        self.assertEqual(
            ha.writes, [("switch.pump", "on"), ("switch.pump", "off")]
        )

    def test_unscheduled_outlet_restores_its_baseline(self):
        ha = FakeHA({"switch.pump": "on", "switch.heater": "on"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 16, 59, 30)
        service.tick()
        service.accept(
            command(outlets=[planned_outlet(HEATER, seconds=60)], now=self.now),
            self.now,
        )
        self.at(MONDAY, 17, 0, 31)
        service.tick()
        service.tick()
        self.assertEqual(ha.states["switch.heater"], "on")
        self.assertEqual(ha.states["switch.pump"], "off")
        self.assertEqual(self.store.sessions(), [])

    def test_schedule_work_waits_for_a_restoration_coming_due(self):
        ha = FakeHA({"switch.pump": "off", "switch.heater": "on"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 8, 59, 0)
        service.tick()
        service.accept(
            command(outlets=[planned_outlet(HEATER, seconds=70)], now=self.now),
            self.now,
        )
        # Heater restores at 09:00:10; the pump's 09:00 edge waits for it.
        self.at(MONDAY, 9, 0, 0)
        service.tick()
        self.assertNotIn(("switch.pump", "on"), ha.writes)
        self.at(MONDAY, 9, 0, 11)
        service.tick()
        service.tick()
        self.assertIn(("switch.heater", "on"), ha.writes)
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))

    def test_schedule_work_waits_for_a_restoration_due_within_a_minute(self):
        ha = FakeHA({"switch.pump": "off", "switch.heater": "on"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 8, 59, 0)
        service.tick()
        service.accept(
            command(outlets=[planned_outlet(HEATER, seconds=100)], now=self.now),
            self.now,
        )
        # The heater restores at 09:00:40, 40 s after the pump's edge is due.
        self.at(MONDAY, 9, 0, 0)
        service.tick()
        self.assertNotIn(("switch.pump", "on"), ha.writes)
        self.at(MONDAY, 9, 0, 41)
        service.tick()
        service.tick()
        self.assertIn(("switch.heater", "on"), ha.writes)
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))

    def test_failed_writes_back_off_from_the_end_of_each_attempt(self):
        test = self

        class SlowFlakyHA(FlakyHA):
            def set_state(self, entity_id, state):
                test.now += 40  # the switch call and readback take 40 s
                return super().set_state(entity_id, state)

        ha = SlowFlakyHA({"switch.pump": "off"}, failures=3)
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        service.tick()
        self.at(MONDAY, 9)
        service.tick()  # attempt 1 fails at 09:00:40; next try 30 s later
        self.assertEqual(len(ha.writes), 1)
        self.at(MONDAY, 9, 1, 9)
        service.tick()
        self.assertEqual(len(ha.writes), 1)
        self.at(MONDAY, 9, 1, 10)
        service.tick()  # attempt 2 fails at 09:01:50; next try 60 s later
        self.assertEqual(len(ha.writes), 2)
        self.at(MONDAY, 9, 2, 49)
        service.tick()
        self.assertEqual(len(ha.writes), 2)
        self.at(MONDAY, 9, 2, 50)
        service.tick()  # attempt 3 fails at 09:03:30; next try 120 s later
        self.assertEqual(len(ha.writes), 3)
        self.at(MONDAY, 9, 5, 29)
        service.tick()
        self.assertEqual(len(ha.writes), 3)
        self.at(MONDAY, 9, 5, 30)
        service.tick()  # attempt 4 succeeds
        self.assertEqual(ha.states["switch.pump"], "on")
        self.assertEqual(self.last_edge(PUMP), local(MONDAY, 9))

    def test_unreadable_scheduled_outlet_at_restore_is_retried_not_flagged(self):
        ha = FakeHA({"switch.pump": "off"})
        service = self.service(ha)
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 9)
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")

        self.at(MONDAY, 16, 59, 30)
        service.accept(command(outlets=[planned_outlet(PUMP)], now=self.now), self.now)
        self.assertEqual(ha.states["switch.pump"], "off")
        ha.states["switch.pump"] = "unavailable"
        self.at(MONDAY, 17, 0, 31)
        service.tick()
        self.assertNotIn(
            "external_change",
            {str(item["error_code"]) for item in self.store.obligations(TANK)},
        )
        self.assertNotIn(
            "recovery_required", {str(row["phase"]) for row in self.store.sessions()}
        )

        ha.states["switch.pump"] = "off"
        self.now += 31
        service.tick()
        service.tick()
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.reports()[-1]["phase"], "normal")
        self.assertEqual(ha.states["switch.pump"], "off")

    def test_time_zone_change_applies_each_schedule_once_in_the_new_zone(self):
        ha = FakeHA({"switch.pump": "off"})
        zone_name = {"value": "America/New_York"}
        service = self.service(ha, time_zone_provider=lambda: zone_name["value"])
        self.store.install_schedules(1, schedules((PUMP, "09:00", "17:00")), self.now)
        self.at(MONDAY, 10)
        service.tick()
        self.assertEqual(ha.states["switch.pump"], "on")

        # 10:00 in New York is 07:00 in Los Angeles: before the window opens.
        zone_name["value"] = "America/Los_Angeles"
        service.refresh_registry()
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "off"))
        self.assertEqual(self.store.time_zone(), "America/Los_Angeles")

        # 09:00 in Los Angeles turns it back on.
        self.now = datetime(2026, 11, 9, 9, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
        service.tick()
        self.assertEqual(ha.writes[-1], ("switch.pump", "on"))


def v2_response(**overrides):
    value = {
        "protocolVersion": 2,
        "serverTime": "2026-11-09T14:00:00.000Z",
        "acknowledgedReportIds": [],
        "retryAfterSeconds": 2,
        "configRevision": 1,
        "configuration": None,
        "commands": [],
        "recoveryRequired": False,
        "recoveryTankIds": [],
        "reportSequenceFloor": 0,
        "schedulesRevision": 3,
        "schedules": [
            {"outletId": PUMP, "weekdays": 127, "onAt": "09:00", "offAt": "17:00"}
        ],
    }
    value.update(overrides)
    return value


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = local(MONDAY, 8)
        self.store = ControlStore(Path(self.temp.name, "control.sqlite3"), INSTALLATION)
        self.store.install_config(1, {"outlets": []}, self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def client_result(self, body):
        class Transport:
            def post_json(self, url, *, headers, payload, timeout):  # noqa: ANN001
                return HttpResponse(200, {}, json.dumps(body).encode())

        return ControlClient("https://waterlog.example", "credential-value", transport=Transport()).exchange({})

    def test_client_accepts_v2_and_rejects_malformed_schedules(self):
        self.assertEqual(self.client_result(v2_response())["schedulesRevision"], 3)
        self.assertIsNone(self.client_result(v2_response(schedules=None))["schedules"])
        legacy = v2_response(protocolVersion=1)
        del legacy["schedulesRevision"], legacy["schedules"]
        malformed = [
            legacy,
            v2_response(protocolVersion=1),
            v2_response(protocolVersion=3),
            v2_response(schedulesRevision=-1),
            v2_response(schedulesRevision=True),
            v2_response(schedules={}),
            v2_response(schedules=[{"outletId": PUMP, "weekdays": 0, "onAt": "09:00", "offAt": "17:00"}]),
            v2_response(schedules=[{"outletId": PUMP, "weekdays": 1, "onAt": "09:00", "offAt": "09:00"}]),
            v2_response(schedules=[{"outletId": PUMP, "weekdays": 1, "onAt": "09:00"}]),
            {**v2_response(), "extra": None},
        ]
        for body in malformed:
            with self.assertRaises(ControlProtocolError):
                self.client_result(body)

    def test_payload_carries_schedule_revision_time_zone_strip_and_state(self):
        payloads = []

        class Cloud:
            def exchange(inner_self, payload):
                payloads.append(payload)
                return v2_response(schedules=None if len(payloads) > 1 else v2_response()["schedules"])

        local_inventory = (replace(inventory()[0], strip_name="Pea Puffer Power Strip"), inventory()[1])
        ha = FakeHA({"switch.pump": "on", "switch.heater": "off"})
        service = ControlService(
            self.store,
            Cloud(),
            ha,
            INSTALLATION,
            local_inventory,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            time_zone_provider=lambda: "America/New_York",
        )
        service.exchange_once()
        first = payloads[0]
        self.assertEqual(
            set(first),
            {
                "protocolVersion",
                "installationId",
                "reportSequence",
                "knownConfigRevision",
                "knownSchedulesRevision",
                "timeZone",
                "acknowledgedCommandIds",
                "inventory",
                "results",
            },
        )
        self.assertEqual(first["protocolVersion"], 2)
        self.assertEqual(first["knownSchedulesRevision"], 0)
        self.assertIsNone(first["timeZone"])
        self.assertEqual(first["inventory"][0]["state"], "unknown")
        self.assertEqual(first["inventory"][0]["stripName"], "Pea Puffer Power Strip")
        self.assertIsNone(first["inventory"][1]["stripName"])
        self.assertEqual(len(first["inventory"][0]), 10)

        service.refresh_registry()
        service.refresh_states()
        service.exchange_once()
        second = payloads[1]
        # Acknowledged only after the schedules were committed locally.
        self.assertEqual(second["knownSchedulesRevision"], 3)
        self.assertEqual(second["timeZone"], "America/New_York")
        self.assertEqual(
            [item["state"] for item in second["inventory"]], ["on", "off"]
        )
        self.assertEqual(self.store.schedule(PUMP)["on_at"], "09:00")
        self.assertEqual(self.store.time_zone(), "America/New_York")

        self.now += 61
        service.exchange_once()
        self.assertEqual(
            [item["state"] for item in payloads[2]["inventory"]], ["unknown", "unknown"]
        )

    def test_registry_refresh_failure_keeps_the_last_good_inventory(self):
        calls = []

        def discover(pinned):
            calls.append(pinned)
            if len(calls) > 1:
                raise RuntimeError("registry unavailable")
            return (replace(inventory()[0], label="Renamed Pump"),)

        def time_zone():
            raise HomeAssistantControlError("unreachable")

        service = ControlService(
            self.store,
            NoCloud(),
            FakeHA({}),
            INSTALLATION,
            inventory(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            inventory_discoverer=discover,
            time_zone_provider=time_zone,
        )
        with self.assertLogs("waterlog_bridge.control_service", level="WARNING"):
            service.refresh_registry()
            service.refresh_registry()
        self.assertEqual([item.label for item in service.inventory], ["Renamed Pump"])
        self.assertEqual([row["label"] for row in self.store.inventory()], ["Renamed Pump"])
        self.assertEqual(calls[1], service.inventory)
        self.assertIsNone(self.store.time_zone())


if __name__ == "__main__":
    unittest.main()
