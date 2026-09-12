"""Allowlisted Home Assistant switch control with mandatory readback."""

from __future__ import annotations

import json
from urllib.parse import quote

from .http import HttpTransport, TransportError


class HomeAssistantControlError(ConnectionError):
    pass


class HomeAssistantControl:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: int = 5,
        transport: HttpTransport | None = None,
        core_api_url: str = "http://supervisor/core/api",
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport or HttpTransport()
        self._base = core_api_url.rstrip("/")

    def state(self, entity_id: str) -> str:
        try:
            response = self._transport.get(
                f"{self._base}/states/{quote(entity_id,safe='.')}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except TransportError as error:
            raise HomeAssistantControlError("Home Assistant is unreachable") from error
        if response.status in {401, 403}:
            raise HomeAssistantControlError("Home Assistant rejected its local token")
        if response.status == 404:
            return "unknown"
        if response.status != 200:
            return "unavailable"
        try:
            value = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "unknown"
        state = value.get("state") if isinstance(value, dict) else None
        return state if state in {"on", "off"} else "unavailable"

    def set_state(self, entity_id: str, state: str) -> str:
        if state not in {"on", "off"} or not entity_id.startswith("switch."):
            raise ValueError("only explicit individual switch on/off is allowed")
        service = "turn_on" if state == "on" else "turn_off"
        try:
            response = self._transport.post_json(
                f"{self._base}/services/switch/{service}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                payload={"entity_id": entity_id},
                timeout=self._timeout,
            )
        except TransportError as error:
            raise HomeAssistantControlError(
                "Home Assistant switch call was uncertain"
            ) from error
        if response.status not in {200, 201}:
            raise HomeAssistantControlError("Home Assistant rejected switch control")
        return self.state(entity_id)
