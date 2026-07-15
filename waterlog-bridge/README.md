# Waterlog Home Assistant Bridge

This directory is a self-contained Home Assistant OS app (formerly called an
add-on). It is the first vendor-neutral acquisition adapter for Waterlog
monitoring. ReefATO+, ESPHome, HYDROS, and other integrations are all treated as
ordinary Home Assistant numeric entities.

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

The request body is:

```json
{
  "samples": [
    {
      "clientSampleId": "4dba98c1-7714-47a7-9de4-4cf765fb5571",
      "streamId": "11111111-1111-4111-8111-111111111111",
      "observedAt": "2026-07-15T03:00:00.000Z",
      "sourceUpdatedAt": "2026-07-15T02:59:58.000Z",
      "value": 78.2,
      "unit": "°F"
    }
  ],
  "statuses": [
    {
      "clientStatusId": "a05442dd-38ce-473c-aec1-c6ffce82cf22",
      "kind": "stream",
      "streamId": "11111111-1111-4111-8111-111111111111",
      "occurredAt": "2026-07-15T03:00:00.000Z",
      "status": "unavailable",
      "code": "entity_unavailable"
    }
  ]
}
```

The response contains indexed item acknowledgements in `samples` and `statuses`:

```json
{
  "samples": [{ "index": 0, "status": "accepted" }],
  "statuses": [{ "index": 0, "status": "duplicate" }]
}
```

`accepted` and `duplicate` are safe to delete. `rejected` and `conflict` are
permanent and quarantined. An omitted item or `retryable` result remains queued.
The parser also accepts a unified `results` array when it identifies each item
by kind/index or client ID.

## Home Assistant installation

Add `https://github.com/jasongibb/waterlog-home-assistant-apps` as a custom repository in the Home
Assistant app store, then install **Waterlog Bridge**. Releases are published as
versioned `amd64`/`aarch64` images at the exact version from `config.yaml`; no
mutable `latest` tag is used.

For local development, copy this directory to `/addons/waterlog_bridge`, comment
out the `image` field in `config.yaml`, reload the app store, and install it from
Local apps so Supervisor builds the working tree instead of pulling GHCR.

Current Home Assistant app documentation:

- <https://developers.home-assistant.io/docs/apps/configuration/>
- <https://developers.home-assistant.io/docs/apps/communication/>
- <https://developers.home-assistant.io/docs/apps/publishing/>
