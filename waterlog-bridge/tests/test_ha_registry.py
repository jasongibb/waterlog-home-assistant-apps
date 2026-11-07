from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import websocket  # noqa: F401
except ModuleNotFoundError:
    sys.modules["websocket"] = types.SimpleNamespace(create_connection=None)

from waterlog_bridge.control_models import RegistryOutlet
from waterlog_bridge.control_store import ControlStore
from waterlog_bridge.ha_registry import (
    RegistryError,
    discover_allowlisted,
    discover_outlets,
)

INSTALLATION = "10000000-0000-4000-8000-000000000001"


def pump_entry(entity_id: str) -> dict[str, object]:
    return {
        "id": "registry-pump",
        "platform": "tplink",
        "config_entry_id": "config",
        "device_id": "device-pump",
        "unique_id": "unique-pump",
        "entity_id": entity_id,
        "name": "Pump",
        "disabled_by": None,
    }


class FakeRegistryConnection:
    """Answers each registry list command by its request type and id."""

    def __init__(
        self,
        entities: list[dict[str, object]] | str,
        devices: list[dict[str, object]] | None = None,
    ) -> None:
        if isinstance(entities, str):
            entities = [pump_entry(entities)]
        self.lists = {
            "config/entity_registry/list": entities,
            "config/device_registry/list": devices,
        }
        self.pending = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def recv(self) -> str:
        return self.pending.pop(0)

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if message.get("type") == "auth":
            return
        result = self.lists.get(str(message["type"]))
        if result is None:
            self.pending.append(
                json.dumps({"id": message["id"], "success": False, "error": {}})
            )
        else:
            self.pending.append(
                json.dumps({"id": message["id"], "success": True, "result": result})
            )

    def close(self) -> None:
        self.closed = True


def load_pins(store: ControlStore) -> tuple[RegistryOutlet, ...]:
    return tuple(
        RegistryOutlet(
            row["outlet_id"],
            row["registry_entry_id"],
            row["platform"],
            row["config_entry_id"],
            row["device_id"],
            row["unique_id"],
            row["entity_id"],
            row["label"],
            row["configured_entity_id"] or row["entity_id"],
            row["strip_name"],
        )
        for row in store.inventory()
    )


def entity(
    registry_id: str,
    entity_id: str,
    device_id: str,
    *,
    name: str | None = None,
    original_name: str | None = None,
    entity_category: str | None = None,
    disabled_by: str | None = None,
) -> dict[str, object]:
    return {
        "id": registry_id,
        "platform": "tplink",
        "config_entry_id": "config",
        "device_id": device_id,
        "unique_id": f"unique-{registry_id}",
        "entity_id": entity_id,
        "name": name,
        "original_name": original_name,
        "entity_category": entity_category,
        "disabled_by": disabled_by,
    }


STRIP_DEVICES = [
    {
        "id": "device-strip",
        "name": "Tapo P316M",
        "name_by_user": "Pea Puffer Power Strip",
        "via_device_id": None,
    },
    {
        "id": "device-child-1",
        "name": "Tapo P316M_1",
        "name_by_user": None,
        "via_device_id": "device-strip",
    },
    {"id": "device-other", "name": "Kitchen Plug", "name_by_user": None, "via_device_id": None},
]

STRIP_ENTITIES = [
    entity("registry-co2", "switch.co2", "device-strip", name="Pea Puffer CO2 Solenoid"),
    entity("registry-air", "switch.airstone", "device-strip", original_name="Tapo P316M_2"),
    entity("registry-child", "switch.child_outlet", "device-child-1", original_name="Outlet 1"),
    entity("registry-led", "switch.strip_led", "device-strip", entity_category="config"),
    entity("registry-auto-off", "switch.child_auto_off", "device-child-1", entity_category="config"),
    entity("registry-disabled", "switch.disabled", "device-strip", disabled_by="user"),
    entity("registry-sensor", "sensor.strip_power", "device-strip"),
    entity("registry-kitchen", "switch.kitchen", "device-other", name="Kitchen"),
]


