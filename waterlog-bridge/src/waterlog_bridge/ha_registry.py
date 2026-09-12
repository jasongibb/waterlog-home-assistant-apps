"""Resolve explicitly configured switch hints to stable HA registry identity."""

from __future__ import annotations

import json
import uuid
from typing import Any

import websocket

from .control_models import RegistryOutlet


class RegistryError(RuntimeError):
    pass


def discover_allowlisted(
    token: str,
    entity_ids: tuple[str, ...],
    *,
    pinned: tuple[RegistryOutlet, ...] = (),
    url: str = "ws://supervisor/core/websocket",
) -> tuple[RegistryOutlet, ...]:
    """Use HA's registry command; never enumerate entities beyond local hints in output."""
    connection = websocket.create_connection(
        url, timeout=5, header=[f"Authorization: Bearer {token}"]
    )
    try:
        hello = json.loads(connection.recv())
        if hello.get("type") == "auth_required":
            connection.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(connection.recv()).get("type") != "auth_ok":
                raise RegistryError("Home Assistant registry authentication failed")
        connection.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
        response = json.loads(connection.recv())
        if not response.get("success") or not isinstance(response.get("result"), list):
            raise RegistryError("Home Assistant registry query failed")
        entries = {
            item.get("entity_id"): item
            for item in response["result"]
            if isinstance(item, dict)
        }
        outlets = []
        for entity_id in entity_ids:
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
                hinted_identity = (
                    str(hinted.get("id")),
                    str(hinted.get("platform")),
                    str(hinted.get("config_entry_id")),
                    str(hinted.get("device_id")),
                    str(hinted.get("unique_id")),
                )
                identity_matches = [
                    candidate
                    for candidate in pinned
                    if candidate.pinned_identity() == hinted_identity
                ]
                expected = (
                    identity_matches[0] if len(identity_matches) == 1 else None
                )
            if expected is None:
                item = entries.get(entity_id)
            else:
                matches = [
                    item
                    for item in entries.values()
                    if (
                        str(item.get("id")),
                        str(item.get("platform")),
                        str(item.get("config_entry_id")),
                        str(item.get("device_id")),
                        str(item.get("unique_id")),
                    )
                    == expected.pinned_identity()
                ]
                item = matches[0] if len(matches) == 1 else None
            if (
                not item
                or item.get("disabled_by") is not None
                or not all(
                    item.get(key)
                    for key in (
                        "id",
                        "platform",
                        "config_entry_id",
                        "device_id",
                        "unique_id",
                    )
                )
            ):
                raise RegistryError(f"allowlisted entity is unavailable: {entity_id}")
            # Deterministic local correlation ID; immutable registry identity is separately pinned.
            outlet_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "waterlog-ha:" + str(item["id"]))
            )
            outlets.append(
                RegistryOutlet(
                    outlet_id,
                    str(item["id"]),
                    str(item["platform"]),
                    str(item["config_entry_id"]),
                    str(item["device_id"]),
                    str(item["unique_id"]),
                    str(item["entity_id"]),
                    str(
                        item.get("name")
                        or item.get("original_name")
                        or item["entity_id"]
                    ),
                    entity_id,
                )
            )
        return tuple(outlets)
    finally:
        connection.close()
