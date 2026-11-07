"""Resolve configured switches and power strips to stable HA registry identity."""

from __future__ import annotations

import json
import uuid
from typing import Any

import websocket

from .control_models import RegistryOutlet


class RegistryError(RuntimeError):
    pass


MAX_OUTLETS = 128
_IDENTITY_KEYS = ("id", "platform", "config_entry_id", "device_id", "unique_id")


def _identity(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(str(item.get(key)) for key in _IDENTITY_KEYS)  # type: ignore[return-value]


def _outlet_id(registry_id: str) -> str:
    # Deterministic local correlation ID; immutable registry identity is separately pinned.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "waterlog-ha:" + registry_id))


def _device_name(device: dict[str, Any] | None) -> str | None:
    if not device:
        return None
    for key in ("name_by_user", "name"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120].strip()
    return None


def _strip_name(device_id: object, devices: dict[str, dict[str, Any]]) -> str | None:
    device = devices.get(str(device_id))
    if device is None:
        return None
    parent_id = device.get("via_device_id")
    if parent_id and str(parent_id) in devices:
        return _device_name(devices[str(parent_id)]) or _device_name(device)
    return _device_name(device)


def _outlet(
    item: dict[str, Any],
    devices: dict[str, dict[str, Any]],
    configured_entity_id: str | None,
    strip_name: str | None = None,
) -> RegistryOutlet:
    return RegistryOutlet(
        _outlet_id(str(item["id"])),
        str(item["id"]),
        str(item["platform"]),
        str(item["config_entry_id"]),
        str(item["device_id"]),
        str(item["unique_id"]),
        str(item["entity_id"]),
        str(item.get("name") or item.get("original_name") or item["entity_id"]),
        configured_entity_id,
        strip_name
        if strip_name is not None
        else _strip_name(item.get("device_id"), devices),
    )


def _usable(item: dict[str, Any] | None) -> bool:
    return bool(
        item
        and item.get("disabled_by") is None
        and all(item.get(key) for key in _IDENTITY_KEYS)
    )


class _Connection:
    """One authenticated HA websocket used for both registry reads."""

    def __init__(self, token: str, url: str) -> None:
        self._connection = websocket.create_connection(
            url, timeout=5, header=[f"Authorization: Bearer {token}"]
        )
        self._next_id = 1
        try:
            hello = json.loads(self._connection.recv())
            if hello.get("type") == "auth_required":
                self._connection.send(
                    json.dumps({"type": "auth", "access_token": token})
                )
                if json.loads(self._connection.recv()).get("type") != "auth_ok":
                    raise RegistryError("Home Assistant registry authentication failed")
        except Exception:
            self._connection.close()
            raise

    def list(self, command: str) -> list[dict[str, Any]]:
        message_id = self._next_id
        self._next_id += 1
        self._connection.send(json.dumps({"id": message_id, "type": command}))
        for _ in range(16):
            response = json.loads(self._connection.recv())
            if not isinstance(response, dict) or response.get("id") not in {
                None,
                message_id,
            }:
                continue
            if not response.get("success") or not isinstance(
                response.get("result"), list
            ):
                raise RegistryError("Home Assistant registry query failed")
            return [item for item in response["result"] if isinstance(item, dict)]
        raise RegistryError("Home Assistant registry query failed")

    def close(self) -> None:
        self._connection.close()


def _resolve_hint(
    entity_id: str,
    entries: dict[object, dict[str, Any]],
    pinned: tuple[RegistryOutlet, ...],
) -> dict[str, Any]:
    expected = next(
        (
            candidate
            for candidate in pinned
            if candidate.configured_entity_id == entity_id
        ),
        None,
    )
    if expected is None:
        expected = next(
            (candidate for candidate in pinned if candidate.entity_id == entity_id),
            None,
        )
    if expected is None and (hinted := entries.get(entity_id)) is not None:
        identity_matches = [
            candidate
            for candidate in pinned
            if candidate.pinned_identity() == _identity(hinted)
        ]
        expected = identity_matches[0] if len(identity_matches) == 1 else None
    if expected is None:
        item = entries.get(entity_id)
    else:
        matches = [
            item
            for item in entries.values()
            if _identity(item) == expected.pinned_identity()
        ]
        item = matches[0] if len(matches) == 1 else None
    if not _usable(item):
        raise RegistryError(f"allowlisted entity is unavailable: {entity_id}")
    return item  # type: ignore[return-value]


def _strip_device(value: str, devices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if value in devices:
        return devices[value]
    matches = [
        device
        for device in devices.values()
        if value in {device.get("name_by_user"), device.get("name")}
    ]
    if len(matches) != 1:
        raise RegistryError(
            f"control strip is {'ambiguous' if matches else 'not found'}: {value}"
        )
    return matches[0]


def discover_outlets(
    token: str,
    entity_ids: tuple[str, ...],
    strips: tuple[str, ...] = (),
    *,
    pinned: tuple[RegistryOutlet, ...] = (),
    url: str = "ws://supervisor/core/websocket",
) -> tuple[RegistryOutlet, ...]:
    """Resolve hints and listed strips; never report entities beyond them.

    A strip's outlets are its switch entities and those of its child devices
    that are enabled and are not configuration entities. Hinted outlets come
    first and keep their configured entity ID; strip outlets merge by registry
    entry ID.
    """
    connection = _Connection(token, url)
    try:
        entities = connection.list("config/entity_registry/list")
        try:
            device_rows = connection.list("config/device_registry/list")
        except Exception:
            # Strip names are optional for explicitly listed switches, which
            # keep working when the device registry cannot be read.
            if strips:
                raise
            device_rows = []
        devices = {str(item["id"]): item for item in device_rows if item.get("id")}
        entries = {item.get("entity_id"): item for item in entities}

        outlets: list[RegistryOutlet] = []
        seen: set[str] = set()
        for entity_id in entity_ids:
            item = _resolve_hint(entity_id, entries, pinned)
            if str(item["id"]) in seen:
                continue
            seen.add(str(item["id"]))
            outlets.append(_outlet(item, devices, entity_id))

        for value in strips:
            strip = _strip_device(value, devices)
            strip_id = str(strip["id"])
            member_devices = {strip_id} | {
                device_id
                for device_id, device in devices.items()
                if str(device.get("via_device_id") or "") == strip_id
            }
            name = _device_name(strip)
            members = sorted(
                (
                    item
                    for item in entities
                    if str(item.get("device_id")) in member_devices
                    and str(item.get("entity_id") or "").startswith("switch.")
                    and item.get("entity_category") is None
                    and _usable(item)
                ),
                key=lambda item: str(item.get("entity_id")),
            )
            for item in members:
                if str(item["id"]) in seen:
                    continue
                seen.add(str(item["id"]))
                outlets.append(_outlet(item, devices, None, name))

        if len(outlets) > MAX_OUTLETS:
            raise RegistryError(f"control outlets exceed the limit of {MAX_OUTLETS}")
        return tuple(outlets)
    finally:
        connection.close()


def discover_allowlisted(
    token: str,
    entity_ids: tuple[str, ...],
    *,
    pinned: tuple[RegistryOutlet, ...] = (),
    url: str = "ws://supervisor/core/websocket",
) -> tuple[RegistryOutlet, ...]:
    """Resolve only explicitly configured switch hints."""
    return discover_outlets(token, entity_ids, (), pinned=pinned, url=url)
