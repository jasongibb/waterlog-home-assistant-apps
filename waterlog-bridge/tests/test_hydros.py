from __future__ import annotations

import json
import math
import unittest

from waterlog_bridge.hydros import (
    HydrosAuthenticationError,
    HydrosClient,
    HydrosPollTokenError,
    HydrosTransportError,
)
from waterlog_bridge.models import HttpResponse, HydrosDeviceConfig, HydrosStreamConfig


DEVICE = HydrosDeviceConfig(name="lagoon", device_key="a" * 20)
SESSION_URL = "https://api.coralvuehydros.com/api/v1/device/state/session"
POLL_URL = "https://api.coralvuehydros.com/api/v1/device/state?id=abc-123"


class ScriptedTransport:
    """Consumes canned (method, HttpResponse) pairs in call order."""

    def __init__(self, responses: list[tuple[str, HttpResponse]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> HttpResponse:
        method, response = self._responses.pop(0)
        assert method == "GET", f"expected a GET, script said {method}"
        self.calls.append({"method": "GET", "url": url, "headers": headers, "timeout": timeout})
        return response

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict, timeout: int
    ) -> HttpResponse:
        method, response = self._responses.pop(0)
        assert method == "POST", f"expected a POST, script said {method}"
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        return response


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status,
        {},
        json.dumps(payload, allow_nan=True).encode("utf-8"),
    )


def session_payload(
    *, poll_url: str = POLL_URL, poll_token: str = "token-1", expires_at: str = "2026-08-21T12:00:00Z"
) -> dict:
    return {
        "pollUrl": poll_url,
        "pollToken": poll_token,
        "durationSeconds": 21600,
        "pollIntervalSeconds": 30,
        "expiresAt": expires_at,
    }


def state_payload(*, inputs: dict, millis: int = 1_700_000_000_000) -> dict:
    return {"millis": millis, "mode": "Normal", "Input": inputs}


def make_client(
    responses: list[tuple[str, HttpResponse]],
    *,
    now: float = 1_700_000_000.0,
    devices: tuple[HydrosDeviceConfig, ...] = (DEVICE,),
) -> tuple[HydrosClient, ScriptedTransport]:
    transport = ScriptedTransport(responses)
    client = HydrosClient(
        "provider-key-123",
        devices,
        timeout_seconds=10,
        transport=transport,
        clock=lambda: now,
    )
    return client, transport


