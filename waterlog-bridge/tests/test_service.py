from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from waterlog_bridge.models import (
    BridgeConfig,
    EntityReading,
    StreamConfig,
    UploadOutcome,
)
from waterlog_bridge.queue import DurableQueue
from waterlog_bridge.service import BridgeService


FIRST_STREAM = "11111111-1111-4111-8111-111111111111"
SECOND_STREAM = "22222222-2222-4222-8222-222222222222"


class FakeHomeAssistant:
    def read_entity(self, stream: StreamConfig) -> EntityReading:
        if stream.stream_id == FIRST_STREAM:
            return EntityReading(
                status="ok",
                value=78.25,
                unit="°F",
                source_updated_at="2026-07-15T02:59:58.000Z",
            )
        return EntityReading(status="unavailable", code="entity_unavailable")


class FakeUploader:
    def upload_once(self, *, now: float) -> UploadOutcome:
        return UploadOutcome()


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.queue = DurableQueue(Path(self.directory.name, "queue.sqlite3"))
        self.config = BridgeConfig(
            waterlog_url="https://waterlog.fish",
            credential="wlb_test-credential-never-log",
            streams=(
                StreamConfig(FIRST_STREAM, "sensor.good_temperature"),
                StreamConfig(SECOND_STREAM, "sensor.unavailable_temperature"),
            ),
        )
        self.service = BridgeService(
            self.config,
            self.queue,
            FakeHomeAssistant(),
            FakeUploader(),
        )

    def tearDown(self) -> None:
        self.queue.close()
        self.directory.cleanup()

    def test_poll_separates_numeric_sample_probe_failure_and_bridge_health(self) -> None:
        sample_count, unhealthy_count = self.service.poll_once(now=100)
        self.assertEqual((sample_count, unhealthy_count), (1, 1))
        items = self.queue.due_items(now=100, limit=20)
        samples = [item.payload for item in items if item.kind == "sample"]
        statuses = [item.payload for item in items if item.kind == "status"]
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["streamId"], FIRST_STREAM)
        self.assertEqual(samples[0]["value"], 78.25)
        self.assertEqual(
            samples[0]["sourceUpdatedAt"], "2026-07-15T02:59:58.000Z"
        )
        self.assertNotEqual(samples[0]["value"], 0)
        self.assertTrue(
            any(
                status["kind"] == "bridge" and status["status"] == "ok"
                for status in statuses
            )
        )
        self.assertTrue(
            any(
                status.get("streamId") == SECOND_STREAM
                and status["status"] == "unavailable"
                for status in statuses
            )
        )

    def test_repeated_probe_failure_is_an_edge_but_bridge_heartbeat_repeats(self) -> None:
        self.service.poll_once(now=100)
        first_count = self.queue.stats().pending
        self.service.poll_once(now=400)
        new_items = self.queue.stats().pending - first_count
        # One new valid sample plus one bridge heartbeat. Neither unchanged stream
        # status is repeated.
        self.assertEqual(new_items, 2)

    def test_queue_overflow_retains_a_loud_bridge_health_event(self) -> None:
        small_config = BridgeConfig(
            waterlog_url=self.config.waterlog_url,
            credential=self.config.credential,
            streams=self.config.streams,
            max_queue_items=1000,
        )
        service = BridgeService(
            small_config,
            self.queue,
            FakeHomeAssistant(),
            FakeUploader(),
        )
        # Use the queue API directly to avoid 1000 HTTP fixture reads.
        for index in range(1001):
            self.queue.enqueue_bridge_status(
                occurred_at=f"2026-07-15T03:00:{index % 60:02d}.000Z",
                status="ok",
                now=100 + index,
            )
        dropped = service.enforce_queue_limits(now=2000)
        self.assertEqual(dropped, 2)
        items = self.queue.due_items(now=2000, limit=1100)
        self.assertTrue(
            any(
                item.payload.get("code") == "queue_items_dropped" for item in items
            )
        )
        self.assertEqual(self.queue.stats().pending, 1000)


if __name__ == "__main__":
    unittest.main()
