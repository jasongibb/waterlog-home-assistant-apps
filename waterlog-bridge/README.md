# Waterlog Home Assistant Bridge

This directory is a self-contained Home Assistant OS app (formerly called an
add-on). It is Waterlog's vendor-neutral acquisition adapter: any Home Assistant
integration that exposes a numeric sensor entity can use the same stream
mapping.

The bridge deliberately has no Home Assistant service-write code. Its only Home
Assistant permission is `homeassistant_api: true`, used to `GET` configured
entity states through `http://supervisor/core/api` with the injected
`SUPERVISOR_TOKEN`. The Waterlog credential is used only in an Authorization
header to `POST /api/ingest/telemetry`.

## Package layout

- `config.yaml` — Supervisor app metadata, options, and schema.
- `Dockerfile` / `run.sh` — multi-architecture container entry point.
- `src/waterlog_bridge` — configuration, read-only HA client, SQLite outbox,
  uploader, and scheduler.
- `DOCS.md` — installation-time user documentation.
- `tests` — deterministic standard-library unit tests.

The runtime has no third-party Python dependencies. The container supports the
Home Assistant OS Raspberry Pi architecture (`aarch64`) and development/host
architecture (`amd64`). Persistent state lives only under `/data`.

## Local tests

From this directory:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
docker build --build-arg BUILD_ARCH=amd64 -t waterlog-bridge:test .
```

PowerShell equivalent:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
docker build --build-arg BUILD_ARCH=amd64 -t waterlog-bridge:test .
```

## Ingest contract

This section is the public contract for the shipped endpoint. A third-party
collector can implement it without using the bundled bridge.

### Endpoint and authentication

Send:

```http
POST https://<waterlog-origin>/api/ingest/telemetry
Authorization: Bearer <show-once Waterlog telemetry credential>
Content-Type: application/json
Accept: application/json
```

The Authorization value is exactly one non-whitespace bearer token. Never put a
credential in a URL, query string, request body, log, or diagnostic report. A
credential authenticates one telemetry source; the server derives the
organization from that source and accepts only its configured streams.

### Strict envelope

The JSON body is a strict object with `samples` and/or `statuses` arrays:

```json
{
  "samples": [
    {
      "clientSampleId": "4dba98c1-7714-47a7-9de4-4cf765fb5571",
      "streamId": "11111111-1111-4111-8111-111111111111",
      "observedAt": "2026-07-15T03:00:00.000Z",
      "sourceUpdatedAt": "2026-07-15T02:59:58.000Z",
      "value": 25.8,
      "unit": "°C"
    }
  ],
  "statuses": [
    {
      "clientStatusId": "a05442dd-38ce-473c-aec1-c6ffce82cf22",
      "kind": "bridge",
      "occurredAt": "2026-07-15T03:00:00.000Z",
      "status": "ok"
    },
    {
      "clientStatusId": "71e45cdd-6339-4e29-ac6a-d71782747e34",
      "kind": "stream",
      "streamId": "11111111-1111-4111-8111-111111111111",
      "occurredAt": "2026-07-15T03:00:00.000Z",
      "status": "unavailable",
      "code": "entity_unavailable",
      "message": "Entity unavailable",
      "details": {
        "attempt": 3,
        "recoverable": true
      }
    }
  ]
}
```

Either top-level array may be omitted and is treated as empty, but the combined
batch must contain at least one item. Unknown top-level keys make the entire
envelope invalid. The body must be nonempty UTF-8 JSON. A valid envelope
deliberately holds unparsed items so one malformed item can receive
`rejected / invalid_item` without blocking valid neighbors.

#### Sample fields

Sample objects are strict; unknown or missing fields produce
`rejected / invalid_item`.

| Field | Required | Contract |
| --- | --- | --- |
| `clientSampleId` | Yes | UUID used only to correlate the acknowledgement. It is **not** the sample idempotency key. |
| `streamId` | Yes | UUID of an active stream owned by the authenticated source. |
| `observedAt` | Yes | ISO 8601 timestamp with `Z` or a numeric UTC offset. This becomes the device measurement time. |
| `sourceUpdatedAt` | No | ISO 8601 timestamp with `Z` or a numeric UTC offset for the upstream entity update. It cannot be more than five minutes after server time or more than five minutes after `observedAt`. |
| `value` | Yes | Finite JSON number. `NaN`, infinities, strings, `unknown`, and `unavailable` are not samples. |
| `unit` | Yes | Nonblank string of at most 40 characters. It must byte-match the stream's configured incoming unit. |

Do not send an organization, aquarium, parameter, or canonical unit. Those are
resolved from the immutable stream mapping.

#### Status fields

Status objects are strict; unknown or missing fields produce
`rejected / invalid_item`.

