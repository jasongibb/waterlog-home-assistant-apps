from __future__ import annotations

import json
import unittest

from waterlog_bridge.home_assistant_control import (
    HomeAssistantControl,
    HomeAssistantControlError,
)
from waterlog_bridge.http import TransportError
from waterlog_bridge.models import HttpResponse


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode())


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SequenceTransport:
    def __init__(self, reads: list[HttpResponse | Exception]) -> None:
        self.reads = reads
        self.posts: list[dict[str, object]] = []
        self.get_timeouts: list[int] = []
        self.post_timeouts: list[int] = []

    def post_json(self, url, *, headers, payload, timeout):  # noqa: ANN001
        self.posts.append(payload)
        self.post_timeouts.append(timeout)
        return response(200, [])

    def get(self, url, *, headers, timeout):  # noqa: ANN001
        self.get_timeouts.append(timeout)
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HomeAssistantControlTests(unittest.TestCase):
    def test_accepted_write_waits_for_delayed_state_confirmation(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [
                response(200, {"state": "on"}),
                response(200, {"state": "on"}),
                response(200, {"state": "off"}),
            ]
        )
        client = HomeAssistantControl(
            "supervisor-secret",
            transport=transport,
            confirmation_timeout_seconds=1,
            confirmation_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(client.set_state("switch.heater", "off"), "off")
        self.assertEqual(transport.posts, [{"entity_id": "switch.heater"}])
        self.assertEqual(transport.post_timeouts, [15])
        self.assertEqual(transport.get_timeouts, [5, 5, 5])
        self.assertEqual(clock.now, 0.2)

    def test_accepted_write_retries_transient_state_read_failure(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [TransportError("temporary"), response(200, {"state": "off"})]
        )
        client = HomeAssistantControl(
            "supervisor-secret",
            transport=transport,
            confirmation_timeout_seconds=1,
            confirmation_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(client.set_state("switch.heater", "off"), "off")
        self.assertEqual(clock.now, 0.1)

    def test_accepted_write_stops_when_confirmation_deadline_expires(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [
                response(200, {"state": "on"}),
                response(200, {"state": "on"}),
                response(200, {"state": "on"}),
            ]
        )
        client = HomeAssistantControl(
            "supervisor-secret",
            transport=transport,
            confirmation_timeout_seconds=0.2,
            confirmation_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        with self.assertRaisesRegex(
            HomeAssistantControlError, "confirmation timed out"
        ):
            client.set_state("switch.heater", "off")

        self.assertEqual(transport.posts, [{"entity_id": "switch.heater"}])
        self.assertAlmostEqual(clock.now, 0.2)

    def test_larger_request_timeout_applies_to_service_and_state_calls(self) -> None:
        transport = SequenceTransport([response(200, {"state": "off"})])
        client = HomeAssistantControl(
            "supervisor-secret",
            timeout_seconds=21,
            transport=transport,
        )

        self.assertEqual(client.set_state("switch.heater", "off"), "off")
        self.assertEqual(transport.post_timeouts, [21])
        self.assertEqual(transport.get_timeouts, [21])


if __name__ == "__main__":
    unittest.main()
