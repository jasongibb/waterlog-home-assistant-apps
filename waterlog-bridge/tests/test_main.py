from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

# The startup test does not make a Home Assistant connection. The dependency is
# optional in the minimal test runtime, so scope a small import stub to this
# module import rather than leaving it in sys.modules for other tests.
try:
    import websocket  # type: ignore[import-not-found]  # noqa: F401
except ModuleNotFoundError:
    sys.modules["websocket"] = types.ModuleType("websocket")
    try:
        from waterlog_bridge.main import _run_telemetry
    finally:
        del sys.modules["websocket"]
else:
    from waterlog_bridge.main import _run_telemetry
from waterlog_bridge.models import BridgeConfig


class TelemetryStartupTests(unittest.TestCase):
    def test_telemetry_queue_is_created_and_closed_by_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = BridgeConfig(
                waterlog_url="https://waterlog.fish",
                credential="wlb_test-credential-never-log",
                streams=(),
            )
            stop_event = threading.Event()
            stop_event.set()
            failures: list[BaseException] = []

            def run_worker() -> None:
                try:
                    _run_telemetry(config, Path(directory), "supervisor-token", stop_event)
                except BaseException as error:
                    failures.append(error)

            worker = threading.Thread(
                target=run_worker,
            )
            worker.start()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            with sqlite3.connect(Path(directory, "waterlog-bridge.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
