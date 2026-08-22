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
class HydrosDeviceConfig:
    """One HYDROS device, identified by a local handle and its read-only device key."""

    name: str
    device_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class HydrosStreamConfig:
    """One HYDROS Input mapped to one immutable Waterlog stream."""

    stream_id: str
    device: str
    input_name: str
    unit: str
    value_field: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Validated app configuration loaded from Home Assistant options."""

    waterlog_url: str
    credential: str = field(repr=False)
    streams: tuple[StreamConfig, ...]
    sample_interval_seconds: int = 300
    upload_interval_seconds: int = 1800
    batch_size: int = 250
    request_timeout_seconds: int = 20
    queue_retention_days: int = 30
    max_queue_items: int = 100_000
    allow_insecure_http: bool = False
    log_level: str = "INFO"
    hydros_provider_key: str | None = field(default=None, repr=False)
    hydros_devices: tuple[HydrosDeviceConfig, ...] = ()
    hydros_streams: tuple[HydrosStreamConfig, ...] = ()

    @property
    def ingest_url(self) -> str:
        return f"{self.waterlog_url.rstrip('/')}/api/ingest/telemetry"


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
