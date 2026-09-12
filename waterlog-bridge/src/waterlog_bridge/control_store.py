"""Durable tank-mode state, intentionally separate from telemetry retention."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable


class ControlStoreError(RuntimeError):
    pass


class ControlStore:
    def __init__(self, path: str | Path, installation_id: str) -> None:
        self.path = Path(path)
        self._lock_file = open(self.path.with_suffix(".lock"), "a+b")  # noqa: SIM115
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock_file.close()
            raise ControlStoreError(
                "another control executor owns this installation"
            ) from error
        try:
            self.db = sqlite3.connect(
                self.path, isolation_level="DEFERRED", check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            pinned = self.get_meta("installation_id")
            if pinned is None:
                self.set_meta("installation_id", installation_id)
                self.db.commit()
            elif pinned != installation_id:
                raise ControlStoreError(
                    "installation identity does not match durable control state"
                )
        except Exception:
            self.close()
            raise

    def _migrate(self) -> None:
        self.db.executescript(
            """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS installed_config(
          revision INTEGER PRIMARY KEY,payload TEXT NOT NULL,installed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS commands(
          command_id TEXT PRIMARY KEY,tank_id TEXT NOT NULL,revision INTEGER NOT NULL,
          mode TEXT NOT NULL,status TEXT NOT NULL,accepted_at REAL NOT NULL,payload TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS commands_tank_revision ON commands(tank_id,revision);
        CREATE TABLE IF NOT EXISTS sessions(
          tank_id TEXT PRIMARY KEY,session_command_id TEXT NOT NULL,generation INTEGER NOT NULL,
          command_id TEXT NOT NULL,revision INTEGER NOT NULL,config_revision INTEGER NOT NULL,
          mode TEXT NOT NULL,phase TEXT NOT NULL,last_utc REAL NOT NULL,error_code TEXT,
          last_report_sequence INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS obligations(
          tank_id TEXT NOT NULL,outlet_id TEXT NOT NULL,generation INTEGER NOT NULL,
          entity_id TEXT NOT NULL,registry_entry_id TEXT NOT NULL,
          platform TEXT NOT NULL DEFAULT '',config_entry_id TEXT NOT NULL DEFAULT '',
          device_id TEXT NOT NULL DEFAULT '',unique_id TEXT NOT NULL DEFAULT '',
          baseline TEXT NOT NULL,
          expected_state TEXT NOT NULL,deadline REAL NOT NULL,owns_restore INTEGER NOT NULL,
          requires_pump_outlet_id TEXT,intent TEXT,result TEXT,error_code TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0,retry_at REAL,
          active_in_next INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(tank_id,outlet_id),FOREIGN KEY(tank_id) REFERENCES sessions(tank_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS report_outbox(
          report_id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at REAL NOT NULL,
          sequence INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS recovery_tanks(
          tank_id TEXT PRIMARY KEY,recognized_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS local_inventory(
          outlet_id TEXT PRIMARY KEY,registry_entry_id TEXT NOT NULL UNIQUE,
          platform TEXT NOT NULL,config_entry_id TEXT NOT NULL,device_id TEXT NOT NULL,
          unique_id TEXT NOT NULL,entity_id TEXT NOT NULL,label TEXT NOT NULL,
          observed_at REAL NOT NULL,position INTEGER NOT NULL DEFAULT 0,
          configured_entity_id TEXT NOT NULL DEFAULT '');
        COMMIT;
        """
        )
        self._ensure_column("obligations", "platform", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            "obligations", "config_entry_id", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column("obligations", "device_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("obligations", "unique_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            "obligations", "active_in_next", "INTEGER NOT NULL DEFAULT 1"
        )
        self._ensure_column(
            "report_outbox", "sequence", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "local_inventory", "position", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "local_inventory", "configured_entity_id", "TEXT NOT NULL DEFAULT ''"
        )
        self._backfill_report_sequences()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.db.commit()

    def _backfill_report_sequences(self) -> None:
        rows = list(
            self.db.execute(
                "SELECT report_id,payload FROM report_outbox WHERE sequence=0"
            )
        )
        if not rows:
            return
        updates: list[tuple[int, str]] = []
        for row in rows:
            sequence = json.loads(str(row["payload"])).get("reportSequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ControlStoreError("durable report outbox has an invalid sequence")
            updates.append((sequence, str(row["report_id"])))
        with self.db:
            self.db.executemany(
                "UPDATE report_outbox SET sequence=? WHERE report_id=?",
                updates,
            )

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        if not self._lock_file.closed:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def install_config(
        self, revision: int, payload: dict[str, Any], now: float
    ) -> None:
        with self.db:
            current = int(self.get_meta("config_revision") or 0)
            if revision < current:
                return
            self.db.execute(
                "INSERT OR REPLACE INTO installed_config VALUES(?,?,?)",
                (revision, json.dumps(payload, separators=(",", ":")), now),
            )
            self.set_meta("config_revision", str(revision))

    def config_revision(self) -> int:
        return int(self.get_meta("config_revision") or 0)

    def accepted(self, command_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            is not None
        )

    def record_command(
        self,
        *,
        command_id: str,
        tank_id: str,
        revision: int,
        mode: str,
        status: str,
        now: float,
        payload: object,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO commands VALUES(?,?,?,?,?,?,?)",
                (
                    command_id,
                    tank_id,
                    revision,
                    mode,
                    status,
                    now,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

    def command_status(self, command_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def set_command_status(self, command_id: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE commands SET status=? WHERE command_id=?",
                (status, command_id),
            )

    def begin_session(
        self,
        *,
        tank_id: str,
        session_command_id: str,
        command_id: str,
        revision: int,
        config_revision: int,
        mode: str,
        now: float,
        obligations: Iterable[dict[str, Any]],
        phase: str = "starting",
    ) -> None:
        """Write the complete baseline/deadline plan before the first HA call."""
        items = list(obligations)
        with self.db:
            generation_row = self.db.execute(
                "SELECT generation FROM sessions WHERE tank_id=?", (tank_id,)
            ).fetchone()
            generation = (int(generation_row[0]) + 1) if generation_row else 1
            self.db.execute(
                "INSERT OR REPLACE INTO commands VALUES(?,?,?,?,?,?,?)",
                (
                    command_id,
                    tank_id,
                    revision,
                    mode,
                    "accepted",
                    now,
                    json.dumps(items, separators=(",", ":")),
                ),
            )
            self.db.execute(
                """INSERT OR REPLACE INTO sessions(
              tank_id,session_command_id,generation,command_id,revision,config_revision,
              mode,phase,last_utc,error_code,last_report_sequence)
              VALUES(?,?,?,?,?,?,?,?,?,NULL,0)""",
                (
                    tank_id,
                    session_command_id,
                    generation,
                    command_id,
                    revision,
                    config_revision,
                    mode,
                    phase,
                    now,
                ),
            )
            self.db.execute("DELETE FROM obligations WHERE tank_id=?", (tank_id,))
            for item in items:
                self.db.execute(
                    """INSERT INTO obligations(tank_id,outlet_id,generation,entity_id,registry_entry_id,
                  platform,config_entry_id,device_id,unique_id,baseline,expected_state,deadline,
                  owns_restore,requires_pump_outlet_id,active_in_next)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tank_id,
                        item["outlet_id"],
                        generation,
                        item["entity_id"],
                        item["registry_entry_id"],
                        item["platform"],
                        item["config_entry_id"],
                        item["device_id"],
                        item["unique_id"],
                        item["baseline"],
                        "off",
                        item["deadline"],
                        1 if item["baseline"] == "on" else 0,
                        item.get("requires_pump_outlet_id"),
                        1 if item.get("active_in_next", True) else 0,
                    ),
                )

    def transition_session_command(
        self,
        tank_id: str,
        *,
        command_id: str,
        revision: int,
        config_revision: int,
        mode: str,
        phase: str,
        error: str | None,
        now: float,
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE sessions SET command_id=?,revision=?,config_revision=?,mode=?,
                phase=?,error_code=?,last_utc=? WHERE tank_id=?""",
                (
                    command_id,
                    revision,
                    config_revision,
                    mode,
                    phase,
                    error,
                    now,
                    tank_id,
                ),
            )

    def remove_inactive_obligations(self, tank_id: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM obligations WHERE tank_id=? AND active_in_next=0",
                (tank_id,),
            )

    def update_obligation_route(
        self, tank_id: str, outlet_id: str, entity_id: str
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE obligations SET entity_id=? WHERE tank_id=? AND outlet_id=?",
                (entity_id, tank_id, outlet_id),
            )

    def touch_sessions(self, now: float) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET last_utc=? WHERE phase IN ('active','starting','switching','restoring')",
                (now,),
            )

    def mark_intent(self, tank_id: str, outlet_id: str, intent: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE obligations SET intent=?,result=NULL WHERE tank_id=? AND outlet_id=?",
                (intent, tank_id, outlet_id),
            )

    def mark_result(
        self, tank_id: str, outlet_id: str, result: str, error: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE obligations SET result=?,error_code=? WHERE tank_id=? AND outlet_id=?",
                (result, error, tank_id, outlet_id),
            )

    def retry_obligation(
        self,
        tank_id: str,
        outlet_id: str,
        *,
        error: str,
        retry_count: int,
        retry_at: float,
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE obligations SET error_code=?,retry_count=?,retry_at=?
                WHERE tank_id=? AND outlet_id=?""",
                (error, retry_count, retry_at, tank_id, outlet_id),
            )

    def set_session(
        self,
        tank_id: str,
        phase: str,
        *,
        mode: str | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET phase=?,mode=COALESCE(?,mode),error_code=?,last_utc=COALESCE(?,last_utc) WHERE tank_id=?",
                (phase, mode, error, now, tank_id),
            )

    def sessions(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM sessions ORDER BY tank_id"))

    def obligations(self, tank_id: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM obligations WHERE tank_id=? ORDER BY outlet_id",
                (tank_id,),
            )
        )

    def remove_session(self, tank_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE tank_id=?", (tank_id,))

    def queue_report(self, payload: dict[str, Any], now: float) -> str:
        report_id = str(uuid.uuid4())
        with self.db:
            sequence = int(self.get_meta("report_sequence") or 0) + 1
            payload = {"reportId": report_id, "reportSequence": sequence, **payload}
            self.set_meta("report_sequence", str(sequence))
            tank_id = payload.get("tankId")
            if isinstance(tank_id, str):
                self.db.execute(
                    "UPDATE sessions SET last_report_sequence=? WHERE tank_id=?",
                    (sequence, tank_id),
                )
            self.db.execute(
                "INSERT INTO report_outbox(report_id,payload,created_at,sequence) VALUES(?,?,?,?)",
                (
                    report_id,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    sequence,
                ),
            )
        return report_id

    def reports(self, limit: int = 32) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM report_outbox ORDER BY sequence,report_id LIMIT ?",
                (limit,),
            )
        ]

    def acknowledge_reports(self, ids: Iterable[str]) -> None:
        values = list(ids)
        if not values:
            return
        with self.db:
            self.db.executemany(
                "DELETE FROM report_outbox WHERE report_id=?",
                ((value,) for value in values),
            )

    def report_sequence(self) -> int:
        return int(self.get_meta("report_sequence") or 0)

    def advance_report_sequence(self, floor: int) -> None:
        """Resume a replacement store above the authenticated cloud high-water mark."""
        if floor < 0:
            raise ControlStoreError("report sequence floor cannot be negative")
        with self.db:
            if floor > self.report_sequence():
                self.set_meta("report_sequence", str(floor))

    def replace_recovery_tanks(self, tank_ids: Iterable[str], now: float) -> None:
        """Persist the server-authoritative set of tanks awaiting reconciliation."""
        values = sorted(set(tank_ids))
        with self.db:
            if values:
                placeholders = ",".join("?" for _ in values)
                self.db.execute(
                    f"DELETE FROM recovery_tanks WHERE tank_id NOT IN ({placeholders})",
                    values,
                )
                self.db.executemany(
                    "INSERT INTO recovery_tanks(tank_id,recognized_at) VALUES(?,?) "
                    "ON CONFLICT(tank_id) DO NOTHING",
                    ((tank_id, now) for tank_id in values),
                )
            else:
                self.db.execute("DELETE FROM recovery_tanks")
            self.set_meta("recovery_required", "1" if values else "0")

    def recovery_tanks(self) -> set[str]:
        return {
            str(row[0])
            for row in self.db.execute("SELECT tank_id FROM recovery_tanks")
        }

    def recovery_required(self) -> bool:
        return self.get_meta("recovery_required") == "1" or bool(
            self.recovery_tanks()
        )

    def resolve_recovery_tank(self, tank_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM recovery_tanks WHERE tank_id=?", (tank_id,))
            if not self.recovery_tanks():
                self.set_meta("recovery_required", "0")

    def replace_inventory(self, outlets: Iterable[Any], now: float) -> None:
        items = list(outlets)
        with self.db:
            self.db.execute("DELETE FROM local_inventory")
            self.db.executemany(
                """INSERT INTO local_inventory(
                outlet_id,registry_entry_id,platform,config_entry_id,device_id,
                unique_id,entity_id,label,observed_at,position,configured_entity_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        item.outlet_id,
                        item.registry_entry_id,
                        item.platform,
                        item.config_entry_id,
                        item.device_id,
                        item.unique_id,
                        item.entity_id,
                        item.label,
                        now,
                        position,
                        item.configured_entity_id or item.entity_id,
                    )
                    for position, item in enumerate(items)
                ),
            )

    def inventory(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM local_inventory ORDER BY position"))

    def backup_health_check(self) -> None:
        result = self.db.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise ControlStoreError("durable control store failed integrity check")
