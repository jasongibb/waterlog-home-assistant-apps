"""Home Assistant app entry point."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config
from .home_assistant import HomeAssistantClient
from .hydros import HydrosClient
from .logging_utils import configure_logging
from .queue import DurableQueue
from .service import BridgeService, SourceGroup, home_assistant_group, hydros_group
from .uploader import WaterlogUploader


LOGGER = logging.getLogger(__name__)


def main() -> int:
    os.umask(0o077)
    configure_logging("INFO")
    options_path = os.environ.get("WATERLOG_OPTIONS_PATH", "/data/options.json")
    try:
        config = load_config(options_path)
    except ConfigError as error:
        LOGGER.critical("Configuration error: %s", error)
        return 2

    configure_logging(config.log_level, secrets=(config.credential,))
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        LOGGER.critical(
            "SUPERVISOR_TOKEN is unavailable; homeassistant_api must be enabled for this app"
        )
        return 2

    # Redact the local token and every HYDROS secret too, even though
    # application code never logs them.
    hydros_secrets = tuple(
        secret
        for secret in (
            config.hydros_provider_key,
            *(device.device_key for device in config.hydros_devices),
        )
        if secret
    )
    configure_logging(
        config.log_level,
        secrets=(config.credential, supervisor_token, *hydros_secrets),
    )

    data_directory = Path(os.environ.get("WATERLOG_DATA_DIR", "/data"))
    data_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    queue = DurableQueue(data_directory / "waterlog-bridge.sqlite3")
    queue.reset_health_edges()
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    groups: list[SourceGroup] = []
    if config.streams:
        home_assistant = HomeAssistantClient(
            supervisor_token, timeout_seconds=config.request_timeout_seconds
        )
        groups.append(home_assistant_group(home_assistant, config.streams))
    if config.hydros_streams:
        hydros_client = HydrosClient(
            config.hydros_provider_key or "",
            config.hydros_devices,
            timeout_seconds=config.request_timeout_seconds,
        )
        groups.append(hydros_group(hydros_client, config.hydros_streams))

    uploader = WaterlogUploader(config, queue)
    service = BridgeService(
        config,
        queue,
        tuple(groups),
        uploader,
        stop_event=stop_event,
    )
    try:
        service.run()
    except Exception:
        LOGGER.exception("Waterlog Bridge stopped after an unexpected internal error")
        return 1
    finally:
        queue.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
