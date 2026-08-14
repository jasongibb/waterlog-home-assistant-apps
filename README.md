# Waterlog Home Assistant Apps

This is the public Home Assistant app repository for
[Waterlog](https://waterlog.fish). It distributes **Waterlog Bridge**, a
read-only telemetry bridge that samples explicitly configured Home Assistant
numeric entities and sends them to the matching aquarium streams in Waterlog.

## Install

In Home Assistant, open **Settings > Apps > App store > Repositories** and add:

```text
https://github.com/jasongibb/waterlog-home-assistant-apps
```

Install **Waterlog Bridge**, then paste the show-once configuration generated
by **Waterlog > Settings > Integrations**. The bridge samples every five
minutes, stores pending observations in a durable local SQLite queue, and
uploads them in batches. It reads entity state only; it cannot control heaters,
pumps, outlets, ATOs, or other Home Assistant devices.

The app source and operator documentation are in the
[`waterlog-bridge`](./waterlog-bridge) directory. Container releases are built
for `aarch64` and `amd64` and published under the exact app version at
`ghcr.io/jasongibb/waterlog-home-assistant-bridge`.

## Optional HA-Hydros recovery guard

The community HA-Hydros integration can occasionally leave all of its entities
unavailable until its configuration entry is reloaded. The optional
[HA-Hydros recovery blueprint](./blueprints/automation/waterlog/hydros_recovery_guard.yaml)
performs one integration reload after every selected HA-Hydros entity has
remained unavailable for a configured duration.

[Import the blueprint into Home Assistant](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fjasongibb%2Fwaterlog-home-assistant-apps%2Fmain%2Fblueprints%2Fautomation%2Fwaterlog%2Fhydros_recovery_guard.yaml),
then create an automation from it. Select at least one dependable entity from
every HYDROS collective. The defaults wait three minutes before reloading and
hold a ten-minute cooldown.

Only the explicit Home Assistant state `unavailable` counts as an outage;
startup-style `unknown` states are intentionally ignored. If the selected
entities are already unavailable when the automation is created, reload
HA-Hydros once manually. The guard then protects future unavailable
transitions.

The guard makes one attempt per continuous outage. If that reload does not
restore HA-Hydros, it deliberately stops instead of creating a reload loop;
inspect Home Assistant and reload the integration manually. The automation can
be disabled or deleted without changing Waterlog or the Waterlog Bridge.

This is a household recovery workaround for an unofficial community
integration. It is not a life-support controller or alert path. Keep HYDROS and
Home Assistant safety alerts enabled.

## Community integrations

Any Home Assistant integration that exposes numeric sensor entities can feed
Waterlog through the same source and stream mapping, including community
integrations maintained by their own authors.

Setup recipes belong in this repository's [GitHub
Discussions](https://github.com/jasongibb/waterlog-home-assistant-apps/discussions)
so community-specific steps stay separate from Waterlog's supported bridge and
ingest documentation.

Standing disclaimer: not supported by Waterlog, may be subject to vendor terms,
never for life-support control.
