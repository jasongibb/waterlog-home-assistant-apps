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
from waterlog_bridge.ha_registry import discover_allowlisted

INSTALLATION = "10000000-0000-4000-8000-000000000001"


class FakeRegistryConnection:
    def __init__(self, entity_id: str) -> None:
        self.responses = iter(
            [
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_ok"}),
                json.dumps(
                    {
                        "success": True,
                        "result": [
                            {
                                "id": "registry-pump",
                                "platform": "tplink",
                                "config_entry_id": "config",
                                "device_id": "device-pump",
                                "unique_id": "unique-pump",
                                "entity_id": entity_id,
                                "name": "Pump",
                                "disabled_by": None,
                            }
                        ],
                    }
                ),
            ]
        )
        self.sent: list[str] = []
        self.closed = False

    def recv(self) -> str:
        return next(self.responses)

    def send(self, payload: str) -> None:
        self.sent.append(payload)

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
        )
        for row in store.inventory()
    )


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


if __name__ == "__main__":
    unittest.main()
