from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from waterlog_bridge.http import TransportError
from waterlog_bridge.models import BridgeConfig, HttpResponse, StreamConfig
from waterlog_bridge.queue import DurableQueue
from waterlog_bridge.uploader import WaterlogUploader


STREAM_ID = "11111111-1111-4111-8111-111111111111"


class FakeTransport:
    def __init__(self, result: HttpResponse | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def post_json(self, url, *, headers, payload, timeout):  # noqa: ANN001
        self.calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def http_response(status: int, payload: object, headers=None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(payload).encode())


class UploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.queue = DurableQueue(Path(self.directory.name, "queue.sqlite3"))
        self.config = BridgeConfig(
            waterlog_url="https://waterlog.fish",
            credential="wlb_test-credential-never-log",
            streams=(StreamConfig(STREAM_ID, "sensor.temperature", "°F"),),
        )

    def tearDown(self) -> None:
        self.queue.close()
        self.directory.cleanup()

    def enqueue_pair(self) -> None:
        self.queue.enqueue_sample(
            stream_id=STREAM_ID,
            observed_at="2026-07-15T03:00:00.000Z",
            value=78.2,
            unit="°F",
            now=100,
        )
        self.queue.enqueue_bridge_status(
            occurred_at="2026-07-15T03:00:00.000Z", status="ok", now=100
        )

    def test_item_acceptance_and_duplicate_delete_only_acknowledged_rows(self) -> None:
        self.enqueue_pair()
        transport = FakeTransport(
            http_response(
                200,
                {
                    "samples": [{"index": 0, "status": "accepted"}],
                    "statuses": [{"index": 0, "status": "duplicate"}],
                },
            )
        )
        outcome = WaterlogUploader(
            self.config, self.queue, transport=transport, jitter=lambda a, b: a
        ).upload_once(now=100)
        self.assertEqual(outcome.acknowledged, 2)
        self.assertEqual(self.queue.stats().pending, 0)
        call = transport.calls[0]
        self.assertEqual(
            call["headers"]["Authorization"], "Bearer wlb_test-credential-never-log"
        )
        self.assertNotIn("sourceId", json.dumps(call["payload"]))

    def test_permanent_item_failure_is_quarantined_while_other_item_succeeds(self) -> None:
        self.enqueue_pair()
        transport = FakeTransport(
            http_response(
                200,
                {
                    "samples": [
                        {"index": 0, "status": "rejected", "code": "unknown_stream"}
                    ],
                    "statuses": [{"index": 0, "status": "accepted"}],
                },
            )
        )
        outcome = WaterlogUploader(
            self.config, self.queue, transport=transport, jitter=lambda a, b: a
        ).upload_once(now=100)
        self.assertEqual(outcome.acknowledged, 1)
        self.assertEqual(outcome.quarantined, 1)
        self.assertEqual(self.queue.stats().quarantined, 1)
        self.assertEqual(self.queue.stats().pending, 0)

    def test_missing_acknowledgement_remains_queued_with_backoff(self) -> None:
        self.enqueue_pair()
        transport = FakeTransport(
            http_response(
                200,
                {"samples": [{"index": 0, "status": "accepted"}], "statuses": []},
            )
        )
        outcome = WaterlogUploader(
            self.config, self.queue, transport=transport, jitter=lambda a, b: a
        ).upload_once(now=100)
        self.assertEqual(outcome.acknowledged, 1)
        self.assertEqual(outcome.retryable, 1)
        self.assertEqual(self.queue.stats().pending, 1)
        self.assertFalse(self.queue.has_due(now=123))
        self.assertTrue(self.queue.has_due(now=124))

    def test_network_and_rate_limit_failures_retry(self) -> None:
        self.enqueue_pair()
        network = WaterlogUploader(
            self.config,
            self.queue,
            transport=FakeTransport(TransportError("offline")),
            jitter=lambda a, b: a,
        ).upload_once(now=100)
        self.assertEqual(network.retryable, 2)
        self.assertEqual(network.retry_at, 124)

        rate_limited = WaterlogUploader(
            self.config,
            self.queue,
            transport=FakeTransport(http_response(429, {}, {"retry-after": "120"})),
            jitter=lambda a, b: a,
        ).upload_once(now=124)
        self.assertEqual(rate_limited.retry_at, 244)

    def test_authentication_failure_stops_upload_without_deleting_queue(self) -> None:
        self.enqueue_pair()
        outcome = WaterlogUploader(
            self.config,
            self.queue,
            transport=FakeTransport(http_response(401, {})),
        ).upload_once(now=100)
        self.assertTrue(outcome.auth_blocked)
        self.assertEqual(self.queue.stats().pending, 2)

    def test_unified_results_shape_is_supported(self) -> None:
        self.queue.enqueue_sample(
            stream_id=STREAM_ID,
            observed_at="2026-07-15T03:00:00.000Z",
            value=78.2,
            unit="°F",
            now=100,
        )
        item = self.queue.due_items(now=100, limit=1)[0]
        transport = FakeTransport(
            http_response(
                200,
                {
                    "results": [
                        {
                            "kind": "sample",
                            "clientSampleId": item.client_id,
                            "status": "accepted",
                        }
                    ]
                },
            )
        )
        outcome = WaterlogUploader(
            self.config, self.queue, transport=transport
        ).upload_once(now=100)
        self.assertEqual(outcome.acknowledged, 1)


if __name__ == "__main__":
    unittest.main()
