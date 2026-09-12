"""Home Assistant option loading and validation."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import BridgeConfig, StreamConfig


class ConfigError(ValueError):
    """Raised for a safe-to-display configuration problem."""


_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _integer(
    options: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ConfigError(f"{label} must be a UUID") from error


def load_config(path: str | Path) -> BridgeConfig:
    """Load Supervisor's options file without ever echoing its secret values."""

    try:
        options_path = Path(path)
        if options_path.stat().st_size > 1_048_576:
            raise ConfigError("Home Assistant options file is unexpectedly large")
        raw = options_path.read_text(encoding="utf-8")
        options = json.loads(raw)
    except ConfigError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            "could not read a valid Home Assistant options file"
        ) from error

    if not isinstance(options, dict):
        raise ConfigError("Home Assistant options must be a JSON object")

    allow_insecure = options.get("allow_insecure_http", False)
    if not isinstance(allow_insecure, bool):
        raise ConfigError("allow_insecure_http must be true or false")

    waterlog_url = options.get("waterlog_url")
    if not isinstance(waterlog_url, str) or not waterlog_url.strip():
        raise ConfigError("waterlog_url is required")
    if len(waterlog_url) > 2_048:
        raise ConfigError("waterlog_url is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in waterlog_url):
        raise ConfigError("waterlog_url contains an invalid control character")
    waterlog_url = waterlog_url.strip().rstrip("/")
    try:
        parsed = urlsplit(waterlog_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ConfigError("waterlog_url is not a valid URL") from error
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ConfigError("waterlog_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            "waterlog_url cannot contain credentials, a query, or a fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ConfigError("waterlog_url cannot contain a path")
    if parsed.scheme != "https" and not allow_insecure:
        raise ConfigError("waterlog_url must use HTTPS")

    credential_value = options.get("waterlog_credential")
    credential: str | None
    if credential_value is None or credential_value == "":
        credential = None
    elif (
        isinstance(credential_value, str)
        and 16 <= len(credential_value.strip()) <= 2_048
    ):
        credential = credential_value.strip()
        if any(
            ord(character) < 33 or ord(character) == 127 for character in credential
        ):
            raise ConfigError(
                "waterlog_credential contains invalid whitespace or control characters"
            )
    else:
        raise ConfigError("waterlog_credential appears incomplete")

    stream_options = options.get("streams")
    if not isinstance(stream_options, list):
        raise ConfigError("streams must be a list")
    if len(stream_options) > 100:
        raise ConfigError("no more than 100 stream mappings are allowed")

    streams: list[StreamConfig] = []
    stream_ids: set[str] = set()
    entity_ids: set[str] = set()
    for index, item in enumerate(stream_options):
        label = f"streams[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be an object")
        stream_id = _uuid(item.get("stream_id"), f"{label}.stream_id")
        entity_id = item.get("entity_id")
        if (
            not isinstance(entity_id, str)
            or len(entity_id) > 255
            or not _ENTITY_ID.fullmatch(entity_id.strip())
        ):
            raise ConfigError(
                f"{label}.entity_id is not a valid Home Assistant entity ID"
            )
        entity_id = entity_id.strip()
        unit_value = item.get("unit_override")
        unit_override: str | None
        if unit_value is None or unit_value == "":
            unit_override = None
        elif isinstance(unit_value, str) and 0 < len(unit_value.strip()) <= 32:
            unit_override = unit_value.strip()
            if any(
                ord(character) < 32 or ord(character) == 127
                for character in unit_override
            ):
                raise ConfigError(
                    f"{label}.unit_override contains an invalid control character"
                )
        else:
            raise ConfigError(f"{label}.unit_override must be at most 32 characters")
        if stream_id in stream_ids:
            raise ConfigError("stream_id values must be unique")
        if entity_id in entity_ids:
            raise ConfigError("entity_id values must be unique")
        stream_ids.add(stream_id)
        entity_ids.add(entity_id)
        streams.append(StreamConfig(stream_id, entity_id, unit_override))

    control_credential_value = options.get("waterlog_control_credential")
    control_credential: str | None
    if control_credential_value is None or control_credential_value == "":
        control_credential = None
    elif (
        isinstance(control_credential_value, str)
        and 16 <= len(control_credential_value.strip()) <= 2_048
    ):
        control_credential = control_credential_value.strip()
        if any(
            ord(character) < 33 or ord(character) == 127
            for character in control_credential
        ):
            raise ConfigError(
                "waterlog_control_credential contains invalid whitespace or control characters"
            )
    else:
        raise ConfigError("waterlog_control_credential appears incomplete")
    raw_control_entities = options.get("control_entities", [])
    if not isinstance(raw_control_entities, list) or len(raw_control_entities) > 64:
        raise ConfigError("control_entities must contain at most 64 switch entity IDs")
    control_entities: list[str] = []
    for index, value in enumerate(raw_control_entities):
        if not isinstance(value, str) or not re.fullmatch(
            r"switch[.][a-z0-9_]+", value.strip()
        ):
            raise ConfigError(
                f"control_entities[{index}] is not an individual switch entity ID"
            )
        if value.strip() in control_entities:
            raise ConfigError("control entity IDs must be unique")
        control_entities.append(value.strip())
    if credential and not streams:
        raise ConfigError("at least one stream is required with waterlog_credential")
    if streams and not credential:
        raise ConfigError("waterlog_credential is required with telemetry streams")
    if bool(control_entities) != bool(control_credential):
        raise ConfigError(
            "control_entities and waterlog_control_credential must be configured together"
        )
    if not streams and not control_entities:
        raise ConfigError(
            "configure telemetry streams or explicitly allowlisted control entities"
        )

    log_level = options.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in _LOG_LEVELS:
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, or ERROR")

    return BridgeConfig(
        waterlog_url=waterlog_url,
        credential=credential,
        streams=tuple(streams),
        control_credential=control_credential,
        control_entities=tuple(control_entities),
        sample_interval_seconds=_integer(
            options, "sample_interval_seconds", 300, 300, 300
        ),
        upload_interval_seconds=_integer(
            options, "upload_interval_seconds", 1800, 300, 7200
        ),
        batch_size=_integer(options, "batch_size", 250, 1, 500),
        request_timeout_seconds=_integer(options, "request_timeout_seconds", 20, 5, 60),
        queue_retention_days=_integer(options, "queue_retention_days", 30, 1, 30),
        max_queue_items=_integer(options, "max_queue_items", 100_000, 1_000, 500_000),
        allow_insecure_http=allow_insecure,
        log_level=log_level.upper(),
    )
