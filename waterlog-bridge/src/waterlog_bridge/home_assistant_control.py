"""Allowlisted Home Assistant switch control with mandatory readback."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
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
        confirmation_timeout_seconds: float = 15.0,
        confirmation_interval_seconds: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport or HttpTransport()
        self._base = core_api_url.rstrip("/")
        self._confirmation_timeout = confirmation_timeout_seconds
        self._confirmation_interval = confirmation_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep

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
                # TP-Link/Tapo control awaits a device write and then a delayed
                # device refresh, each of which may use HA's five-second timeout.
                timeout=max(15, self._timeout),
            )
        except TransportError as error:
            raise HomeAssistantControlError(
                "Home Assistant switch call was uncertain"
            ) from error
        if response.status not in {200, 201}:
            raise HomeAssistantControlError("Home Assistant rejected switch control")
        # HA can accept a switch call before its integration publishes the new
        # state. P316M readback has arrived just after five seconds in practice;
        # allow that delayed confirmation without resending the service call.
        deadline = self._monotonic() + self._confirmation_timeout
        while True:
            try:
                observed = self.state(entity_id)
            except HomeAssistantControlError:
                observed = "unavailable"
            if observed == state:
                return observed
            if self._monotonic() >= deadline:
                raise HomeAssistantControlError(
                    "Home Assistant switch confirmation timed out: "
                    f"{entity_id} expected {state}, last observed {observed}"
                )
            self._sleep(self._confirmation_interval)
