"""Read-only client for the CoralVue HYDROS public REST API.

See ``docs/hydros-connector-design.md`` sections 4.2-4.4 for the full design
this module implements: session lifecycle, auth header form resolution, and
reading extraction from the device state document.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .http import HttpResponse, HttpTransport, TransportError
from .models import EntityReading, HydrosDeviceConfig, HydrosStreamConfig
from .sources import SourceAuthenticationError, SourceTransportError


class HydrosAuthenticationError(SourceAuthenticationError):
    pass


class HydrosPollTokenError(HydrosAuthenticationError):
    """A freshly minted poll token was rejected — the provider/device key
    pair itself was accepted at session start, so this is a session/JWT
    fault, not a credential-copy problem."""


class HydrosTransportError(SourceTransportError):
    pass


#: Precedence order used to pick the default numeric field from an Input
#: entry when a stream does not configure an explicit ``value_field``.
NUMERIC_FIELD_PRECEDENCE: tuple[str, ...] = (
    "probeValue",
    "senseValue",
    "value",
    "i10Value",
)

_USER_AGENT = "Waterlog-Hydros-Bridge/0.1"
_RENEW_MARGIN_SECONDS = 1_800
#: After an authentication rejection, no session start or poll is attempted
#: for this long. Session starts are budgeted at 5/hour per device by the
#: vendor; without this cooldown a persistent auth fault would consume one
#: start per 300s poll cycle (12/hour) and drown the real 401 in 429 noise.
_AUTH_COOLDOWN_SECONDS = 2_700


class _DeviceSession:
    __slots__ = ("poll_url", "poll_token", "expires_at")

    def __init__(self, poll_url: str, poll_token: str, expires_at: float) -> None:
        self.poll_url = poll_url
        self.poll_token = poll_token
        self.expires_at = expires_at


class HydrosClient:
    """Poll HYDROS device state through the CoralVue public REST API.

    One state document is fetched per device per poll cycle
    (``begin_cycle()`` / cached in ``read_input``), regardless of how many
    streams map to that device.
    """

    #: The auth header form ("plain" or "bearer") that a 200 response proved
    #: correct. This is a *class* attribute deliberately: the gateway's
    #: accepted header form is a property of the HYDROS deployment, not of
    #: any one device or client instance, so it is resolved once per process.
    _pinned_header_form: str | None = None

    def __init__(
        self,
        provider_key: str,
        devices: tuple[HydrosDeviceConfig, ...],
        *,
        timeout_seconds: int,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.coralvuehydros.com",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._provider_key = provider_key
        self._devices = {device.name: device for device in devices}
        self._timeout = timeout_seconds
        self._transport = transport or HttpTransport()
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._sessions: dict[str, _DeviceSession] = {}
        self._cycle_docs: dict[str, dict[str, Any] | None] = {}
        self._auth_cooldown_until: dict[str, float] = {}

    @property
    def pinned_header_form(self) -> str | None:
        return type(self)._pinned_header_form

    def begin_cycle(self) -> None:
        """Clear the per-cycle state-document cache. Call once per poll pass."""

        self._cycle_docs = {}

    def read_input(self, stream: HydrosStreamConfig) -> EntityReading:
        device = self._devices.get(stream.device)
        if device is None:
            return EntityReading(status="error", code="hydros_device_not_configured")

        if self._clock() < self._auth_cooldown_until.get(stream.device, 0.0):
            # A recent authentication rejection is cooling down; do not spend
            # the device's 5/hour session-start budget re-proving it.
            return EntityReading(status="error", code="hydros_auth_rejected")

        if stream.device not in self._cycle_docs:
            self._cycle_docs[stream.device] = self.fetch_state(device)
        document = self._cycle_docs[stream.device]

        if document is None:
            return EntityReading(status="unavailable", code="hydros_no_state")
        return _extract_reading(document, stream)

    def fetch_state(self, device: HydrosDeviceConfig) -> dict[str, Any] | None:
        """Fetch this device's state document, handling the session lifecycle.

        Returns ``None`` for the documented "no state cached" (404) signal.
        Raises :class:`HydrosAuthenticationError` / :class:`HydrosTransportError`
        for every other failure; an authentication rejection also arms a
        per-device cooldown that ``read_input`` honors, so a persistent auth
        fault costs at most one session-start burst per cooldown window
        instead of one per poll cycle. Not cycle-cached; callers that want
        the once-per-cycle guarantee should go through :meth:`read_input`.
        """

        try:
            self._ensure_session(device)
            document = self._poll(device, allow_retry=True)
        except HydrosAuthenticationError:
            self._auth_cooldown_until[device.name] = (
                self._clock() + _AUTH_COOLDOWN_SECONDS
            )
            raise
        self._auth_cooldown_until.pop(device.name, None)
        return document

    def get_device(self, device: HydrosDeviceConfig) -> dict[str, Any]:
        """``GET /api/v1/device`` — informational device identity.

        Not used by the bridge's own poll loop; exposed for the activation
        probe script (``scripts/hydros_probe.py``).
        """

        url = f"{self._base_url}/api/v1/device"
        response = self._request_with_auth_fallback("GET", url, device)
        return _parse_json_object(response.body, "HYDROS device response")

    # -- session lifecycle -------------------------------------------------

    def _ensure_session(self, device: HydrosDeviceConfig) -> None:
        session = self._sessions.get(device.name)
        if session is not None and session.expires_at - self._clock() >= _RENEW_MARGIN_SECONDS:
            return
        self._start_session(device)

    def _start_session(self, device: HydrosDeviceConfig) -> None:
        url = f"{self._base_url}/api/v1/device/state/session"
        response = self._request_with_auth_fallback("POST", url, device, payload={})
        payload = _parse_json_object(response.body, "HYDROS session response")
        self._sessions[device.name] = _parse_session(payload)

    def _poll(self, device: HydrosDeviceConfig, *, allow_retry: bool) -> dict[str, Any] | None:
        session = self._sessions[device.name]
        try:
            response = self._transport.get(
                session.poll_url,
                headers={
                    "Authorization": f"Bearer {session.poll_token}",
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                },
                timeout=self._timeout,
            )
        except TransportError as error:
            raise HydrosTransportError("HYDROS state poll request failed") from error

        if response.status == 200:
            return _parse_json_object(response.body, "HYDROS state document")
        if response.status == 404:
            return None
        if response.status == 401:
            if not allow_retry:
                raise HydrosPollTokenError("HYDROS poll token was rejected twice")
            del self._sessions[device.name]
            self._start_session(device)
            return self._poll(device, allow_retry=False)
        raise HydrosTransportError(f"HYDROS state poll returned HTTP {response.status}")

    # -- auth header form resolution ---------------------------------------

    def _auth_header_value(self, device: HydrosDeviceConfig, form: str) -> str:
        raw = f"{self._provider_key}:{device.device_key}"
        return raw if form == "plain" else f"Bearer {raw}"

    def _request_with_auth_fallback(
        self,
        method: str,
        url: str,
        device: HydrosDeviceConfig,
        *,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Send a device-key-authenticated request, resolving the header form.

        The spec's prose shows ``{provider_key}:{device_key}`` while its
        security scheme is declared bearer. Plain form is tried first; a 401
        (only when no form has been pinned yet for this process) retries
        once with a ``Bearer`` prefix, and whichever form succeeds is pinned
        on the class for the rest of the process lifetime.
        """

        already_pinned = type(self)._pinned_header_form is not None
        first_form = type(self)._pinned_header_form or "plain"
        response = self._send(method, url, device, first_form, payload)
        if response.status == 200:
            type(self)._pinned_header_form = first_form
            return response

        if response.status == 401 and not already_pinned:
            second_form = "bearer" if first_form == "plain" else "plain"
            response = self._send(method, url, device, second_form, payload)
            if response.status == 200:
                type(self)._pinned_header_form = second_form
                return response

        if response.status in {401, 403}:
            raise HydrosAuthenticationError("HYDROS rejected the provider/device key")
        raise HydrosTransportError(f"HYDROS request returned HTTP {response.status}")

    def _send(
        self,
        method: str,
        url: str,
        device: HydrosDeviceConfig,
        form: str,
        payload: dict[str, Any] | None,
    ) -> HttpResponse:
        headers = {
            "Authorization": self._auth_header_value(device, form),
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        try:
            if method == "GET":
                return self._transport.get(url, headers=headers, timeout=self._timeout)
            return self._transport.post_json(
                url, headers=headers, payload=payload or {}, timeout=self._timeout
            )
        except TransportError as error:
            raise HydrosTransportError(f"HYDROS {method} request failed") from error


def _parse_json_object(body: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HydrosTransportError(f"{label} was invalid JSON") from error
    if not isinstance(payload, dict):
        raise HydrosTransportError(f"{label} was not a JSON object")
    return payload


def _parse_session(payload: dict[str, Any]) -> _DeviceSession:
    poll_url = payload.get("pollUrl")
    poll_token = payload.get("pollToken")
    if not isinstance(poll_url, str) or not poll_url:
        raise HydrosTransportError("HYDROS session response is missing pollUrl")
    if not isinstance(poll_token, str) or not poll_token:
        raise HydrosTransportError("HYDROS session response is missing pollToken")
    expires_at = _parse_expires_at(payload.get("expiresAt"))
    return _DeviceSession(poll_url=poll_url, poll_token=poll_token, expires_at=expires_at)


def _parse_expires_at(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    raise HydrosTransportError("HYDROS session response has an invalid expiresAt")


def _extract_reading(document: dict[str, Any], stream: HydrosStreamConfig) -> EntityReading:
    inputs = document.get("Input")
    entry = inputs.get(stream.input_name) if isinstance(inputs, dict) else None
    if not isinstance(entry, dict):
        return EntityReading(status="error", code="input_not_found")

    if stream.value_field is not None:
        if stream.value_field not in entry:
            return EntityReading(status="error", code="no_numeric_field")
        field_name: str | None = stream.value_field
    else:
        field_name = next(
            (name for name in NUMERIC_FIELD_PRECEDENCE if name in entry), None
        )
        if field_name is None:
            return EntityReading(status="error", code="no_numeric_field")

    raw_value = entry[field_name]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return EntityReading(status="error", code="non_numeric_value")
    if not math.isfinite(raw_value):
        return EntityReading(status="error", code="non_finite_value")

    return EntityReading(
        status="ok",
        value=float(raw_value),
        unit=stream.unit,
        source_updated_at=_source_updated_at(entry, document),
    )


def _source_updated_at(entry: dict[str, Any], document: dict[str, Any]) -> str | None:
    entry_time = entry.get("time")
    if _is_plausible_positive_number(entry_time):
        formatted = _format_epoch(float(entry_time), is_seconds=True)
        if formatted is not None:
            return formatted
    millis = document.get("millis")
    if _is_plausible_positive_number(millis):
        formatted = _format_epoch(float(millis), is_seconds=False)
        if formatted is not None:
            return formatted
    return None


def _is_plausible_positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _format_epoch(value: float, *, is_seconds: bool) -> str | None:
    seconds = value if is_seconds else value / 1000.0
    try:
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None
