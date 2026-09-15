# Changelog

## 0.3.3

- Allow 15 seconds for Home Assistant to confirm an outlet change after an
  accepted switch call. This avoids aborting Feed when a P316M reports its new
  state just after the previous five-second limit.
- Keep the original restoration deadlines and fail safely if confirmation
  still does not arrive. Timeout logs identify the switch, requested state,
  and last reported state.

## 0.3.2

- Wait for Home Assistant to confirm outlet changes during tank-mode commands.
- Allow 15 seconds for switch service calls, including Tapo's device write and
  refresh, while keeping individual state reads on their existing timeout.
- Keep restoration pending after an uncertain switch operation until the
  baseline is explicitly restored and confirmed. Preserve failed-entry reports.
- Keep the monitoring database on its worker thread so enabling equipment
  control does not interrupt monitoring.

## 0.3.1

- Make telemetry and control credentials truly optional in the app schema so
  either independently optional mode can start without a null credential.
- Monitoring installations can upgrade without creating a control credential.
  If still using 0.3.0, set `waterlog_control_credential: ""` and
  `control_entities: []` in Configuration → Edit in YAML to resume monitoring.

## 0.3.0

- Add opt-in Feed/Normal/Water Change execution for explicitly allowlisted
  Home Assistant switches with a separate Waterlog control credential.
- Persist local baselines, deadlines, recovery obligations, configuration, and
  report outbox independently of telemetry so restoration survives outages and
  restarts.
- Add stable registry identity checks, dependency ordering, service-call
  readback, truthful failure/recovery states, and control-only operation.

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
