"""Durable SQLite outbox for samples and health events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from .models import QueueItem, QueueStats


class DurableQueue:
    """Append-before-send outbox with explicit acknowledgement and quarantine."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._database = sqlite3.connect(self.path, timeout=30)
        self._database.row_factory = sqlite3.Row
        self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute("PRAGMA synchronous=FULL")
        self._database.execute("PRAGMA busy_timeout=30000")
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('sample', 'status')),
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT,
                quarantined_at REAL,
                quarantine_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_due_idx
                ON outbox (next_attempt_at, id)
                WHERE quarantined_at IS NULL;
            CREATE TABLE IF NOT EXISTS health_state (
                scope_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
        self._database.commit()

    def enqueue_sample(
        self,
        *,
        stream_id: str,
        observed_at: str,
        source_updated_at: str | None = None,
        value: float,
        unit: str,
        now: float,
    ) -> str:
        client_id = str(uuid.uuid4())
        payload = {
            "clientSampleId": client_id,
            "streamId": stream_id,
            "observedAt": observed_at,
            "value": value,
            "unit": unit,
        }
        if source_updated_at is not None:
            payload["sourceUpdatedAt"] = source_updated_at
        with self._database:
            self._insert("sample", client_id, payload, now)
        return client_id

    def enqueue_bridge_status(
        self,
        *,
        occurred_at: str,
        status: Literal["ok", "unavailable", "error"],
        now: float,
        code: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        return self._enqueue_status(
            kind="bridge",
            stream_id=None,
            occurred_at=occurred_at,
            status=status,
            code=code,
            message=message,
            details=details,
            now=now,
        )

    def enqueue_stream_status_if_changed(
        self,
        *,
        stream_id: str,
        occurred_at: str,
        status: Literal["ok", "unavailable", "error"],
        now: float,
        code: str | None = None,
        message: str | None = None,
    ) -> str | None:
        """Persist stream status edges; valid samples provide ongoing OK freshness."""

        fingerprint = f"{status}\0{code or ''}"
        scope_key = f"stream:{stream_id}"
        with self._database:
            row = self._database.execute(
                "SELECT fingerprint FROM health_state WHERE scope_key = ?", (scope_key,)
            ).fetchone()
            if row is not None and row["fingerprint"] == fingerprint:
                return None
            self._database.execute(
                """
                INSERT INTO health_state (scope_key, fingerprint, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    updated_at = excluded.updated_at
                """,
                (scope_key, fingerprint, now),
            )
            return self._insert_status(
                kind="stream",
                stream_id=stream_id,
                occurred_at=occurred_at,
                status=status,
                code=code,
                message=message,
                details=None,
                now=now,
            )

    def _enqueue_status(
        self,
        *,
        kind: Literal["bridge", "stream"],
        stream_id: str | None,
        occurred_at: str,
        status: Literal["ok", "unavailable", "error"],
        code: str | None,
        message: str | None,
        details: dict[str, Any] | None,
        now: float,
    ) -> str:
        with self._database:
            return self._insert_status(
                kind=kind,
                stream_id=stream_id,
                occurred_at=occurred_at,
                status=status,
                code=code,
                message=message,
                details=details,
                now=now,
            )

    def _insert_status(
        self,
        *,
        kind: Literal["bridge", "stream"],
        stream_id: str | None,
        occurred_at: str,
        status: Literal["ok", "unavailable", "error"],
        code: str | None,
        message: str | None,
        details: dict[str, Any] | None,
        now: float,
    ) -> str:
        client_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "clientStatusId": client_id,
            "kind": kind,
            "occurredAt": occurred_at,
            "status": status,
        }
        if stream_id is not None:
            payload["streamId"] = stream_id
        if code:
            payload["code"] = code[:64]
        if message:
            payload["message"] = message[:200]
        if details:
            payload["details"] = details
        self._insert("status", client_id, payload, now)
        return client_id

    def _insert(
        self,
        kind: Literal["sample", "status"],
        client_id: str,
        payload: dict[str, Any],
        now: float,
    ) -> None:
        payload_json = json.dumps(
            payload, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        )
        self._database.execute(
            """
            INSERT INTO outbox
                (client_id, kind, payload_json, created_at, next_attempt_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (client_id, kind, payload_json, now, now),
        )

    def due_items(self, *, now: float, limit: int) -> list[QueueItem]:
        rows = self._database.execute(
            """
            SELECT id, client_id, kind, payload_json, attempt_count
            FROM outbox
            WHERE quarantined_at IS NULL AND next_attempt_at <= ?
            ORDER BY id
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        items: list[QueueItem] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise RuntimeError("outbox payload is not an object")
            items.append(
                QueueItem(
                    row_id=row["id"],
                    client_id=row["client_id"],
                    kind=row["kind"],
                    payload=payload,
                    attempt_count=row["attempt_count"],
                )
            )
        return items

    def acknowledge(self, client_ids: list[str]) -> None:
        if not client_ids:
            return
        placeholders = ",".join("?" for _ in client_ids)
        with self._database:
            self._database.execute(
                f"DELETE FROM outbox WHERE client_id IN ({placeholders})", client_ids
            )

    def quarantine(self, client_ids: list[str], *, reason: str, now: float) -> None:
        if not client_ids:
            return
        placeholders = ",".join("?" for _ in client_ids)
        with self._database:
            self._database.execute(
                f"""
                UPDATE outbox
                SET quarantined_at = ?, quarantine_reason = ?
                WHERE client_id IN ({placeholders}) AND quarantined_at IS NULL
                """,
                [now, reason[:120], *client_ids],
            )

    def retry(
        self,
        client_ids: list[str],
        *,
        error_code: str,
        next_attempt_at: float,
    ) -> None:
        if not client_ids:
            return
        placeholders = ",".join("?" for _ in client_ids)
        with self._database:
            self._database.execute(
                f"""
                UPDATE outbox
                SET attempt_count = attempt_count + 1,
                    last_error_code = ?,
                    next_attempt_at = ?
                WHERE client_id IN ({placeholders}) AND quarantined_at IS NULL
                """,
                [error_code[:80], next_attempt_at, *client_ids],
            )

    def has_due(self, *, now: float) -> bool:
        return (
            self._database.execute(
                """
                SELECT 1 FROM outbox
                WHERE quarantined_at IS NULL AND next_attempt_at <= ? LIMIT 1
                """,
                (now,),
            ).fetchone()
            is not None
        )

    def stats(self) -> QueueStats:
        row = self._database.execute(
            """
            SELECT
                SUM(CASE WHEN quarantined_at IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN quarantined_at IS NOT NULL THEN 1 ELSE 0 END) AS quarantined
            FROM outbox
            """
        ).fetchone()
        return QueueStats(
            pending=row["pending"] or 0, quarantined=row["quarantined"] or 0
        )

    def reset_health_edges(self) -> None:
        """Force a fresh health edge after app restart or configuration repair."""

        with self._database:
            self._database.execute("DELETE FROM health_state")

    def enforce_limits(
        self,
        *,
        now: float,
        retention_days: int,
        max_items: int,
        reserve_items: int = 1,
    ) -> tuple[int, int]:
        """Drop expired/oldest items and reserve room for one loud health event."""

        cutoff = now - retention_days * 86_400
        with self._database:
            expired_cursor = self._database.execute(
                "DELETE FROM outbox WHERE created_at < ?", (cutoff,)
            )
            expired = max(0, expired_cursor.rowcount)
            count = self._database.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            overflow = max(0, count - max_items)
            if overflow:
                self._database.execute(
                    """
                    DELETE FROM outbox WHERE id IN (
                        SELECT id FROM outbox ORDER BY id LIMIT ?
                    )
                    """,
                    (overflow,),
                )
                count -= overflow
            # Reserve room only when this call actually discarded data and the
            # caller therefore needs to append a health event. Once that event
            # fills the queue, later healthy calls do not churn one row per poll.
            reserve_drop = (
                min(reserve_items, count)
                if (expired or overflow) and count + reserve_items > max_items
                else 0
            )
            if reserve_drop:
                self._database.execute(
                    """
                    DELETE FROM outbox WHERE id IN (
                        SELECT id FROM outbox ORDER BY id LIMIT ?
                    )
                    """,
                    (reserve_drop,),
                )
                overflow += reserve_drop
            if expired or overflow:
                # A dropped status edge may not have reached Waterlog. Force the
                # next poll to emit each stream's current status again.
                self._database.execute("DELETE FROM health_state")
        return expired, overflow

    def close(self) -> None:
        self._database.close()
