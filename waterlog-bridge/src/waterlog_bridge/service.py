"""Sampling and scheduling orchestration."""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .home_assistant import (
    HomeAssistantAuthenticationError,
    HomeAssistantClient,
    HomeAssistantTransportError,
)
from .models import BridgeConfig
from .queue import DurableQueue
from .uploader import WaterlogUploader


LOGGER = logging.getLogger(__name__)


class BridgeService:
    def __init__(
        self,
        config: BridgeConfig,
        queue: DurableQueue,
        home_assistant: HomeAssistantClient,
        uploader: WaterlogUploader,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._home_assistant = home_assistant
        self._uploader = uploader
        self._clock = clock
        self._jitter = jitter
        self._stop = stop_event or threading.Event()

    def poll_once(self, *, now: float) -> tuple[int, int]:
        occurred_at = _timestamp(now)
        sample_count = 0
        unhealthy_count = 0
        transport_failures = 0

        for index, stream in enumerate(self._config.streams):
            try:
                reading = self._home_assistant.read_entity(stream)
            except HomeAssistantAuthenticationError:
                LOGGER.critical(
                    "Home Assistant rejected the Supervisor token; no entity values were sampled"
                )
                for remaining in self._config.streams[index:]:
                    self._queue.enqueue_stream_status_if_changed(
                        stream_id=remaining.stream_id,
                        occurred_at=occurred_at,
                        status="error",
                        code="home_assistant_auth_rejected",
                        message="Home Assistant API authentication failed",
                        now=now,
                    )
                    unhealthy_count += 1
                transport_failures += len(self._config.streams) - index
                break
            except HomeAssistantTransportError:
                # A proxy/network failure is installation-wide. Avoid multiplying
                # the configured timeout by every stream while Core is offline.
                for remaining in self._config.streams[index:]:
                    self._queue.enqueue_stream_status_if_changed(
                        stream_id=remaining.stream_id,
                        occurred_at=occurred_at,
                        status="error",
                        code="home_assistant_unreachable",
                        message="Home Assistant entity state could not be read",
                        now=now,
                    )
                    unhealthy_count += 1
                transport_failures += len(self._config.streams) - index
                break

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
                        "Home Assistant entity is unavailable"
                        if reading.status == "unavailable"
                        else "Home Assistant entity did not contain a usable numeric value"
                    ),
                    now=now,
                )
                unhealthy_count += 1

        if transport_failures:
            code = (
                "home_assistant_unreachable"
                if transport_failures == len(self._config.streams)
                else "partial_home_assistant_failure"
            )
            self._queue.enqueue_bridge_status(
                occurred_at=occurred_at,
                status="error",
                code=code,
                message="Home Assistant state polling encountered a transport failure",
                now=now,
            )
        else:
            # A bridge heartbeat is intentionally queued every sample cycle. It proves
            # the Pi is healthy even when every mapped probe is unavailable.
            self._queue.enqueue_bridge_status(
                occurred_at=occurred_at, status="ok", now=now
            )

        LOGGER.info(
            "Home Assistant poll queued %s samples; %s streams were unavailable or invalid",
            sample_count,
            unhealthy_count,
        )
        return sample_count, unhealthy_count

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

        stats = self._queue.stats()
        LOGGER.info(
            "Waterlog Bridge started with %s stream mappings, %s queued items, and %s quarantined items",
            len(self._config.streams),
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
