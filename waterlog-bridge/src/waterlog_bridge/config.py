"""Home Assistant option loading and validation."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import BridgeConfig, HydrosDeviceConfig, HydrosStreamConfig, StreamConfig


class ConfigError(ValueError):
    """Raised for a safe-to-display configuration problem."""


_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_HYDROS_DEVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HYDROS_VALUE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


def _no_control_characters(value: str) -> bool:
    return not any(ord(character) < 32 or ord(character) == 127 for character in value)


def _no_control_or_whitespace(value: str) -> bool:
    return not any(ord(character) < 33 or ord(character) == 127 for character in value)


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
        raise ConfigError("could not read a valid Home Assistant options file") from error

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
        raise ConfigError("waterlog_url cannot contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ConfigError("waterlog_url cannot contain a path")
    if parsed.scheme != "https" and not allow_insecure:
        raise ConfigError("waterlog_url must use HTTPS")

    credential = options.get("waterlog_credential")
    if (
        not isinstance(credential, str)
        or len(credential.strip()) < 16
        or len(credential) > 2_048
    ):
        raise ConfigError("waterlog_credential is required and appears incomplete")
    credential = credential.strip()
    if any(ord(character) < 33 or ord(character) == 127 for character in credential):
        raise ConfigError("waterlog_credential contains invalid whitespace or control characters")

    stream_options = options.get("streams", [])
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
            raise ConfigError(f"{label}.entity_id is not a valid Home Assistant entity ID")
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

    # -- HYDROS options (design doc §4.1) -----------------------------------

    hydros_provider_key_value = options.get("hydros_provider_key")
    hydros_devices_options = options.get("hydros_devices", [])
    if not isinstance(hydros_devices_options, list):
        raise ConfigError("hydros_devices must be a list")
    if len(hydros_devices_options) > 10:
        raise ConfigError("no more than 10 hydros_devices entries are allowed")

    hydros_provider_key: str | None = None
    if hydros_devices_options:
        if not isinstance(hydros_provider_key_value, str) or not hydros_provider_key_value.strip():
            raise ConfigError(
                "hydros_provider_key is required when hydros_devices is configured"
            )
        if len(hydros_provider_key_value) > 2_048:
            raise ConfigError("hydros_provider_key is too long")
        hydros_provider_key = hydros_provider_key_value.strip()
        if not _no_control_or_whitespace(hydros_provider_key):
            raise ConfigError(
                "hydros_provider_key contains invalid whitespace or control characters"
            )

    hydros_devices: list[HydrosDeviceConfig] = []
    hydros_device_names: set[str] = set()
    for index, item in enumerate(hydros_devices_options):
        label = f"hydros_devices[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not _HYDROS_DEVICE_NAME.fullmatch(name):
            raise ConfigError(
                f"{label}.name must match ^[a-z0-9][a-z0-9_-]{{0,31}}$"
            )
        if name in hydros_device_names:
            raise ConfigError("hydros_devices[].name values must be unique")
        device_key = item.get("device_key")
        if (
            not isinstance(device_key, str)
            or len(device_key) < 16
            or len(device_key) > 2_048
        ):
            raise ConfigError(f"{label}.device_key must be at least 16 characters")
        if not _no_control_or_whitespace(device_key):
            raise ConfigError(
                f"{label}.device_key contains invalid whitespace or control characters"
            )
        hydros_device_names.add(name)
        hydros_devices.append(HydrosDeviceConfig(name=name, device_key=device_key))

    hydros_stream_options = options.get("hydros_streams", [])
    if not isinstance(hydros_stream_options, list):
        raise ConfigError("hydros_streams must be a list")
    if len(hydros_stream_options) > 100:
        raise ConfigError("no more than 100 hydros_streams entries are allowed")

    hydros_streams: list[HydrosStreamConfig] = []
    for index, item in enumerate(hydros_stream_options):
        label = f"hydros_streams[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be an object")
        stream_id = _uuid(item.get("stream_id"), f"{label}.stream_id")
        device = item.get("device")
        if not isinstance(device, str) or device not in hydros_device_names:
            raise ConfigError(f"{label}.device must name a configured hydros device")
        input_name = item.get("input")
        if (
            not isinstance(input_name, str)
            or not 1 <= len(input_name) <= 100
            or not _no_control_characters(input_name)
        ):
            raise ConfigError(
                f"{label}.input must be 1-100 characters with no control characters"
            )
        value_field = item.get("value_field")
        if value_field is None or value_field == "":
            value_field = None
        elif not isinstance(value_field, str) or not _HYDROS_VALUE_FIELD.fullmatch(value_field):
            raise ConfigError(
                f"{label}.value_field must match ^[A-Za-z][A-Za-z0-9]{{0,63}}$"
            )
        unit = item.get("unit")
        if (
            not isinstance(unit, str)
            or not 1 <= len(unit) <= 32
            or not _no_control_characters(unit)
        ):
            raise ConfigError(
                f"{label}.unit is required and must be 1-32 characters with no control characters"
            )
        if stream_id in stream_ids:
            raise ConfigError("stream_id values must be unique")
        stream_ids.add(stream_id)
        hydros_streams.append(
            HydrosStreamConfig(
                stream_id=stream_id,
                device=device,
                input_name=input_name,
                unit=unit,
                value_field=value_field,
            )
        )

    if not streams and not hydros_streams:
        raise ConfigError("at least one stream mapping is required")

    log_level = options.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in _LOG_LEVELS:
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, or ERROR")

    return BridgeConfig(
        waterlog_url=waterlog_url,
        credential=credential,
        streams=tuple(streams),
        hydros_provider_key=hydros_provider_key,
        hydros_devices=tuple(hydros_devices),
        hydros_streams=tuple(hydros_streams),
        sample_interval_seconds=_integer(
            options, "sample_interval_seconds", 300, 300, 300
        ),
        upload_interval_seconds=_integer(
            options, "upload_interval_seconds", 1800, 300, 7200
        ),
        batch_size=_integer(options, "batch_size", 250, 1, 500),
        request_timeout_seconds=_integer(
            options, "request_timeout_seconds", 20, 5, 60
        ),
        queue_retention_days=_integer(
            options, "queue_retention_days", 30, 1, 30
        ),
        max_queue_items=_integer(
            options, "max_queue_items", 100_000, 1_000, 500_000
        ),
        allow_insecure_http=allow_insecure,
        log_level=log_level.upper(),
    )
