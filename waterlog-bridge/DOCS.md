# Waterlog Bridge

Waterlog Bridge reads the Home Assistant numeric entities you explicitly map and
sends observations to Waterlog. It does not call Home Assistant services and
cannot control heaters, pumps, outlets, ATOs, or other equipment.

It can optionally also poll the official CoralVue HYDROS public REST API
directly (a second, independent source), for households whose controller is
not bridged through Home Assistant. HYDROS polling is read-only: the bridge
never writes an override or command, and HYDROS device keys should always be
created **Read only** in the HYDROS app.

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

### HYDROS configuration (optional)

Add a HYDROS provider key, one entry per HYDROS device, and one stream per
Input you want to send to Waterlog. `streams` may be empty when `hydros_streams`
is used instead, as long as at least one stream is configured across the two:

```yaml
hydros_provider_key: paste-your-hydros-provider-key
hydros_devices:
  - name: lagoon-launch
    device_key: paste-a-read-only-device-key
hydros_streams:
  - stream_id: 22222222-2222-4222-8222-222222222222
    device: lagoon-launch
    input: "pH"
    unit: "pH"
  - stream_id: 33333333-3333-4333-8333-333333333333
    device: lagoon-launch
    input: "Temperature 1"
    unit: "°C"
```

- `name` is a local handle (lowercase letters, digits, `_`/`-`) used only to
  link a `hydros_streams` entry to its `hydros_devices` entry; it is not sent
  to HYDROS or Waterlog.
- `device_key` must be created in the HYDROS app (Device Properties → Manage
  API Keys) with **Read only** permission. The bridge never writes to HYDROS.
- `input` is the exact Input name from the HYDROS state document (case- and
  space-sensitive, 1–100 characters); renaming a sensor in the HYDROS app
  breaks the mapping until the name is restored or the stream is recreated.
- An authentication rejection (bad key pair, or a poll token rejected twice)
  pauses that device for 45 minutes before the bridge tries again, keeping
  session starts far inside the vendor's 5/hour budget. During the pause the
  device's streams report `hydros_auth_rejected`.
- `value_field` is optional. Omit it to use the first present of
  `probeValue`, `senseValue`, `value`, `i10Value`; set it only to override
  that default for one Input.
- `unit` is required — HYDROS state documents carry no units. It must
  byte-match the incoming unit configured for the matching Waterlog stream.

Run `python scripts/hydros_probe.py --provider-key ... --device-key ...`
before configuring the add-on to confirm the key pair works and to get a
ready-to-paste `hydros_streams` stanza per discovered Input, including the
Waterlog stream `externalId` (`deviceId/Input name`) to use when creating
each stream in Waterlog.

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
- Home Assistant and HYDROS are independent sources: a failure in one (a bad
  HYDROS key, HYDROS cloud outage, and so on) does not stop the other from
  polling. A HYDROS device with no cached state reports `unavailable` /
  `hydros_no_state` for every one of its mapped streams; that is expected
  while the device is offline or a session has not yet started.
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
