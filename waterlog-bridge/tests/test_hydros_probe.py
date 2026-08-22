from __future__ import annotations

import importlib.util
import io
import json
import unittest
from pathlib import Path

from waterlog_bridge.hydros import HydrosClient
from waterlog_bridge.models import HttpResponse


_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "hydros_probe.py"
_SPEC = importlib.util.spec_from_file_location("hydros_probe", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
hydros_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hydros_probe)


class ScriptedTransport:
    def __init__(self, responses: list[tuple[str, HttpResponse]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> HttpResponse:
        method, response = self._responses.pop(0)
        assert method == "GET"
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return response

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict, timeout: int
    ) -> HttpResponse:
        method, response = self._responses.pop(0)
        assert method == "POST"
        self.calls.append({"method": "POST", "url": url, "headers": headers})
        return response


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode("utf-8"))


class HydrosProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        HydrosClient._pinned_header_form = None

    def test_probe_prints_device_identity_input_entries_and_stanzas(self) -> None:
        transport = ScriptedTransport(
            [
                (
                    "GET",
                    json_response(
                        200,
                        {
                            "deviceId": "a0b765227294",
                            "friendlyName": "Display Tank Controller",
                            "type": "X4",
                            "owner": "user@example.com",
                        },
                    ),
                ),
                (
                    "POST",
                    json_response(
                        200,
                        {
                            "pollUrl": "https://api.coralvuehydros.com/api/v1/device/state?id=abc",
                            "pollToken": "token-1",
                            "durationSeconds": 21600,
                            "pollIntervalSeconds": 30,
                            "expiresAt": "2026-08-21T18:00:00Z",
                        },
                    ),
                ),
                (
                    "GET",
                    json_response(
                        200,
                        {
                            "millis": 1_700_000_000_000,
                            "mode": "Normal",
                            "Input": {
                                "pH": {"probeValue": 8.012, "probeRawValue": -308},
                                "Salinity": {"probeValue": 36.148},
                            },
                        },
                    ),
                ),
            ]
        )
        out = io.StringIO()
        exit_code = hydros_probe.run_probe(
            provider_key="provider-secret",
            device_key="device-secret-key-12345",
            base_url="https://api.coralvuehydros.com",
            transport=transport,
            out=out,
        )
        output = out.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertIn("a0b765227294", output)
        self.assertIn("Display Tank Controller", output)
        self.assertIn("Auth header form accepted: plain", output)
        self.assertIn("pH", output)
        self.assertIn("Salinity", output)
        self.assertIn("a0b765227294/pH", output)
        self.assertIn("a0b765227294/Salinity", output)
        self.assertIn('unit: "TODO"', output)
        # Neither secret is ever printed.
        self.assertNotIn("provider-secret", output)
        self.assertNotIn("device-secret-key-12345", output)

    def test_probe_reports_no_state_without_crashing(self) -> None:
        transport = ScriptedTransport(
            [
                ("GET", json_response(200, {"deviceId": "a0b765227294", "friendlyName": "Tank", "type": "X4"})),
                (
                    "POST",
                    json_response(
                        200,
                        {
                            "pollUrl": "https://api.coralvuehydros.com/api/v1/device/state?id=abc",
                            "pollToken": "token-1",
                            "durationSeconds": 21600,
                            "pollIntervalSeconds": 30,
                            "expiresAt": "2026-08-21T18:00:00Z",
                        },
                    ),
                ),
                ("GET", json_response(404, {"error": "No state available"})),
            ]
        )
        out = io.StringIO()
        exit_code = hydros_probe.run_probe(
            provider_key="provider-secret",
            device_key="device-secret-key-12345",
            transport=transport,
            out=out,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("No state is cached", out.getvalue())

    def test_probe_reports_authentication_failure_at_device_lookup(self) -> None:
        transport = ScriptedTransport(
            [
                ("GET", json_response(401, {"error": "Unauthorized"})),
                ("GET", json_response(401, {"error": "Unauthorized"})),
            ]
        )
        out = io.StringIO()
        exit_code = hydros_probe.run_probe(
            provider_key="provider-secret",
            device_key="device-secret-key-12345",
            transport=transport,
            out=out,
        )
        self.assertEqual(exit_code, 2)
        self.assertIn("rejected the provider key", out.getvalue())

    def test_probe_distinguishes_a_rejected_poll_token_from_bad_keys(self) -> None:
        session = {
            "pollUrl": "https://api.coralvuehydros.com/api/v1/device/state?id=abc",
            "pollToken": "token-1",
            "durationSeconds": 21600,
            "pollIntervalSeconds": 30,
            "expiresAt": "2099-01-01T00:00:00Z",
        }
        transport = ScriptedTransport(
            [
                ("GET", json_response(200, {"deviceId": "a0b765227294", "friendlyName": "Lagoon", "type": "Launch"})),
                ("POST", json_response(200, session)),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
                ("POST", json_response(200, dict(session, pollToken="token-2"))),
                ("GET", json_response(401, {"error": "invalid or expired poll token"})),
            ]
        )
        out = io.StringIO()
        exit_code = hydros_probe.run_probe(
            provider_key="provider-secret",
            device_key="device-secret-key-12345",
            transport=transport,
            out=out,
        )
        self.assertEqual(exit_code, 4)
        self.assertIn("poll token was rejected", out.getvalue())
        self.assertIn("not a key-copy problem", out.getvalue())

    def test_probe_reports_transport_failure(self) -> None:
        transport = ScriptedTransport([("GET", json_response(500, {"error": "boom"}))])
        out = io.StringIO()
        exit_code = hydros_probe.run_probe(
            provider_key="provider-secret",
            device_key="device-secret-key-12345",
            transport=transport,
            out=out,
        )
        self.assertEqual(exit_code, 3)
        self.assertIn("Could not reach HYDROS", out.getvalue())

    def test_parse_args_reads_flags(self) -> None:
        args = hydros_probe.parse_args(
            ["--provider-key", "p", "--device-key", "d", "--base-url", "https://example.test"]
        )
        self.assertEqual(args.provider_key, "p")
        self.assertEqual(args.device_key, "d")
        self.assertEqual(args.base_url, "https://example.test")

    def test_main_requires_keys(self) -> None:
        exit_code = hydros_probe.main([])
        self.assertEqual(exit_code, 2)

    def test_slugify_produces_a_valid_device_name(self) -> None:
        self.assertEqual(hydros_probe._slugify("Display Tank Controller!"), "display-tank-controller")
        self.assertTrue(hydros_probe._DEVICE_NAME.fullmatch(hydros_probe._slugify("!!!")))


if __name__ == "__main__":
    unittest.main()
