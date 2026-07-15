from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from waterlog_bridge.queue import DurableQueue


STREAM_ID = "11111111-1111-4111-8111-111111111111"


class DurableQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name, "queue.sqlite3")
        self.queue = DurableQueue(self.path)

    def tearDown(self) -> None:
        self.queue.close()
        self.directory.cleanup()

    def test_sample_survives_reopen_until_explicit_acknowledgement(self) -> None:
        client_id = self.queue.enqueue_sample(
            stream_id=STREAM_ID,
            observed_at="2026-07-15T03:00:00.000Z",
            source_updated_at="2026-07-15T02:59:58.000Z",
            value=78.2,
            unit="°F",
            now=100,
        )
        self.queue.close()
        self.queue = DurableQueue(self.path)
        items = self.queue.due_items(now=100, limit=10)
        self.assertEqual(items[0].client_id, client_id)
        self.assertEqual(items[0].payload["value"], 78.2)
        self.assertEqual(
            items[0].payload["sourceUpdatedAt"], "2026-07-15T02:59:58.000Z"
        )
        self.queue.acknowledge([client_id])
        self.assertEqual(self.queue.stats().pending, 0)

    def test_retry_and_quarantine_are_separate_states(self) -> None:
        first = self.queue.enqueue_sample(
            stream_id=STREAM_ID,
            observed_at="2026-07-15T03:00:00.000Z",
            value=78.2,
            unit="°F",
            now=100,
        )
        second = self.queue.enqueue_bridge_status(
            occurred_at="2026-07-15T03:00:00.000Z", status="ok", now=100
        )
        self.queue.retry([first], error_code="network", next_attempt_at=200)
        self.queue.quarantine([second], reason="unknown_stream", now=101)
        self.assertEqual(self.queue.due_items(now=150, limit=10), [])
        self.assertEqual(self.queue.stats().pending, 1)
        self.assertEqual(self.queue.stats().quarantined, 1)
        self.assertEqual(self.queue.due_items(now=200, limit=10)[0].attempt_count, 1)

    def test_stream_statuses_are_edges_not_repeated_spam(self) -> None:
        first = self.queue.enqueue_stream_status_if_changed(
            stream_id=STREAM_ID,
            occurred_at="2026-07-15T03:00:00.000Z",
            status="unavailable",
            code="entity_unavailable",
            now=100,
        )
        repeated = self.queue.enqueue_stream_status_if_changed(
            stream_id=STREAM_ID,
            occurred_at="2026-07-15T03:05:00.000Z",
            status="unavailable",
            code="entity_unavailable",
            now=400,
        )
        recovery = self.queue.enqueue_stream_status_if_changed(
            stream_id=STREAM_ID,
            occurred_at="2026-07-15T03:10:00.000Z",
            status="ok",
            now=700,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(repeated)
        self.assertIsNotNone(recovery)
        self.assertEqual(self.queue.stats().pending, 2)

    def test_restart_reset_reemits_current_health_after_configuration_repair(self) -> None:
        first = self.queue.enqueue_stream_status_if_changed(
            stream_id=STREAM_ID,
            occurred_at="2026-07-15T03:00:00.000Z",
            status="unavailable",
            code="entity_unavailable",
            now=100,
        )
        self.queue.reset_health_edges()
        after_restart = self.queue.enqueue_stream_status_if_changed(
            stream_id=STREAM_ID,
            occurred_at="2026-07-15T03:05:00.000Z",
            status="unavailable",
            code="entity_unavailable",
            now=400,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(after_restart)

    def test_queue_limit_reserves_room_for_a_health_event(self) -> None:
        for index in range(5):
            self.queue.enqueue_sample(
                stream_id=STREAM_ID,
                observed_at=f"2026-07-15T03:{index:02d}:00.000Z",
                value=78.0 + index,
                unit="°F",
                now=100 + index,
            )
        expired, overflow = self.queue.enforce_limits(
            now=200, retention_days=30, max_items=3, reserve_items=1
        )
        self.assertEqual(expired, 0)
        self.assertEqual(overflow, 3)
        self.assertEqual(self.queue.stats().pending, 2)

    def test_full_queue_does_not_churn_without_a_new_drop(self) -> None:
        for index in range(3):
            self.queue.enqueue_bridge_status(
                occurred_at=f"2026-07-15T03:00:0{index}.000Z",
                status="ok",
                now=100 + index,
            )
        first = self.queue.enforce_limits(
            now=200, retention_days=30, max_items=3, reserve_items=1
        )
        second = self.queue.enforce_limits(
            now=201, retention_days=30, max_items=3, reserve_items=1
        )
        self.assertEqual(first, (0, 0))
        self.assertEqual(second, (0, 0))
        self.assertEqual(self.queue.stats().pending, 3)


if __name__ == "__main__":
    unittest.main()
