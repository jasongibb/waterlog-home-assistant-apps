# Waterlog Bridge

Waterlog monitoring and equipment control are independently optional. Control
requires a dedicated show-once control credential plus an explicit list of
power strips (`control_strips`), individual `switch.*` entities
(`control_entities`), or both. Do not reuse the telemetry credential. Leaving
the control credential and both lists empty keeps service writes disabled.

Complete enrollment in Waterlog under **Settings → Integrations → Equipment
control**, then configure `waterlog_control_credential` and `control_strips`
or `control_entities` in this app. List each strip by its Home Assistant device
name or device ID. Every enabled outlet switch on a listed strip (configuration
switches such as LEDs and auto-off timers are left out) appears in Waterlog with
its Home Assistant name, its strip name and its current state. The Bridge
discovers stable registry identities and Waterlog will not enable a tank
profile until the installed configuration is acknowledged.

```yaml
waterlog_control_credential: paste-the-show-once-control-credential
control_strips:
  - Pea Puffer Power Strip
control_entities: []
```

## Outlet schedules

Set an outlet's schedule in Waterlog under **Settings → Outlets**. The Bridge
stores installed schedules locally and switches the outlet at each scheduled
change, using Home Assistant's time zone, even while Waterlog or the Internet
is unreachable.

- A manual switch in Home Assistant or the Tapo app stays until the next
  scheduled change. Restarting the Bridge does not undo it.
- After the Bridge was down, it applies only the latest missed change.
- A change that Home Assistant does not confirm is retried after 30 seconds,
  backing off to every 5 minutes, until it succeeds or the next scheduled
  change replaces it.
- Schedules never switch an outlet that a Tank Mode is using. When the mode
  ends, a scheduled outlet goes to its schedule's current state instead of its
  state before the mode. Outlets without a schedule return to their state
  before the mode, as before.
- Schedules pause while Home Assistant's time zone is unknown or the system
  clock is not trusted.

Temporary-mode restoration is stored under `/data/control.sqlite3` and runs
locally during Internet, Waterlog, or credential outages. Unknown/readback
failures require attention; do not treat a Home Assistant `on` state as proof of
physical flow. Before real use, perform the attended hardware validation listed
in the repository README with an unused load first.

Waterlog Bridge reads mapped numeric entities and sends observations to
Waterlog. Only when the separate control options are configured can it call
switch services, and only for outlets on the listed strips or entities, for
tank modes and outlet schedules.

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
