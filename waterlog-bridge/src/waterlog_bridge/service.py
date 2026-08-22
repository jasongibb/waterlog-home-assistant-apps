"""Sampling and scheduling orchestration."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .home_assistant import HomeAssistantClient
from .hydros import HydrosClient
from .models import BridgeConfig, EntityReading, HydrosStreamConfig, StreamConfig
from .queue import DurableQueue
from .sources import SourceAuthenticationError, SourceTransportError
from .uploader import WaterlogUploader


LOGGER = logging.getLogger(__name__)

# The pre-multi-source code used these exact codes/messages for an
# HA-only bridge; a config with only Home Assistant streams must keep
# emitting them unchanged.
_HOME_ASSISTANT_UNREACHABLE_CODE = "home_assistant_unreachable"
_HOME_ASSISTANT_PARTIAL_CODE = "partial_home_assistant_failure"

# Multi-source (or non-HA sole source) configs get honest generic codes
# instead of borrowing the HA-specific ones.
_GENERIC_UNREACHABLE_CODE = "source_unreachable"
_GENERIC_PARTIAL_CODE = "partial_source_failure"


@dataclass(frozen=True, slots=True)
class SourceGroup:
    """One vendor source polled once per cycle.

    ``read`` is a bound method (e.g. ``HomeAssistantClient.read_entity`` or
    ``HydrosClient.read_input``) rather than a shared "adapter" interface,
    since the two vendor clients do not share a read method name; wrapping
    them in a factory (``home_assistant_group`` / ``hydros_group`` below)
    keeps that detail out of ``BridgeService`` and its callers.
    """

    name: str
    read: Callable[[Any], EntityReading]
    streams: tuple[Any, ...]
    auth_code: str
    unreachable_code: str
    auth_rejected_message: str
    unreachable_message: str
    unavailable_message: str
    invalid_message: str
    auth_log_message: str
    begin_cycle: Callable[[], None] | None = None


def home_assistant_group(
    client: HomeAssistantClient, streams: tuple[StreamConfig, ...]
) -> SourceGroup:
    return SourceGroup(
        name="Home Assistant",
        read=client.read_entity,
        streams=streams,
        auth_code="home_assistant_auth_rejected",
        unreachable_code=_HOME_ASSISTANT_UNREACHABLE_CODE,
        auth_rejected_message="Home Assistant API authentication failed",
        unreachable_message="Home Assistant entity state could not be read",
        unavailable_message="Home Assistant entity is unavailable",
        invalid_message="Home Assistant entity did not contain a usable numeric value",
        auth_log_message=(
            "Home Assistant rejected the Supervisor token; no entity values were sampled"
        ),
    )


def hydros_group(
    client: HydrosClient, streams: tuple[HydrosStreamConfig, ...]
) -> SourceGroup:
    return SourceGroup(
        name="HYDROS",
        read=client.read_input,
        streams=streams,
        auth_code="hydros_auth_rejected",
        unreachable_code="hydros_unreachable",
        auth_rejected_message="HYDROS rejected the provider/device key",
        unreachable_message="HYDROS device state could not be read",
        unavailable_message="HYDROS device state is unavailable",
        invalid_message="HYDROS input did not contain a usable numeric value",
        auth_log_message=(
            "HYDROS rejected the provider/device key; no HYDROS values were sampled"
        ),
        begin_cycle=client.begin_cycle,
    )


class BridgeService:
    def __init__(
        self,
        config: BridgeConfig,
        queue: DurableQueue,
        groups: tuple[SourceGroup, ...],
        uploader: WaterlogUploader,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._groups = groups
        self._uploader = uploader
        self._clock = clock
        self._jitter = jitter
        self._stop = stop_event or threading.Event()

    def poll_once(self, *, now: float) -> tuple[int, int]:
        occurred_at = _timestamp(now)
        sample_count = 0
        unhealthy_count = 0
        failed_stream_counts: list[int] = []

        for group in self._groups:
            if group.begin_cycle is not None:
                group.begin_cycle()
            group_samples, group_unhealthy, group_failed = self._poll_group(
                group, occurred_at=occurred_at, now=now
            )
            sample_count += group_samples
            unhealthy_count += group_unhealthy
            failed_stream_counts.append(group_failed)
            LOGGER.info(
                "%s poll queued %s samples; %s streams were unavailable or invalid",
                group.name,
                group_samples,
                group_unhealthy,
            )

        total_streams = sum(len(group.streams) for group in self._groups)
        total_failed = sum(failed_stream_counts)

        if total_failed == 0:
            self._queue.enqueue_bridge_status(
                occurred_at=occurred_at, status="ok", now=now
            )
        else:
            sole_group_is_home_assistant = (
                len(self._groups) == 1
                and self._groups[0].unreachable_code == _HOME_ASSISTANT_UNREACHABLE_CODE
            )
            if sole_group_is_home_assistant:
                full_outage_code = _HOME_ASSISTANT_UNREACHABLE_CODE
                partial_code = _HOME_ASSISTANT_PARTIAL_CODE
                message = "Home Assistant state polling encountered a transport failure"
            else:
                full_outage_code = _GENERIC_UNREACHABLE_CODE
                partial_code = _GENERIC_PARTIAL_CODE
                message = "Telemetry source polling encountered a transport failure"
            code = full_outage_code if total_failed == total_streams else partial_code
            self._queue.enqueue_bridge_status(
                occurred_at=occurred_at,
                status="error",
                code=code,
                message=message,
                now=now,
            )

        return sample_count, unhealthy_count

    def _poll_group(
        self, group: SourceGroup, *, occurred_at: str, now: float
    ) -> tuple[int, int, int]:
        sample_count = 0
        unhealthy_count = 0
        streams = group.streams

        for index, stream in enumerate(streams):
            try:
                reading = group.read(stream)
            except SourceAuthenticationError:
                LOGGER.critical(group.auth_log_message)
                for remaining in streams[index:]:
                    self._queue.enqueue_stream_status_if_changed(
                        stream_id=remaining.stream_id,
                        occurred_at=occurred_at,
                        status="error",
                        code=group.auth_code,
                        message=group.auth_rejected_message,
                        now=now,
                    )
                    unhealthy_count += 1
                return sample_count, unhealthy_count, len(streams) - index
            except SourceTransportError:
                # A transport failure is source-wide for this cycle. Avoid
                # multiplying the configured timeout by every remaining stream.
                for remaining in streams[index:]:
                    self._queue.enqueue_stream_status_if_changed(
                        stream_id=remaining.stream_id,
                        occurred_at=occurred_at,
                        status="error",
                        code=group.unreachable_code,
                        message=group.unreachable_message,
                        now=now,
                    )
                    unhealthy_count += 1
                return sample_count, unhealthy_count, len(streams) - index

            if reading.status == "ok":
                assert reading.value is not None and reading.unit is not None
                self._queue.enqueue_sample(
                    stream_id=stream.stream_id,
                    observed_at=occurred_at,
                    source_updated_at=reading.source_updated_at,
                    value=reading.value,
                    unit=reading.unit,
                    now=now,
                )
                self._queue.enqueue_stream_status_if_changed(
                    stream_id=stream.stream_id,
                    occurred_at=occurred_at,
                    status="ok",
                    now=now,
                )
                sample_count += 1
            else:
                self._queue.enqueue_stream_status_if_changed(
                    stream_id=stream.stream_id,
                    occurred_at=occurred_at,
                    status=reading.status,
                    code=reading.code,
                    message=(
                        group.unavailable_message
                        if reading.status == "unavailable"
                        else group.invalid_message
                    ),
                    now=now,
                )
                unhealthy_count += 1

        return sample_count, unhealthy_count, 0

    def enforce_queue_limits(self, *, now: float) -> int:
        expired, overflow = self._queue.enforce_limits(
            now=now,
            retention_days=self._config.queue_retention_days,
            max_items=self._config.max_queue_items,
            reserve_items=1,
        )
        dropped = expired + overflow
        if dropped:
            self._queue.enqueue_bridge_status(
                occurred_at=_timestamp(now),
                status="error",
                code="queue_items_dropped",
                message="The durable telemetry queue exceeded its configured limit",
                details={"expiredItems": expired, "overflowItems": overflow},
                now=now,
            )
            LOGGER.critical(
                "Durable queue discarded %s expired/overflow items; a health event was retained",
                dropped,
            )
        return dropped

    def run(self) -> None:
        now = self._clock()
        next_poll = now
        next_upload: float | None = now
        auth_blocked = False
        last_auth_reminder = 0.0

        total_streams = sum(len(group.streams) for group in self._groups)
        stats = self._queue.stats()
        LOGGER.info(
            "Waterlog Bridge started with %s stream mappings, %s queued items, and %s quarantined items",
            total_streams,
            stats.pending,
            stats.quarantined,
        )
        if stats.quarantined:
            LOGGER.warning(
                "%s permanently rejected telemetry items remain quarantined in /data/waterlog-bridge.sqlite3",
                stats.quarantined,
            )

        while not self._stop.is_set():
            now = self._clock()
            if now >= next_poll:
                self.poll_once(now=now)
                self.enforce_queue_limits(now=now)
                next_poll = now + self._config.sample_interval_seconds

            if next_upload is not None and now >= next_upload and not auth_blocked:
                outcome = self._uploader.upload_once(now=now)
                if outcome.auth_blocked:
                    auth_blocked = True
                    next_upload = None
                    last_auth_reminder = now
                    self._queue.enqueue_bridge_status(
                        occurred_at=_timestamp(now),
                        status="error",
                        code="waterlog_auth_rejected",
                        message="Waterlog rejected the bridge credential",
                        now=now,
                    )
                elif outcome.has_more_due:
                    next_upload = now + 1
                elif outcome.retry_at is not None:
                    next_upload = outcome.retry_at
                else:
                    interval = self._config.upload_interval_seconds
                    next_upload = now + self._jitter(interval * 0.95, interval * 1.05)

            if auth_blocked and now - last_auth_reminder >= 3600:
                LOGGER.critical(
                    "Waterlog uploads remain disabled after credential rejection; repair the credential and restart the app"
                )
                last_auth_reminder = now

            wake_candidates = [next_poll]
            if next_upload is not None:
                wake_candidates.append(next_upload)
            sleep_seconds = max(0.1, min(30.0, min(wake_candidates) - self._clock()))
            self._stop.wait(sleep_seconds)

        LOGGER.info("Waterlog Bridge stopped")


def _timestamp(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
