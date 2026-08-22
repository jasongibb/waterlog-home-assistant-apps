from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from waterlog_bridge.hydros import HydrosAuthenticationError, HydrosTransportError
from waterlog_bridge.home_assistant import (
    HomeAssistantAuthenticationError,
    HomeAssistantTransportError,
)
from waterlog_bridge.models import (
    BridgeConfig,
    EntityReading,
    HydrosStreamConfig,
    StreamConfig,
    UploadOutcome,
)
from waterlog_bridge.queue import DurableQueue
from waterlog_bridge.service import BridgeService, home_assistant_group, hydros_group


FIRST_STREAM = "11111111-1111-4111-8111-111111111111"
SECOND_STREAM = "22222222-2222-4222-8222-222222222222"
HYDROS_STREAM = "33333333-3333-4333-8333-333333333333"


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


class FailingHomeAssistant:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def read_entity(self, stream: StreamConfig) -> EntityReading:
        raise self._error


class FakeHydros:
    def __init__(self) -> None:
        self.begin_cycle_calls = 0

    def begin_cycle(self) -> None:
        self.begin_cycle_calls += 1

    def read_input(self, stream: HydrosStreamConfig) -> EntityReading:
        return EntityReading(status="ok", value=8.1, unit="pH")


class FailingHydros:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.begin_cycle_calls = 0

    def begin_cycle(self) -> None:
        self.begin_cycle_calls += 1

    def read_input(self, stream: HydrosStreamConfig) -> EntityReading:
        raise self._error


