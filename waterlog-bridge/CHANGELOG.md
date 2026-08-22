# Changelog

## 0.2.1

- Withdraws the unvalidated direct HYDROS API preview introduced in 0.2.0.
- Restores the supported Home Assistant-only acquisition path and the
  pre-0.2.0 configuration/runtime behavior. Existing Home Assistant mappings,
  Waterlog credentials, and the durable SQLite queue are unchanged.
- Removes the `hydros_provider_key`, `hydros_devices`, and `hydros_streams`
  options. If Home Assistant reports them as unknown after upgrading, remove
  any retained copies from the saved app options.

## 0.2.0 — withdrawn

- Published an unvalidated direct HYDROS API preview. It is superseded by
  0.2.1 and must not be used for HYDROS acquisition.

## 0.1.1

- Documentation release, no code changes: expanded the public ingest contract
  and vendor-neutral Home Assistant setup guidance.

## 0.1.0

- Initial Home Assistant OS app package.
- Five-minute read-only numeric entity sampling through the Supervisor Core API.
- Durable SQLite sample and health-event outbox with item-aware delivery.
- Exponential retry, rate-limit handling, authentication stop, and permanent-item quarantine.
