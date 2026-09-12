"""Home Assistant app entry point."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import ConfigError, load_config
from .control_client import ControlClient
from .control_models import RegistryOutlet
from .control_service import ControlService
from .control_store import ControlStore, ControlStoreError
from .ha_registry import RegistryError, discover_allowlisted
from .home_assistant import HomeAssistantClient
from .home_assistant_control import HomeAssistantControl
from .logging_utils import configure_logging
from .queue import DurableQueue
from .service import BridgeService
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

    configured_secrets = tuple(
        value for value in (config.credential, config.control_credential) if value
    )
    configure_logging(config.log_level, secrets=configured_secrets)
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        LOGGER.critical(
            "SUPERVISOR_TOKEN is unavailable; homeassistant_api must be enabled for this app"
        )
        return 2

    # Redact the local token too, even though application code never logs it.
    configure_logging(config.log_level, secrets=(*configured_secrets, supervisor_token))

    data_directory = Path(os.environ.get("WATERLOG_DATA_DIR", "/data"))
    data_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    queue = None
    control_store = None
    threads: list[threading.Thread] = []
    try:
        if config.telemetry_enabled:
            queue = DurableQueue(data_directory / "waterlog-bridge.sqlite3")
            queue.reset_health_edges()
            service = BridgeService(
                config,
                queue,
                HomeAssistantClient(
                    supervisor_token, timeout_seconds=config.request_timeout_seconds
                ),
                WaterlogUploader(config, queue),
                stop_event=stop_event,
            )
            if config.control_enabled:
                thread = threading.Thread(
                    target=service.run, name="waterlog-telemetry", daemon=True
                )
                thread.start()
                threads.append(thread)
            else:
                service.run()
        if config.control_enabled:
            identity_path = data_directory / "control-installation-id"
            had_identity = identity_path.exists()
            if had_identity:
                installation_id = str(
                    uuid.UUID(identity_path.read_text(encoding="ascii").strip())
                )
            else:
                installation_id = str(uuid.uuid4())
                temporary = identity_path.with_suffix(".tmp")
                temporary.write_text(installation_id + "\n", encoding="ascii")
                os.chmod(temporary, 0o600)
                temporary.replace(identity_path)
            control_path = data_directory / "control.sqlite3"
            local_state_missing = had_identity and not control_path.exists()
            control_store = ControlStore(control_path, installation_id)
            if local_state_missing:
                control_store.set_meta("recovery_required", "1")
                control_store.db.commit()

            def refresh_control_inventory() -> tuple[RegistryOutlet, ...]:
                pinned = tuple(
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
                    for row in control_store.inventory()
                )
                discovered = discover_allowlisted(
                    supervisor_token,
                    config.control_entities,
                    pinned=pinned,
                )
                control_store.replace_inventory(discovered, time.time())
                return discovered

            inventory = refresh_control_inventory()
            ControlService(
                control_store,
                ControlClient(config.waterlog_url, config.control_credential or ""),
                HomeAssistantControl(supervisor_token),
                installation_id,
                inventory,
                inventory_provider=refresh_control_inventory,
                stop_event=stop_event,
            ).run()
    except (ControlStoreError, RegistryError):
        LOGGER.exception("Waterlog control could not start safely")
        return 2
    except Exception:
        LOGGER.exception("Waterlog Bridge stopped after an unexpected internal error")
        return 1
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=10)
        if queue is not None:
            queue.close()
        if control_store is not None:
            control_store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