class FakeUploader:
    def upload_once(self, *, now: float) -> UploadOutcome:
        return UploadOutcome()


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.queue = DurableQueue(Path(self.directory.name, "queue.sqlite3"))
        self.streams = (
            StreamConfig(FIRST_STREAM, "sensor.good_temperature"),
            StreamConfig(SECOND_STREAM, "sensor.unavailable_temperature"),
        )
        self.config = BridgeConfig(
            waterlog_url="https://waterlog.fish",
            credential="wlb_test-credential-never-log",
            streams=self.streams,
        )
        self.service = BridgeService(
            self.config,
            self.queue,
            (home_assistant_group(FakeHomeAssistant(), self.streams),),
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
            (home_assistant_group(FakeHomeAssistant(), self.streams),),
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

    # -- HA-only transport/auth failures keep the pre-multi-source codes -----

    def test_ha_only_full_transport_outage_keeps_legacy_code(self) -> None:
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(
                    FailingHomeAssistant(HomeAssistantTransportError("down")), self.streams
                ),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        items = self.queue.due_items(now=100, limit=20)
        bridge_statuses = [
            item.payload for item in items if item.payload.get("kind") == "bridge"
        ]
        self.assertTrue(
            any(status.get("code") == "home_assistant_unreachable" for status in bridge_statuses)
        )
        stream_statuses = [
            item.payload for item in items if item.payload.get("kind") == "stream"
        ]
        self.assertTrue(
            all(status.get("code") == "home_assistant_unreachable" for status in stream_statuses)
        )

    def test_ha_only_auth_failure_uses_legacy_stream_code(self) -> None:
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(
                    FailingHomeAssistant(HomeAssistantAuthenticationError("bad token")),
                    self.streams,
                ),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        items = self.queue.due_items(now=100, limit=20)
        stream_statuses = [
            item.payload for item in items if item.payload.get("kind") == "stream"
        ]
        self.assertTrue(
            all(
                status.get("code") == "home_assistant_auth_rejected"
                for status in stream_statuses
            )
        )

    # -- multi-group behavior --------------------------------------------------

    def test_hydros_auth_failure_does_not_stop_home_assistant_group(self) -> None:
        hydros_stream = HydrosStreamConfig(
            stream_id=HYDROS_STREAM, device="lagoon", input_name="pH", unit="pH"
        )
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(FakeHomeAssistant(), self.streams),
                hydros_group(
                    FailingHydros(HydrosAuthenticationError("bad key")), (hydros_stream,)
                ),
            ),
            FakeUploader(),
        )
        sample_count, unhealthy_count = service.poll_once(now=100)
        self.assertEqual(sample_count, 1)  # the HA "good" stream still sampled
        items = self.queue.due_items(now=100, limit=20)
        stream_statuses = {
            item.payload.get("streamId"): item.payload
            for item in items
            if item.payload.get("kind") == "stream"
        }
        self.assertEqual(stream_statuses[FIRST_STREAM]["status"], "ok")
        self.assertEqual(stream_statuses[HYDROS_STREAM]["code"], "hydros_auth_rejected")

        bridge_statuses = [
            item.payload for item in items if item.payload.get("kind") == "bridge"
        ]
        # Two groups configured; this is not the "HA-only sole group" case, so
        # the generic partial-failure code is used, not an HA-specific one.
        self.assertTrue(
            any(status.get("code") == "partial_source_failure" for status in bridge_statuses)
        )

    def test_hydros_transport_failure_uses_hydros_unreachable_code(self) -> None:
        hydros_stream = HydrosStreamConfig(
            stream_id=HYDROS_STREAM, device="lagoon", input_name="pH", unit="pH"
        )
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(FakeHomeAssistant(), self.streams),
                hydros_group(
                    FailingHydros(HydrosTransportError("unreachable")), (hydros_stream,)
                ),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        items = self.queue.due_items(now=100, limit=20)
        stream_statuses = {
            item.payload.get("streamId"): item.payload
            for item in items
            if item.payload.get("kind") == "stream"
        }
        self.assertEqual(stream_statuses[HYDROS_STREAM]["code"], "hydros_unreachable")

    def test_hydros_only_full_outage_uses_generic_unreachable_code(self) -> None:
        hydros_stream = HydrosStreamConfig(
            stream_id=HYDROS_STREAM, device="lagoon", input_name="pH", unit="pH"
        )
        hydros_only_config = BridgeConfig(
            waterlog_url=self.config.waterlog_url,
            credential=self.config.credential,
            streams=(),
        )
        service = BridgeService(
            hydros_only_config,
            self.queue,
            (
                hydros_group(
                    FailingHydros(HydrosTransportError("unreachable")), (hydros_stream,)
                ),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        items = self.queue.due_items(now=100, limit=20)
        bridge_statuses = [
            item.payload for item in items if item.payload.get("kind") == "bridge"
        ]
        # Hydros is the sole group here, but it is not Home Assistant, so the
        # legacy HA-specific full-outage code must not be reused.
        self.assertTrue(
            any(status.get("code") == "source_unreachable" for status in bridge_statuses)
        )

    def test_hydros_begin_cycle_is_called_once_per_poll(self) -> None:
        hydros_stream = HydrosStreamConfig(
            stream_id=HYDROS_STREAM, device="lagoon", input_name="pH", unit="pH"
        )
        hydros_client = FakeHydros()
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(FakeHomeAssistant(), self.streams),
                hydros_group(hydros_client, (hydros_stream,)),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        service.poll_once(now=400)
        self.assertEqual(hydros_client.begin_cycle_calls, 2)

    def test_all_groups_healthy_reports_ok_heartbeat(self) -> None:
        hydros_stream = HydrosStreamConfig(
            stream_id=HYDROS_STREAM, device="lagoon", input_name="pH", unit="pH"
        )
        service = BridgeService(
            self.config,
            self.queue,
            (
                home_assistant_group(FakeHomeAssistant(), self.streams),
                hydros_group(FakeHydros(), (hydros_stream,)),
            ),
            FakeUploader(),
        )
        service.poll_once(now=100)
        items = self.queue.due_items(now=100, limit=20)
        bridge_statuses = [
            item.payload for item in items if item.payload.get("kind") == "bridge"
        ]
        self.assertTrue(any(status.get("status") == "ok" for status in bridge_statuses))


if __name__ == "__main__":
    unittest.main()
