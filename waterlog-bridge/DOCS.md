# Waterlog Bridge

Waterlog monitoring and tank-mode control are independently optional. Tank
modes require a dedicated show-once control credential plus an explicit list of
individual `switch.*` entities. Do not reuse the telemetry credential. Leaving
the control credential and entity list empty keeps service writes disabled.

Complete enrollment in Waterlog under **Settings → Integrations → Equipment
control**, then configure `waterlog_control_credential` and `control_entities`
in this app. The Bridge discovers stable registry identities and Waterlog will
not enable a tank profile until the installed configuration is acknowledged.

Temporary-mode restoration is stored under `/data/control.sqlite3` and runs
locally during Internet, Waterlog, or credential outages. Unknown/readback
failures require attention; do not treat a Home Assistant `on` state as proof of
physical flow. Before real use, perform the attended hardware validation listed
in the repository README with an unused load first.

Waterlog Bridge reads mapped numeric entities and sends observations to
Waterlog. Only when the separate control options are configured can it call
switch services for explicitly allowlisted tank-mode outlets.

## Before configuring the app

In Waterlog, create a Home Assistant telemetry source, create one stream for each
entity, and generate a show-once bridge credential. Keep the Waterlog page open
until you have copied the credential and stream IDs into this app's
configuration.

## Configuration

```yaml
waterlog_url: https://waterlog.fish
waterlog_credential: paste-the-show-once-credential
streams:
  - stream_id: 11111111-1111-4111-8111-111111111111
    entity_id: sensor.aquarium_temperature
upload_interval_seconds: 1800
batch_size: 250
request_timeout_seconds: 20
queue_retention_days: 30
max_queue_items: 100000
allow_insecure_http: false
log_level: INFO
```

Omit `unit_override` so the bridge uses the entity's live
`unit_of_measurement` attribute and Waterlog can reject mismatches. Add an
override only when Home Assistant publishes no unit and you have verified that
the numeric value is already expressed in the configured stream unit. An
override labels a value; it does not convert it or safely relabel a measurement
from another unit.

The app rejects non-HTTPS Waterlog URLs unless `allow_insecure_http` is enabled.
That option exists only for local development and must remain off for production.

## Delivery and failure behavior

- Each mapped entity is read through Home Assistant's internal, authenticated
  Core API every five minutes by default.
- Waterlog receives the bridge poll time separately from Home Assistant's
  `last_updated` timestamp when Home Assistant supplies a valid timestamp.
- A valid finite number is committed to `/data/waterlog-bridge.sqlite3` before
  any upload is attempted.
- `unknown`, `unavailable`, missing units, NaN, infinity, and nonnumeric states
  become stream-health events. They never become a numeric zero.
- Uploads are batched about every 30 minutes. Network failures, HTTP 429, and
  server errors use bounded exponential backoff.
- HTTP 401 or 403 disables uploads and emits a critical log message. Sampling
  continues into the bounded local queue. Repair the Waterlog credential and
  restart the app.
- Waterlog must explicitly acknowledge every item. Accepted and duplicate items
  are deleted; rejected and conflicting items are retained as quarantined rows.
- Queue rows older than the configured retention period or beyond the item cap
  are dropped only when necessary. The bridge retains a loud health event and
  critical log whenever that happens.

The app logs counts, safe reason codes, and random client item IDs. It does not
log credentials, Authorization headers, entity values, or request payloads.

## Operational checks

After starting, inspect the app log for:

1. `started with ... stream mappings`;
2. a successful Home Assistant poll;
3. `Waterlog acknowledged ... queued telemetry items`.

An unavailable probe and a healthy Pi are represented separately: the bridge
heartbeat remains healthy while the stream has an `unavailable` edge.

For aquarium life-support protection, continue to use the controller or Home
Assistant's local alerts. Waterlog monitoring is read-only and retrospective.
