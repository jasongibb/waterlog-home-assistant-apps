"""Pure outlet schedule math shared by the runner and Tank Mode restore.

A schedule turns an outlet on at ``on_at`` on each selected weekday and off at
``off_at``: the same day when ``off_at`` is later, otherwise the next day (an
overnight window). Wall-clock times are local to the Home Assistant time zone
and become instants with ``fold=0``: a time in a spring-forward gap lands one
hour later and a repeated fall-back time uses its first occurrence.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ScheduledState = Literal["on", "off"]

ALL_WEEKDAYS = 127
LOOKBACK_DAYS = 8
MAX_SCHEDULES = 128
_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def _minutes(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("schedule time must be HH:MM")
    match = _TIME.fullmatch(value)
    if match is None:
        raise ValueError("schedule time must be HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


@dataclass(frozen=True, slots=True)
class OutletSchedule:
    """One outlet's window. Weekday bit 0 is Monday and bit 6 is Sunday."""

    weekdays: int
    on_at: str
    off_at: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.weekdays, bool)
            or not isinstance(self.weekdays, int)
            or not 1 <= self.weekdays <= ALL_WEEKDAYS
        ):
            raise ValueError("schedule weekdays must be between 1 and 127")
        if _minutes(self.on_at) == _minutes(self.off_at):
            raise ValueError("schedule on and off times must differ")

    @property
    def on_minute(self) -> int:
        return _minutes(self.on_at)

    @property
    def off_minute(self) -> int:
        return _minutes(self.off_at)

    @property
    def overnight(self) -> bool:
        return self.off_minute < self.on_minute

    def runs_on(self, day: date) -> bool:
        return bool(self.weekdays & (1 << day.weekday()))

    def wire(self) -> dict[str, object]:
        return {"weekdays": self.weekdays, "onAt": self.on_at, "offAt": self.off_at}


@dataclass(frozen=True, slots=True)
class InstalledSchedule:
    outlet_id: str
    schedule: OutletSchedule


@dataclass(frozen=True, slots=True)
class Edge:
    at: float
    state: ScheduledState


def parse_schedule_list(value: object) -> tuple[InstalledSchedule, ...]:
    """Strictly parse the protocol v2 ``schedules`` list."""

    if not isinstance(value, list) or len(value) > MAX_SCHEDULES:
        raise ValueError("invalid schedules")
    parsed: list[InstalledSchedule] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "outletId",
            "weekdays",
            "onAt",
            "offAt",
        }:
            raise ValueError("invalid schedule")
        raw_id = item["outletId"]
        if not isinstance(raw_id, str):
            raise ValueError("invalid schedule outlet")
        try:
            outlet_id = str(uuid.UUID(raw_id))
        except ValueError as error:
            raise ValueError("invalid schedule outlet") from error
        if outlet_id != raw_id.lower() or outlet_id in seen:
            raise ValueError("invalid schedule outlet")
        seen.add(outlet_id)
        parsed.append(
            InstalledSchedule(
                outlet_id,
                OutletSchedule(item["weekdays"], item["onAt"], item["offAt"]),
            )
        )
    return tuple(parsed)


def zone(name: str | None) -> tzinfo | None:
    """Return the named IANA zone, or None when it is missing or invalid."""

    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _instant(day: date, minute: int, tz: tzinfo) -> float:
    return datetime(
        day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=tz, fold=0
    ).timestamp()


def _edges_for_days(
    schedule: OutletSchedule, tz: tzinfo, first: date, last: date
) -> list[Edge]:
    keyed: list[tuple[float, date, int, Edge]] = []
    day = first
    while day <= last:
        if schedule.runs_on(day):
            on = _instant(day, schedule.on_minute, tz)
            off_day = day + timedelta(days=1) if schedule.overnight else day
            off = _instant(off_day, schedule.off_minute, tz)
            # Ties only happen around DST changes. Within one window the off
            # edge wins (a zero-length window stays off); a later window's
            # edge wins over an earlier window's edge.
            keyed.append((on, day, 0, Edge(on, "on")))
            keyed.append((off, day, 1, Edge(off, "off")))
        day += timedelta(days=1)
    keyed.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in keyed]


def _local_date(at: float, tz: tzinfo) -> date:
    return datetime.fromtimestamp(at, tz=tz).date()


def latest_edge(schedule: OutletSchedule, tz: tzinfo, at: float) -> Edge:
    """The latest edge at or before ``at``, looking back at most 8 days."""

    today = _local_date(at, tz)
    edges = _edges_for_days(
        schedule, tz, today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=1)
    )
    floor = at - LOOKBACK_DAYS * 86_400
    candidates = [edge for edge in edges if floor <= edge.at <= at]
    if not candidates:
        # Every valid schedule has an on and an off edge each week.
        raise ValueError("schedule has no edge in the lookback window")
    return candidates[-1]


def next_edge(schedule: OutletSchedule, tz: tzinfo, after: float) -> Edge:
    """The first edge strictly after ``after``."""

    today = _local_date(after, tz)
    edges = _edges_for_days(
        schedule, tz, today - timedelta(days=1), today + timedelta(days=LOOKBACK_DAYS)
    )
    for edge in edges:
        if edge.at > after:
            return edge
    raise ValueError("schedule has no upcoming edge")


def scheduled_state(schedule: OutletSchedule, tz: tzinfo, at: float) -> ScheduledState:
    return latest_edge(schedule, tz, at).state