| Field | Required | Contract |
| --- | --- | --- |
| `clientStatusId` | Yes | UUID. Together with the authenticated source, this is the status idempotency key. |
| `kind` | Yes | `bridge` or `stream`. |
| `streamId` | Conditional | Required for `kind: "stream"` and forbidden for `kind: "bridge"`. The UUID must name an active stream owned by the authenticated source. |
| `occurredAt` | Yes | ISO 8601 timestamp with `Z` or a numeric UTC offset. |
| `status` | Yes | `ok`, `unavailable`, or `error`. |
| `code` | No | Machine-readable reason matching `^[a-z][a-z0-9_]{0,63}$`. This producer reason is distinct from an acknowledgement error code. |
| `message` | No | Human-readable text of at most 200 characters. Do not include credentials or sensitive payloads. |
| `details` | No | Flat JSON object with at most 16 fields and a serialized UTF-8 size of at most 1,900 bytes. |

Each `details` key is 1–64 characters. A value may be `null`, a boolean, a
string of at most 256 characters, or a finite number from
`-1,000,000,000,000` through `1,000,000,000,000`; a nonzero number must have an
absolute value of at least `0.000001`. Arrays and nested objects are not
accepted.

### Identity and idempotency

Samples and statuses intentionally use different identities:

- A sample is unique by
  `(organization_id, stream_id, measured_at)`, where `measured_at` is
  `observedAt`. `clientSampleId` is required for queue correlation but is not
  stored as identity and cannot create a second sample at the same stream and
  device timestamp.
- A status is unique by `(source_id, client_status_id)`.

For a sample retry, reuse the same `streamId`, `observedAt`, value, unit, and
`sourceUpdatedAt`. After schema, stream, timestamp, and unit validation succeeds,
changing content at an existing sample identity returns
`conflict / sample_conflict`. For a previously unseen identity, two identical
copies in the same request produce one `accepted` acknowledgement and one
`duplicate`; if that identity was already stored, both are `duplicate`.

For a status retry, reuse the same `clientStatusId` and the identical
kind/stream/status/time/code/message/details payload. After schema, stream, and
timestamp validation succeeds, reusing that ID with different content returns
`conflict / status_conflict`. A new `clientStatusId` represents a new append-only
status event even when its time matches another event.

### Item acknowledgements

A valid processed batch returns HTTP `200` and one result for each input item in
the matching array:

```json
{
  "samples": [
    {
      "index": 0,
      "clientSampleId": "4dba98c1-7714-47a7-9de4-4cf765fb5571",
      "streamId": "11111111-1111-4111-8111-111111111111",
      "status": "accepted"
    }
  ],
  "statuses": [
    {
      "index": 0,
      "clientStatusId": "a05442dd-38ce-473c-aec1-c6ffce82cf22",
      "status": "duplicate"
    }
  ]
}
```

`index` is zero-based within the request's `samples` or `statuses` array. Client
and stream IDs are echoed when the server can safely extract them, so they may
be absent on malformed items.

| Acknowledgement status | Meaning | Collector action |
| --- | --- | --- |
| `accepted` | A new item was committed. | Delete the queued item. |
| `duplicate` | The exact identity and content were already committed. | Delete the queued item. |
| `rejected` | Permanent item validation or mapping failure. | Quarantine the item and fix the producer or mapping. |
| `conflict` | The identity exists with different content. | Quarantine; never overwrite or retry changed content under that identity. |
| `retryable` | Storage was not confirmed or the server used its defensive fallback. | Preserve the exact queued item and retry with backoff. |

The complete acknowledgement-code inventory is:

| Code | Status | Item kind | Meaning |
| --- | --- | --- | --- |
| `invalid_item` | `rejected` | Sample or status | The strict item schema failed, including missing/extra fields or field bounds. |
| `stream_not_available` | `rejected` | Sample or stream status | The stream is unknown, belongs to another source/organization, is retired, or its source/aquarium/parameter is not currently eligible. The response intentionally does not distinguish these cases. |
| `timestamp_future` | `rejected` | Sample or status | `observedAt` / `occurredAt` is more than five minutes after server time. |
| `timestamp_too_old` | `rejected` | Sample or status | The timestamp is outside the active backfill window. |
| `source_timestamp_future` | `rejected` | Sample | `sourceUpdatedAt` is more than five minutes after server time or more than five minutes after `observedAt`. |
| `unit_mismatch` | `rejected` | Sample | Submitted `unit` does not byte-match the stream's incoming unit. |
| `unit_conversion_unsupported` | `rejected` | Sample | The configured incoming-to-canonical conversion is unsupported or non-finite. |
| `sample_conflict` | `conflict` | Sample | The sample identity exists with different content. |
| `status_conflict` | `conflict` | Status | The status identity exists with different content. |
| `storage_unconfirmed` | `retryable` | Sample or status | The item could not be confirmed after the insert attempt. |
| `unclassified` | `retryable` | Sample or status | Defensive fallback for an item that reached no classified result. |

