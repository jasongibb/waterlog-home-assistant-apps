"""Bounded outbound Waterlog control exchange client."""

from __future__ import annotations

import json
from typing import Any

from .http import HttpTransport, TransportError


class ControlAuthenticationError(PermissionError):
    pass


class ControlProtocolError(ValueError):
    pass


class ControlClient:
    def __init__(
        self, url: str, credential: str, *, transport: HttpTransport | None = None
    ) -> None:
        self._url = f"{url.rstrip('/')}/api/control/bridge/exchange"
        self._credential = credential
        self._transport = transport or HttpTransport()

    def exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._transport.post_json(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._credential}",
                    "Accept": "application/json",
                },
                payload=payload,
                timeout=5,
            )
        except TransportError:
            raise
        if response.status in {401, 403}:
            raise ControlAuthenticationError("Waterlog rejected the control credential")
        if response.status != 200:
            raise TransportError(f"control exchange returned HTTP {response.status}")
        try:
            value = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ControlProtocolError("invalid control response") from error
        required = {
            "protocolVersion",
            "serverTime",
            "acknowledgedReportIds",
            "retryAfterSeconds",
            "configRevision",
            "configuration",
            "commands",
            "recoveryRequired",
            "recoveryTankIds",
            "reportSequenceFloor",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value["protocolVersion"] != 1
            or not isinstance(value["commands"], list)
            or len(value["commands"]) > 32
            or not isinstance(value["recoveryTankIds"], list)
            or len(value["recoveryTankIds"]) > 32
            or value["recoveryRequired"] != bool(value["recoveryTankIds"])
            or isinstance(value["reportSequenceFloor"], bool)
            or not isinstance(value["reportSequenceFloor"], int)
            or value["reportSequenceFloor"] < 0
        ):
            raise ControlProtocolError("invalid control response")
        return value
