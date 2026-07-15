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
