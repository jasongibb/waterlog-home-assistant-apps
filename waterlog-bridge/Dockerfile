FROM python:3.13.13-alpine3.22@sha256:e81548ac35b07a3bd4805f275107592ef458b1e893c0e04d45aedaa19416cca5

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Waterlog Bridge" \
    org.opencontainers.image.description="Home Assistant telemetry and opt-in tank-mode bridge for Waterlog" \
    org.opencontainers.image.source="https://github.com/jasongibb/waterlog-home-assistant-apps" \
    org.opencontainers.image.licenses="UNLICENSED"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY src/ /app/
COPY run.sh /run.sh
RUN pip install --no-cache-dir websocket-client==1.8.0 && chmod 0755 /run.sh

ENTRYPOINT ["/run.sh"]