class RegistryResolutionTests(unittest.TestCase):
    def test_rename_survives_consecutive_refreshes_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "control.sqlite3")
            connections = [
                FakeRegistryConnection("switch.pump"),
                FakeRegistryConnection("switch.renamed_pump"),
                FakeRegistryConnection("switch.renamed_pump"),
            ]
            with patch(
                "waterlog_bridge.ha_registry.websocket.create_connection",
                side_effect=connections,
            ):
                store = ControlStore(path, INSTALLATION)
                initial = discover_allowlisted("token", ("switch.pump",))
                store.replace_inventory(initial, 1.0)
                store.close()

                store = ControlStore(path, INSTALLATION)
                renamed = discover_allowlisted(
                    "token", ("switch.pump",), pinned=load_pins(store)
                )
                self.assertEqual(renamed[0].entity_id, "switch.renamed_pump")
                self.assertEqual(renamed[0].configured_entity_id, "switch.pump")
                store.replace_inventory(renamed, 2.0)
                store.close()

                store = ControlStore(path, INSTALLATION)
                after_restart = discover_allowlisted(
                    "token", ("switch.pump",), pinned=load_pins(store)
                )
                self.assertEqual(
                    after_restart[0].entity_id, "switch.renamed_pump"
                )
                self.assertEqual(
                    after_restart[0].pinned_identity(),
                    initial[0].pinned_identity(),
                )
                store.close()

            self.assertTrue(all(connection.closed for connection in connections))

    def discover(self, connection, *args, **kwargs):
        with patch(
            "waterlog_bridge.ha_registry.websocket.create_connection",
            return_value=connection,
        ):
            return discover_outlets("token", *args, **kwargs)

    def test_strip_lists_enabled_switches_with_strip_name_and_labels(self) -> None:
        connection = FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES)
        outlets = self.discover(connection, (), ("Pea Puffer Power Strip",))

        self.assertEqual(
            [(item.entity_id, item.label, item.strip_name) for item in outlets],
            [
                ("switch.airstone", "Tapo P316M_2", "Pea Puffer Power Strip"),
                ("switch.child_outlet", "Outlet 1", "Pea Puffer Power Strip"),
                ("switch.co2", "Pea Puffer CO2 Solenoid", "Pea Puffer Power Strip"),
            ],
        )
        # Both registry lists come over the one authenticated connection.
        self.assertEqual(
            [message["type"] for message in connection.sent],
            ["auth", "config/entity_registry/list", "config/device_registry/list"],
        )
        self.assertTrue(connection.closed)

    def test_strip_can_be_named_by_device_id(self) -> None:
        outlets = self.discover(
            FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES), (), ("device-strip",)
        )
        self.assertEqual(len(outlets), 3)

    def test_listed_entities_merge_with_strip_outlets_by_registry_entry(self) -> None:
        outlets = self.discover(
            FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES),
            ("switch.co2", "switch.kitchen"),
            ("Pea Puffer Power Strip",),
        )

        self.assertEqual(
            [item.entity_id for item in outlets],
            ["switch.co2", "switch.kitchen", "switch.airstone", "switch.child_outlet"],
        )
        by_entity = {item.entity_id: item for item in outlets}
        self.assertEqual(by_entity["switch.co2"].configured_entity_id, "switch.co2")
        self.assertEqual(by_entity["switch.kitchen"].strip_name, "Kitchen Plug")
        self.assertIsNone(by_entity["switch.airstone"].configured_entity_id)

    def test_child_device_entity_reports_parent_strip_name(self) -> None:
        outlets = self.discover(
            FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES),
            ("switch.child_outlet",),
        )
        self.assertEqual(outlets[0].strip_name, "Pea Puffer Power Strip")

    def test_missing_or_ambiguous_strip_is_an_error(self) -> None:
        with self.assertRaisesRegex(RegistryError, "not found"):
            self.discover(
                FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES), (), ("Garage",)
            )
        duplicate = [*STRIP_DEVICES, {**STRIP_DEVICES[2], "id": "device-other-2"}]
        with self.assertRaisesRegex(RegistryError, "ambiguous"):
            self.discover(
                FakeRegistryConnection(STRIP_ENTITIES, duplicate), (), ("Kitchen Plug",)
            )

    def test_listed_entities_work_without_device_registry(self) -> None:
        outlets = self.discover(FakeRegistryConnection(STRIP_ENTITIES, None), ("switch.co2",))
        self.assertEqual(outlets[0].entity_id, "switch.co2")
        self.assertIsNone(outlets[0].strip_name)
        with self.assertRaises(RegistryError):
            self.discover(FakeRegistryConnection(STRIP_ENTITIES, None), (), ("device-strip",))

    def test_strip_name_is_persisted_with_the_local_inventory(self) -> None:
        outlets = self.discover(
            FakeRegistryConnection(STRIP_ENTITIES, STRIP_DEVICES), (), ("device-strip",)
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory, "control.sqlite3"), INSTALLATION)
            try:
                store.replace_inventory(outlets, 1.0)
                self.assertEqual(
                    {pin.strip_name for pin in load_pins(store)},
                    {"Pea Puffer Power Strip"},
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
