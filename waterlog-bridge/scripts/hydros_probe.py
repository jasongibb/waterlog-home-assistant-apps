#!/usr/bin/env python3
"""HYDROS activation-runbook probe (design doc §4.6, §8 step 3).

Confirms a provider key / device key pair works before they are pasted into
the add-on options: looks up device identity, starts one polling session,
polls state once, and prints ready-to-paste ``hydros_streams`` stanzas plus
the matching Waterlog stream ``externalId`` for every discovered Input. The
provider key and device key are never printed.

Usage::

    python scripts/hydros_probe.py --provider-key ... --device-key ...

or, to avoid keys ever appearing in shell history::

    HYDROS_PROVIDER_KEY=... HYDROS_DEVICE_KEY=... python scripts/hydros_probe.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Sequence, TextIO

try:
    from waterlog_bridge.hydros import (
        NUMERIC_FIELD_PRECEDENCE,
        HydrosAuthenticationError,
        HydrosClient,
        HydrosPollTokenError,
        HydrosTransportError,
    )
    from waterlog_bridge.http import HttpTransport
    from waterlog_bridge.models import HydrosDeviceConfig
except ImportError:  # pragma: no cover - only exercised for a bare `python scripts/...` run
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from waterlog_bridge.hydros import (  # noqa: E402
        NUMERIC_FIELD_PRECEDENCE,
        HydrosAuthenticationError,
        HydrosClient,
        HydrosPollTokenError,
        HydrosTransportError,
    )
    from waterlog_bridge.http import HttpTransport  # noqa: E402
    from waterlog_bridge.models import HydrosDeviceConfig  # noqa: E402


_DEVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DEFAULT_BASE_URL = "https://api.coralvuehydros.com"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe a HYDROS provider key / device key pair and print the "
            "discovered device identity, Input entries, and ready-to-paste "
            "hydros_streams stanzas for waterlog-bridge."
        )
    )
    parser.add_argument(
        "--provider-key",
        default=None,
        help="HYDROS provider key (or set HYDROS_PROVIDER_KEY). Never printed.",
    )
    parser.add_argument(
        "--device-key",
        default=None,
        help="HYDROS device key, read-only (or set HYDROS_DEVICE_KEY). Never printed.",
    )
    parser.add_argument(
        "--device-name",
        default=None,
        help=(
            "Local handle used in the generated stanzas (defaults to a slug "
            "of the device's friendlyName)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"Override the HYDROS API base URL (default: {_DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=20, help="Request timeout in seconds."
    )
    return parser.parse_args(argv)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-")
    slug = slug[:32] or "hydros-device"
    if not _DEVICE_NAME.fullmatch(slug):
        slug = ("d" + re.sub(r"[^a-z0-9_-]+", "", slug))[:32] or "hydros-device"
    return slug


def run_probe(
    *,
    provider_key: str,
    device_key: str,
    base_url: str = _DEFAULT_BASE_URL,
    timeout_seconds: int = 20,
    device_name: str | None = None,
    transport: HttpTransport | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the probe end to end. Returns a process exit code."""

    stream = out or sys.stdout
    probe_device = HydrosDeviceConfig(name="probe", device_key=device_key)
    client = HydrosClient(
        provider_key,
        (probe_device,),
        timeout_seconds=timeout_seconds,
        transport=transport,
        base_url=base_url,
    )

    try:
        identity = client.get_device(probe_device)
    except HydrosAuthenticationError:
        print("HYDROS rejected the provider key / device key pair.", file=stream)
        return 2
    except HydrosTransportError as error:
        print(f"Could not reach HYDROS to look up the device: {error}", file=stream)
        return 3

    device_id = identity.get("deviceId")
    friendly_name = identity.get("friendlyName")
    device_type = identity.get("type")

    print("Device identity:", file=stream)
    print(f"  deviceId:     {device_id}", file=stream)
    print(f"  friendlyName: {friendly_name}", file=stream)
    print(f"  type:         {device_type}", file=stream)

    client.begin_cycle()
    try:
        state = client.fetch_state(probe_device)
    except HydrosPollTokenError:
        print(
            "HYDROS accepted the provider key / device key pair (the session "
            "started), but the freshly minted poll token was rejected twice. "
            "This is a session/JWT fault on the HYDROS side, not a key-copy "
            "problem — wait a few minutes and retry before touching the keys.",
            file=stream,
        )
        return 4
    except HydrosAuthenticationError:
        print(
            "HYDROS rejected the provider key / device key pair while starting a session.",
            file=stream,
        )
        return 2
    except HydrosTransportError as error:
        print(f"Could not poll HYDROS device state: {error}", file=stream)
        return 3

    print(f"Auth header form accepted: {client.pinned_header_form}", file=stream)

    if state is None:
        print(
            "No state is cached for this device yet (it may be offline, or the "
            "session just started). Wait a few seconds and re-run the probe.",
            file=stream,
        )
        return 0

    inputs = state.get("Input")
    if not isinstance(inputs, dict) or not inputs:
        print("The state document has no Input entries yet.", file=stream)
        return 0

    local_device_name = device_name or _slugify(
        friendly_name if isinstance(friendly_name, str) and friendly_name else str(device_id or "hydros-device")
    )

    print("", file=stream)
    print("Input entries:", file=stream)
    stanzas: list[str] = []
    external_ids: list[str] = []
    for input_name, entry in inputs.items():
        if not isinstance(entry, dict):
            continue
        print(f"  {input_name}:", file=stream)
        default_field = next(
            (name for name in NUMERIC_FIELD_PRECEDENCE if name in entry), None
        )
        for key, value in entry.items():
            marker = "  <- default numeric field" if key == default_field else ""
            print(f"    {key} = {value!r}{marker}", file=stream)

        if isinstance(device_id, str) and device_id:
            external_ids.append(f"{device_id}/{input_name}")

        field_comment = (
            f"# default numeric field: {default_field}"
            if default_field is not None
            else "# no numeric field found; set value_field explicitly"
        )
        stanzas.append(
            "  - stream_id: <paste-a-new-uuid>\n"
            f"    device: {local_device_name}\n"
            f'    input: "{input_name}"\n'
            f'    unit: "TODO"  {field_comment}'
        )

    print("", file=stream)
    print("Ready-to-paste hydros_streams stanzas (fill in stream_id and unit):", file=stream)
    for stanza in stanzas:
        print(stanza, file=stream)
        print("", file=stream)

    print("Waterlog stream externalId values (device/input):", file=stream)
    for external_id in external_ids:
        print(f"  {external_id}", file=stream)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    provider_key = args.provider_key or os.environ.get("HYDROS_PROVIDER_KEY")
    device_key = args.device_key or os.environ.get("HYDROS_DEVICE_KEY")
    if not provider_key or not device_key:
        print(
            "Provide --provider-key/--device-key or set "
            "HYDROS_PROVIDER_KEY/HYDROS_DEVICE_KEY.",
            file=sys.stderr,
        )
        return 2
    return run_probe(
        provider_key=provider_key,
        device_key=device_key,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
        device_name=args.device_name,
    )


if __name__ == "__main__":
    sys.exit(main())
