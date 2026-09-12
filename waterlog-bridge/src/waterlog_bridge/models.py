"""Small immutable models shared by the bridge modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """One Home Assistant entity mapped to one immutable Waterlog stream."""

    stream_id: str
    entity_id: str
    unit_override: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Validated app configuration loaded from Home Assistant options."""

    waterlog_url: str
    credential: str | None = field(default=None, repr=False)
    streams: tuple[StreamConfig, ...] = ()
    control_credential: str | None = field(default=None, repr=False)
    control_entities: tuple[str, ...] = ()
    sample_interval_seconds: int = 300
    upload_interval_seconds: int = 1800
    batch_size: int = 250
    request_timeout_seconds: int = 20
    queue_retention_days: int = 30
    max_queue_items: int = 100_000
    allow_insecure_http: bool = False
    log_level: str = "INFO"

    @property
    def ingest_url(self) -> str:
        return f"{self.waterlog_url.rstrip('/')}/api/ingest/telemetry"

    @property
    def telemetry_enabled(self) -> bool:
        return self.credential is not None and bool(self.streams)

    @property
    def control_enabled(self) -> bool:
        return self.control_credential is not None and bool(self.control_entities)


@dataclass(frozen=True, slots=True)
class EntityReading:
    """Normalized result from a Home Assistant state read."""

    status: Literal["ok", "unavailable", "error"]
    value: float | None = None
    unit: str | None = None
    source_updated_at: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class QueueItem:
    """A durable item selected from the SQLite outbox."""

    row_id: int
    client_id: str
    kind: Literal["sample", "status"]
    payload: dict[str, Any]
    attempt_count: int


@dataclass(frozen=True, slots=True)
class QueueStats:
    pending: int
    quarantined: int


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    attempted: int = 0
    acknowledged: int = 0
    quarantined: int = 0
    retryable: int = 0
    retry_at: float | None = None
    auth_blocked: bool = False
    has_more_due: bool = False
