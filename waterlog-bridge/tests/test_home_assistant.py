from __future__ import annotations

import json
import unittest

from waterlog_bridge.home_assistant import (
    HomeAssistantAuthenticationError,
    HomeAssistantClient,
    HomeAssistantTransportError,
)
from waterlog_bridge.models import HttpResponse, StreamConfig


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> HttpResponse:
        self.calls.append((url, headers, timeout))
        return self.response


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode())


class HomeAssistantClientTests(unittest.TestCase):
    stream = StreamConfig(
        "11111111-1111-4111-8111-111111111111",
        "sensor.reefato_temperature",
    )

    def client(
        self, http_response: HttpResponse
    ) -> tuple[HomeAssistantClient, FakeTransport]:
        transport = FakeTransport(http_response)
        return (
            HomeAssistantClient(
                "supervisor-secret", timeout_seconds=12, transport=transport
            ),
            transport,
        )

    def test_reads_finite_numeric_state_and_entity_unit(self) -> None:
        client, transport = self.client(
            response(
                200,
                {
                    "state": "78.25",
                    "attributes": {"unit_of_measurement": "°F"},
                    "last_updated": "2026-07-14T21:00:02-06:00",
                },
            )
        )
        reading = client.read_entity(self.stream)
        self.assertEqual(reading.status, "ok")
        self.assertEqual(reading.value, 78.25)
        self.assertEqual(reading.unit, "°F")
        self.assertEqual(reading.source_updated_at, "2026-07-15T03:00:02.000Z")
        self.assertEqual(
            transport.calls[0][1]["Authorization"], "Bearer supervisor-secret"
        )
        self.assertTrue(transport.calls[0][0].endswith("sensor.reefato_temperature"))

    def test_unavailable_never_becomes_zero(self) -> None:
        client, _ = self.client(
            response(200, {"state": "unavailable", "attributes": {}})
        )
        reading = client.read_entity(self.stream)
        self.assertEqual(reading.status, "unavailable")
        self.assertIsNone(reading.value)

    def test_non_finite_state_is_an_error(self) -> None:
        client, _ = self.client(
            response(200, {"state": "NaN", "attributes": {"unit_of_measurement": "°F"}})
        )
        self.assertEqual(client.read_entity(self.stream).code, "non_finite_state")

    def test_unit_override_is_used(self) -> None:
        client, _ = self.client(response(200, {"state": "25.5", "attributes": {}}))
        reading = client.read_entity(
            StreamConfig(self.stream.stream_id, self.stream.entity_id, "°C")
        )
        self.assertEqual(reading.unit, "°C")

    def test_invalid_last_updated_falls_back_to_last_changed(self) -> None:
        client, _ = self.client(
            response(
                200,
                {
                    "state": "25.5",
                    "attributes": {"unit_of_measurement": "°C"},
                    "last_updated": "not-a-time",
                    "last_changed": "2026-07-15T03:00:01Z",
                },
            )
        )
        self.assertEqual(
            client.read_entity(self.stream).source_updated_at,
            "2026-07-15T03:00:01.000Z",
        )

    def test_authentication_rejection_is_distinct(self) -> None:
        client, _ = self.client(response(401, {"message": "no"}))
        with self.assertRaises(HomeAssistantAuthenticationError):
            client.read_entity(self.stream)

    def test_server_failure_is_retryable_transport_failure(self) -> None:
        client, _ = self.client(response(503, {"message": "later"}))
        with self.assertRaises(HomeAssistantTransportError):
            client.read_entity(self.stream)


if __name__ == "__main__":
    unittest.main()