class HydrosClientTests(unittest.TestCase):
    def setUp(self) -> None:
        # The pinned header form is a shared class attribute (by design: the
        # gateway's accepted form is process-wide, not per instance/device).
        # Reset it so tests do not leak state into one another.
        HydrosClient._pinned_header_form = None

    def stream(self, **overrides) -> HydrosStreamConfig:
        defaults = dict(
            stream_id="11111111-1111-4111-8111-111111111111",
            device="lagoon",
            input_name="pH",
            unit="pH",
        )
        defaults.update(overrides)
        return HydrosStreamConfig(**defaults)

    # -- session start + poll happy path ------------------------------------

    def test_session_start_and_poll_happy_path(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(inputs={"pH": {"probeValue": 8.012, "probeRawValue": -308}}),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())

        self.assertEqual(reading.status, "ok")
        self.assertEqual(reading.value, 8.012)
        self.assertEqual(reading.unit, "pH")

        post_call = transport.calls[0]
        self.assertEqual(post_call["url"], SESSION_URL)
        self.assertEqual(post_call["headers"]["Authorization"], f"provider-key-123:{DEVICE.device_key}")

        get_call = transport.calls[1]
        self.assertEqual(get_call["url"], POLL_URL)
        self.assertEqual(get_call["headers"]["Authorization"], "Bearer token-1")

    # -- header-form fallback pinning ----------------------------------------

    def test_header_form_falls_back_to_bearer_and_pins_it(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(401, {"error": "Unauthorized"})),
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": 8.0}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())

        self.assertEqual(reading.status, "ok")
        self.assertEqual(HydrosClient._pinned_header_form, "bearer")
        first_post, second_post = transport.calls[0], transport.calls[1]
        self.assertEqual(first_post["headers"]["Authorization"], f"provider-key-123:{DEVICE.device_key}")
        self.assertEqual(
            second_post["headers"]["Authorization"], f"Bearer provider-key-123:{DEVICE.device_key}"
        )

        # A second client in the same process reuses the pinned form and does
        # not repeat the failed plain-form attempt.
        client2, transport2 = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": 8.1}}))),
            ]
        )
        client2.begin_cycle()
        client2.read_input(self.stream())
        self.assertEqual(len(transport2.calls), 2)
        self.assertEqual(
            transport2.calls[0]["headers"]["Authorization"],
            f"Bearer provider-key-123:{DEVICE.device_key}",
        )

    def test_both_header_forms_rejected_raises_authentication_error(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(401, {"error": "Unauthorized"})),
                ("POST", json_response(401, {"error": "Unauthorized"})),
            ]
        )
        client.begin_cycle()
        with self.assertRaises(HydrosAuthenticationError):
            client.read_input(self.stream())

    def test_session_start_forbidden_raises_authentication_error(self) -> None:
        client, transport = make_client([("POST", json_response(403, {"error": "Forbidden"}))])
        client.begin_cycle()
        with self.assertRaises(HydrosAuthenticationError):
            client.read_input(self.stream())
        # 403 does not trigger the plain/bearer fallback (only 401 does).
        self.assertEqual(len(transport.calls), 1)

    # -- poll 401: one refresh, then fail -------------------------------------

    def test_poll_401_refreshes_session_once_then_raises(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(200, session_payload(poll_token="token-1"))),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
                ("POST", json_response(200, session_payload(poll_token="token-2"))),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
            ]
        )
        client.begin_cycle()
        with self.assertRaises(HydrosAuthenticationError):
            client.read_input(self.stream())
        self.assertEqual(len(transport.calls), 4)

    def test_poll_token_rejected_twice_is_a_distinct_poll_token_error(self) -> None:
        # The key pair was accepted (sessions started); only the minted poll
        # token failed. Callers such as the probe script distinguish this from
        # a credential-copy problem.
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload(poll_token="token-1"))),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
                ("POST", json_response(200, session_payload(poll_token="token-2"))),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
            ]
        )
        client.begin_cycle()
        with self.assertRaises(HydrosPollTokenError):
            client.read_input(self.stream())
        self.assertTrue(issubclass(HydrosPollTokenError, HydrosAuthenticationError))

    # -- auth cooldown: session starts stay inside the 5/hour budget ----------

    def test_auth_rejection_arms_cooldown_then_recovers_after_expiry(self) -> None:
        clock = {"now": 1_700_000_000.0}
        transport = ScriptedTransport(
            [
                ("POST", json_response(401, {"error": "Unauthorized"})),
                ("POST", json_response(401, {"error": "Unauthorized"})),
                # After the cooldown expires: fresh session start + good poll.
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200, state_payload(inputs={"pH": {"probeValue": 8.0}})
                    ),
                ),
            ]
        )
        client = HydrosClient(
            "provider-key-123",
            (DEVICE,),
            timeout_seconds=10,
            transport=transport,
            clock=lambda: clock["now"],
        )
        client.begin_cycle()
        with self.assertRaises(HydrosAuthenticationError):
            client.read_input(self.stream())
        self.assertEqual(len(transport.calls), 2)

        # Cooling down: later cycles surface the auth fault with no network
        # traffic, so the device's 5/hour session-start budget is untouched.
        for cycle in range(3):
            clock["now"] += 300.0
            client.begin_cycle()
            reading = client.read_input(self.stream())
            self.assertEqual(reading.status, "error")
            self.assertEqual(reading.code, "hydros_auth_rejected")
        self.assertEqual(len(transport.calls), 2)

        # Past the cooldown window the client tries again and recovers.
        clock["now"] += 2_700.0
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "ok")
        self.assertEqual(len(transport.calls), 4)

    # -- poll 404: hydros_no_state for every stream of the device -------------

    def test_poll_404_marks_every_stream_of_device_unavailable_and_fetches_once(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(404, {"error": "No state available"})),
            ]
        )
        client.begin_cycle()
        reading_a = client.read_input(self.stream(stream_id="11111111-1111-4111-8111-111111111111", input_name="pH"))
        reading_b = client.read_input(
            self.stream(stream_id="22222222-2222-4222-8222-222222222222", input_name="Salinity")
        )

        for reading in (reading_a, reading_b):
            self.assertEqual(reading.status, "unavailable")
            self.assertEqual(reading.code, "hydros_no_state")
        # One session start + one poll for both streams on the same device.
        self.assertEqual(len(transport.calls), 2)

    # -- 429 / 5xx -> transport error -----------------------------------------

    def test_poll_429_is_a_transport_error(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(429, {"error": "Rate limit exceeded"})),
            ]
        )
        client.begin_cycle()
        with self.assertRaises(HydrosTransportError):
            client.read_input(self.stream())

    def test_poll_5xx_is_a_transport_error(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(500, {"error": "boom"})),
            ]
        )
        client.begin_cycle()
        with self.assertRaises(HydrosTransportError):
            client.read_input(self.stream())

    def test_session_start_5xx_is_a_transport_error(self) -> None:
        client, _ = make_client([("POST", json_response(503, {"error": "boom"}))])
        client.begin_cycle()
        with self.assertRaises(HydrosTransportError):
            client.read_input(self.stream())

    # -- field selection precedence and value_field override ------------------

    def test_default_field_precedence_prefers_probe_value(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(
                            inputs={
                                "pH": {
                                    "probeValue": 8.012,
                                    "senseValue": 8.5,
                                    "value": 8.9,
                                    "i10Value": 90,
                                }
                            }
                        ),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.value, 8.012)

    def test_default_field_falls_back_through_precedence(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"Alky Alkalinity": {"value": 7.236}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream(input_name="Alky Alkalinity"))
        self.assertEqual(reading.value, 7.236)

    def test_value_field_override_selects_configured_field(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(inputs={"pH": {"probeValue": 8.012, "probeRawValue": -308}}),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream(value_field="probeRawValue"))
        self.assertEqual(reading.value, -308)

    def test_value_field_override_missing_is_no_numeric_field(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": 8.012}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream(value_field="notAField"))
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "no_numeric_field")

    def test_input_not_found(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"Salinity": {"probeValue": 36.1}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream(input_name="pH"))
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "input_not_found")

    def test_no_numeric_field_when_entry_has_none_of_the_precedence_fields(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"alert": "low battery"}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "no_numeric_field")

    # -- bool / NaN / missing rejection codes ---------------------------------

    def test_boolean_value_is_rejected_as_non_numeric(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": True}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "non_numeric_value")

    def test_nan_value_is_rejected_as_non_finite(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": math.nan}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "non_finite_value")

    def test_infinite_value_is_rejected_as_non_finite(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": math.inf}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "non_finite_value")

    def test_string_value_is_rejected_as_non_numeric(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": "8.0"}}))),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "non_numeric_value")

    # -- time-vs-millis source_updated_at --------------------------------------

    def test_entry_time_seconds_is_preferred_and_normalized(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(
                            inputs={"Alky Alkalinity": {"value": 7.236, "time": 1767014490}},
                            millis=1_700_000_000_000,
                        ),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream(input_name="Alky Alkalinity"))
        self.assertEqual(reading.source_updated_at, "2025-12-29T13:21:30.000Z")

    def test_falls_back_to_document_millis_when_entry_has_no_time(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(inputs={"pH": {"probeValue": 8.0}}, millis=1_578_947_568_911),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.source_updated_at, "2020-01-13T20:32:48.911Z")

    def test_implausible_entry_time_falls_back_to_millis(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(
                            inputs={"pH": {"probeValue": 8.0, "time": -5}},
                            millis=1_578_947_568_911,
                        ),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertEqual(reading.source_updated_at, "2020-01-13T20:32:48.911Z")

    def test_no_time_or_millis_yields_none(self) -> None:
        client, _ = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, {"mode": "Normal", "Input": {"pH": {"probeValue": 8.0}}})),
            ]
        )
        client.begin_cycle()
        reading = client.read_input(self.stream())
        self.assertIsNone(reading.source_updated_at)

    # -- single-fetch-per-cycle caching ----------------------------------------

    def test_two_streams_on_same_device_share_one_fetch_per_cycle(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(200, session_payload())),
                (
                    "GET",
                    json_response(
                        200,
                        state_payload(
                            inputs={"pH": {"probeValue": 8.0}, "Salinity": {"probeValue": 36.0}}
                        ),
                    ),
                ),
            ]
        )
        client.begin_cycle()
        reading_a = client.read_input(self.stream(input_name="pH"))
        reading_b = client.read_input(
            self.stream(stream_id="22222222-2222-4222-8222-222222222222", input_name="Salinity")
        )
        self.assertEqual(reading_a.status, "ok")
        self.assertEqual(reading_b.status, "ok")
        self.assertEqual(len(transport.calls), 2)  # one POST + one GET total

    def test_begin_cycle_clears_the_cache_for_a_new_fetch(self) -> None:
        client, transport = make_client(
            [
                ("POST", json_response(200, session_payload())),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": 8.0}}))),
                ("GET", json_response(200, state_payload(inputs={"pH": {"probeValue": 8.2}}))),
            ]
        )
        client.begin_cycle()
        first = client.read_input(self.stream())
        self.assertEqual(first.value, 8.0)
        self.assertEqual(len(transport.calls), 2)

        client.begin_cycle()
        second = client.read_input(self.stream())
        self.assertEqual(second.value, 8.2)
        # Session is still valid, so only a new poll GET is made, not a new session.
        self.assertEqual(len(transport.calls), 3)

    def test_unconfigured_device_is_a_configuration_error_not_a_network_call(self) -> None:
        client, transport = make_client([])
        client.begin_cycle()
        reading = client.read_input(self.stream(device="not-configured"))
        self.assertEqual(reading.status, "error")
        self.assertEqual(reading.code, "hydros_device_not_configured")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