Item-level `rejected` or `conflict` results do not roll back accepted neighbors.
An envelope-level HTTP error or source quota failure has no item
acknowledgements.

### Limits

| Limit | Enforced behavior |
| --- | --- |
| Request body | Maximum 256 KiB (262,144 raw bytes), checked before JSON parsing. |
| Batch size | 1–500 combined sample and status items. Each array is individually capped at 500, and the combined cap remains 500. |
| Request rate | 120 authenticated requests per 60-second fixed window **per credential**. Excess requests receive `429` with `Retry-After`. |
| Daily sample quota | New sample identities received since 00:00 UTC are capped by the source's `daily_sample_quota`. Exact duplicates do not consume another slot. |
| Daily status quota | New status identities received since 00:00 UTC are independently capped by that same source `daily_sample_quota` value. Samples and statuses have two equal ceilings, **not one shared pool**. |
| Future skew | `observedAt` and `occurredAt` may be at most five minutes after server time. `sourceUpdatedAt` also obeys the bounds described above. |
| Ordinary backfill | Up to 31 days before server time. |
| Extended backfill | Ingest honors a per-source extended-backfill window of up to 90 days before server time. This is an ingest-acceptance limit independent of raw retention. Enabling this window is not currently self-service: there is no Admin control. Without operator action, the effective backfill limit is the ordinary 31 days. |

Backfill acceptance and retention are separate contracts. Waterlog does not
automatically age-delete accepted raw telemetry samples. Status events and
derived hourly/daily rollups keep independent retention policies, and server-side
materialization remains bounded. Retaining raw samples does not make bridge,
client, chart, or MCP responses unbounded; each surface keeps its own window,
batch, and response limits. Storage growth and compute are monitored against
the current budget, and any retention-policy revisit is based on measured
usage rather than a speculative age cutoff.

If adding the new identities in either daily ledger would exceed its ceiling,
the whole ingest transaction returns `429`; no sample or status from that
request commits. `Retry-After` points to the next UTC day for daily quota
failures.

### Units and immutable streams

The submitted `unit` must byte-match the immutable incoming unit, including
case, whitespace, and the Unicode degree symbol. The only supported conversion
dimension is Temperature:

- Celsius aliases: `°C`, `C`, `degC`
- Fahrenheit aliases: `°F`, `F`, `degF`

Waterlog normalizes between aliases in one set and converts between the Celsius
and Fahrenheit sets. Every non-Temperature stream must use an incoming unit
that exactly equals its parameter's canonical unit.

The bundled bridge's `unit_override` is only for an entity with no
`unit_of_measurement`. It labels an already-correct value; it never converts.
Never use it to relabel a measurement from another unit.

A stream's source, aquarium, parameter, external entity ID, incoming unit,
canonical unit, and equipment mapping are immutable. A label may be edited, but
remapping identity requires retiring the old stream and creating a new one.
Never repurpose a `streamId`; retirement preserves historical meaning.

### HTTP failures and retry policy

| Result | Collector action |
| --- | --- |
| `200` | Process every item acknowledgement independently. Delete only `accepted` and `duplicate` items. |
| `400` | The request failed before item processing: malformed or negative `Content-Length`; a missing or zero-byte body; a non-UTF-8 body; or invalid JSON or envelope. Do not retry the unchanged batch; fix the request framing, encoding, serializer, or envelope. |
| `401` | Credential is invalid, malformed, expired, or revoked, or its source is unavailable during credential resolution. Stop uploads and alert an operator until credential/source repair. |
| `403` | The authenticated source became unavailable before ingest completed. Stop uploads and alert an operator until the source configuration is repaired. |
| `413` | Body exceeds 256 KiB. Split or reduce the batch; do not retry it unchanged. |
| `429` | Preserve all items, honor `Retry-After`, and retry with exponential backoff plus jitter. |
| Network failure or `5xx` | Preserve all items and retry with bounded exponential backoff plus jitter. |

Do not delete an item merely because the HTTP request succeeded. Delete it only
after its explicit `accepted` or `duplicate` acknowledgement. Treat an omitted
item result as retryable.

## Home Assistant installation

Add `https://github.com/jasongibb/waterlog-home-assistant-apps` as a custom
repository in the Home Assistant app store, then install **Waterlog Bridge**.
Releases are published as versioned `amd64`/`aarch64` images at the exact
version from `config.yaml`; no mutable `latest` tag is used.

For local development, copy this directory to `/addons/waterlog_bridge`, comment
out the `image` field in `config.yaml`, reload the app store, and install it from
Local apps so Supervisor builds the working tree instead of pulling GHCR.

Current Home Assistant app documentation:

- <https://developers.home-assistant.io/docs/apps/configuration/>
- <https://developers.home-assistant.io/docs/apps/communication/>
- <https://developers.home-assistant.io/docs/apps/publishing/>
