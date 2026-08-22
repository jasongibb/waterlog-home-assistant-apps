# Changelog

## 0.2.0

- Added an optional HYDROS source: read-only polling of the CoralVue HYDROS
  public REST API (`hydros_provider_key`, `hydros_devices`, `hydros_streams`
  options), alongside the existing Home Assistant source. Either source may
  be configured alone or together; at least one stream mapping is still
  required across the two.
- The bridge now polls an ordered list of source groups per cycle instead of
  a single Home Assistant client. An authentication or transport failure in
  one group fails only that group's remaining streams for the cycle; other
  groups keep polling. A config with only Home Assistant streams keeps its
  existing status codes (`home_assistant_unreachable`,
  `partial_home_assistant_failure`) unchanged; multi-source or HYDROS-only
  configs use generic codes (`source_unreachable`, `partial_source_failure`)
  alongside HYDROS-specific ones (`hydros_auth_rejected`,
  `hydros_unreachable`, `hydros_no_state`).
- Added `scripts/hydros_probe.py`, a stdlib-only CLI that verifies a
  provider key / device key pair, prints the discovered device identity and
  Input entries, and generates ready-to-paste `hydros_streams` config
  stanzas and Waterlog stream `externalId` values.
- Home Assistant's exception types now subclass new vendor-agnostic base
  exceptions shared with HYDROS, with no change to their existing
  `PermissionError`/`ConnectionError` ancestry.

## 0.1.1

- Documentation release, no code changes: expanded the public ingest contract
  and vendor-neutral Home Assistant setup guidance.

## 0.1.0

- Initial Home Assistant OS app package.
- Five-minute read-only numeric entity sampling through the Supervisor Core API.
- Durable SQLite sample and health-event outbox with item-aware delivery.
- Exponential retry, rate-limit handling, authentication stop, and permanent-item quarantine.
