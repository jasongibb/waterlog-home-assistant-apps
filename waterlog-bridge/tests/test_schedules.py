from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from waterlog_bridge.schedules import (
    OutletSchedule,
    latest_edge,
    next_edge,
    parse_schedule_list,
    scheduled_state,
    zone,
)

NY = ZoneInfo("America/New_York")
OUTLET = "20000000-0000-4000-8000-000000000001"
MONDAY = date(2026, 11, 9)  # A Monday well clear of DST changes.
DAILY = 127


def local(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).timestamp()


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def bit(day: date) -> int:
    return 1 << day.weekday()


class ScheduleMathTests(unittest.TestCase):
    def test_weekday_bits_start_on_monday(self) -> None:
        self.assertEqual(MONDAY.weekday(), 0)
        self.assertTrue(OutletSchedule(1, "09:00", "17:00").runs_on(MONDAY))
        self.assertTrue(
            OutletSchedule(64, "09:00", "17:00").runs_on(MONDAY - timedelta(days=1))
        )
        self.assertFalse(OutletSchedule(64, "09:00", "17:00").runs_on(MONDAY))

    def test_daily_window(self) -> None:
        schedule = OutletSchedule(DAILY, "09:00", "17:00")
        self.assertEqual(scheduled_state(schedule, NY, local(MONDAY, 8, 59)), "off")
        self.assertEqual(scheduled_state(schedule, NY, local(MONDAY, 9)), "on")
        self.assertEqual(scheduled_state(schedule, NY, local(MONDAY, 16, 59)), "on")
        self.assertEqual(scheduled_state(schedule, NY, local(MONDAY, 17)), "off")
        self.assertEqual(latest_edge(schedule, NY, local(MONDAY, 12)).at, local(MONDAY, 9))
        upcoming = next_edge(schedule, NY, local(MONDAY, 12))
        self.assertEqual((upcoming.at, upcoming.state), (local(MONDAY, 17), "off"))
        # Strictly after: an edge at the instant itself is not the next one.
        self.assertEqual(
            next_edge(schedule, NY, local(MONDAY, 17)).at,
            local(MONDAY + timedelta(days=1), 9),
        )

    def test_chosen_weekdays(self) -> None:
        wednesday = MONDAY + timedelta(days=2)
        schedule = OutletSchedule(bit(MONDAY) | bit(wednesday), "07:00", "19:00")
        tuesday = MONDAY + timedelta(days=1)
        self.assertEqual(scheduled_state(schedule, NY, local(tuesday, 12)), "off")
        self.assertEqual(latest_edge(schedule, NY, local(tuesday, 12)).at, local(MONDAY, 19))
        self.assertEqual(scheduled_state(schedule, NY, local(wednesday, 10)), "on")
        self.assertEqual(
            next_edge(schedule, NY, local(wednesday, 20)).at,
            local(MONDAY + timedelta(days=7), 7),
        )

    def test_overnight_window_belongs_to_the_day_it_starts(self) -> None:
        daily = OutletSchedule(DAILY, "22:00", "06:00")
        self.assertEqual(scheduled_state(daily, NY, local(MONDAY, 23)), "on")
        self.assertEqual(scheduled_state(daily, NY, local(MONDAY, 5, 59)), "on")
        self.assertEqual(scheduled_state(daily, NY, local(MONDAY, 6)), "off")
        self.assertEqual(scheduled_state(daily, NY, local(MONDAY, 21, 59)), "off")

        friday = MONDAY + timedelta(days=4)
        saturday = friday + timedelta(days=1)
        friday_only = OutletSchedule(bit(friday), "22:00", "06:00")
        self.assertEqual(scheduled_state(friday_only, NY, local(saturday, 3)), "on")
        self.assertEqual(scheduled_state(friday_only, NY, local(saturday, 23)), "off")
        self.assertEqual(scheduled_state(friday_only, NY, local(friday, 3)), "off")

    def test_sunday_and_monday_boundaries(self) -> None:
        sunday = MONDAY - timedelta(days=1)
        sunday_night = OutletSchedule(64, "22:00", "02:00")
        self.assertEqual(scheduled_state(sunday_night, NY, local(sunday, 23)), "on")
        self.assertEqual(scheduled_state(sunday_night, NY, local(MONDAY, 1)), "on")
        self.assertEqual(scheduled_state(sunday_night, NY, local(MONDAY, 2)), "off")
        self.assertEqual(
            next_edge(sunday_night, NY, local(MONDAY, 2)).at,
            local(sunday + timedelta(days=7), 22),
        )

        monday_midnight = OutletSchedule(1, "00:00", "01:00")
        self.assertEqual(scheduled_state(monday_midnight, NY, local(sunday, 23, 59)), "off")
        self.assertEqual(scheduled_state(monday_midnight, NY, local(MONDAY, 0, 30)), "on")
        # Looking back across a whole week finds last Monday's off edge.
        self.assertEqual(
            latest_edge(monday_midnight, NY, local(sunday, 23, 59)).at,
            local(MONDAY - timedelta(days=7), 1),
        )

    def test_spring_forward_gap_lands_one_hour_later(self) -> None:
        # 2026-03-08: 02:00 EST jumps to 03:00 EDT, so 02:30 does not exist.
        schedule = OutletSchedule(DAILY, "02:30", "08:00")
        self.assertEqual(latest_edge(schedule, NY, utc(2026, 3, 8, 7, 45)).at, utc(2026, 3, 8, 7, 30))
        self.assertEqual(scheduled_state(schedule, NY, utc(2026, 3, 8, 7, 15)), "off")
        self.assertEqual(scheduled_state(schedule, NY, utc(2026, 3, 8, 7, 30)), "on")

        # Both edges land on 03:30 EDT: that day's window has no length.
        empty = OutletSchedule(DAILY, "02:30", "03:30")
        self.assertEqual(scheduled_state(empty, NY, utc(2026, 3, 8, 7, 31)), "off")
        self.assertEqual(scheduled_state(empty, NY, local(date(2026, 3, 9), 3)), "on")

    def test_fall_back_repeat_uses_the_first_occurrence_once(self) -> None:
        # 2026-11-01: 01:00 to 01:59 happens twice (EDT, then EST).
        schedule = OutletSchedule(DAILY, "00:00", "01:30")
        first = utc(2026, 11, 1, 5, 30)  # 01:30 EDT
        self.assertEqual(latest_edge(schedule, NY, first).at, first)
        self.assertEqual(scheduled_state(schedule, NY, utc(2026, 11, 1, 5, 45)), "off")
        # 01:40 EST is after the repeated 01:30, which is not a second edge.
        self.assertEqual(latest_edge(schedule, NY, utc(2026, 11, 1, 6, 40)).at, first)
        self.assertEqual(next_edge(schedule, NY, first).at, local(date(2026, 11, 2), 0))

    def test_invalid_schedules_are_rejected(self) -> None:
        for weekdays in (0, 128, True, "127"):
            with self.assertRaises(ValueError):
                OutletSchedule(weekdays, "09:00", "17:00")  # type: ignore[arg-type]
        for on_at, off_at in (("09:00", "09:00"), ("24:00", "05:00"), ("9:00", "17:00"), ("09:00:00", "17:00")):
            with self.assertRaises(ValueError):
                OutletSchedule(DAILY, on_at, off_at)

    def test_protocol_schedule_list_is_strict(self) -> None:
        valid = {"outletId": OUTLET, "weekdays": 127, "onAt": "09:00", "offAt": "17:00"}
        parsed = parse_schedule_list([valid])
        self.assertEqual(parsed[0].outlet_id, OUTLET)
        self.assertEqual(parsed[0].schedule.wire(), {"weekdays": 127, "onAt": "09:00", "offAt": "17:00"})
        self.assertEqual(parse_schedule_list([]), ())
        upper = "A0000000-0000-4000-8000-00000000000B"
        self.assertEqual(
            parse_schedule_list([{**valid, "outletId": upper}])[0].outlet_id, upper.lower()
        )
        bad_lists = [
            None,
            {"outletId": OUTLET},
            [{**valid, "extra": 1}],
            [{key: value for key, value in valid.items() if key != "offAt"}],
            [{**valid, "outletId": "not-a-uuid"}],
            [{**valid, "outletId": "{" + OUTLET + "}"}],
            [{**valid, "outletId": OUTLET.replace("-", "")}],
            [valid, valid],
            [{**valid, "weekdays": 0}],
            [{**valid, "offAt": "09:00"}],
            [
                {**valid, "outletId": f"20000000-0000-4000-8000-{index:012d}"}
                for index in range(129)
            ],
        ]
        for value in bad_lists:
            with self.assertRaises(ValueError):
                parse_schedule_list(value)

    def test_zone_rejects_unknown_names(self) -> None:
        self.assertIsNotNone(zone("America/New_York"))
        self.assertIsNone(zone("Not/AZone"))
        self.assertIsNone(zone(""))
        self.assertIsNone(zone(None))


if __name__ == "__main__":
    unittest.main()
