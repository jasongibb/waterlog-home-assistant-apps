"""Read-only Home Assistant Core state client."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from urllib.parse import quote

from .http import HttpTransport, TransportError
from .models import EntityReading, StreamConfig


class HomeAssistantTransportError(ConnectionError):
    pass


class HomeAssistantAuthenticationError(PermissionError):
    pass


class HomeAssistantClient:
    """Poll numeric entities through Supervisor's Home Assistant API proxy."""

    def __init__(
        self,
        supervisor_token: str,
        *,
        timeout_seconds: int,
        transport: HttpTransport | None = None,
        core_api_url: str = "http://supervisor/core/api",
    ) -> None:
        self._token = supervisor_token
        self._timeout = timeout_seconds
        self._transport = transport or HttpTransport()
        self._core_api_url = core_api_url.rstrip("/")

    def read_entity(self, stream: StreamConfig) -> EntityReading:
        url = f"{self._core_api_url}/states/{quote(stream.entity_id, safe='.')}"
        try:
            response = self._transport.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "User-Agent": "Waterlog-Home-Assistant-Bridge/0.1",
                },
                timeout=self._timeout,
            )
        except TransportError as error:
            raise HomeAssistantTransportError("Home Assistant API is unreachable") from error

        if response.status in {401, 403}:
            raise HomeAssistantAuthenticationError(
                "Home Assistant rejected the Supervisor app token"
            )
        if response.status == 404:
            return EntityReading(status="error", code="entity_not_found")
        if response.status == 429 or response.status >= 500:
            raise HomeAssistantTransportError(
                f"Home Assistant API returned HTTP {response.status}"
            )
        if response.status != 200:
            return EntityReading(
                status="error", code=f"home_assistant_http_{response.status}"
            )

        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return EntityReading(status="error", code="invalid_home_assistant_json")
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return EntityReading(status="error", code="invalid_home_assistant_state")

        state = payload["state"].strip()
        normalized_state = state.lower()
        if normalized_state in {"unknown", "unavailable", "none", "null", ""}:
            return EntityReading(status="unavailable", code="entity_unavailable")
        try:
            value = float(state)
        except ValueError:
            return EntityReading(status="error", code="non_numeric_state")
        if not math.isfinite(value):
            return EntityReading(status="error", code="non_finite_state")

        unit = stream.unit_override
        if unit is None:
            attributes = payload.get("attributes")
            if isinstance(attributes, dict):
                raw_unit = attributes.get("unit_of_measurement")
                if isinstance(raw_unit, str) and raw_unit.strip():
                    unit = raw_unit.strip()
        if unit is None:
            return EntityReading(status="error", code="missing_unit")
        if len(unit) > 32:
            return EntityReading(status="error", code="invalid_unit")
        source_updated_at = _first_valid_timestamp(
            payload.get("last_updated"), payload.get("last_changed")
        )
        return EntityReading(
            status="ok",
            value=value,
            unit=unit,
            source_updated_at=source_updated_at,
        )


def _first_valid_timestamp(*values: object) -> str | None:
    for value in values:
        if not isinstance(value, str) or len(value) > 80:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return None
