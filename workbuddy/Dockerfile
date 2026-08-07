# syntax=docker/dockerfile:1.7

ARG WEBTOP_IMAGE=lscr.io/linuxserver/webtop:ubuntu-kde

FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS clawmanager-agent-builder

ARG TARGETOS=linux
ARG TARGETARCH

WORKDIR /src/clawmanager-agent
COPY clawmanager-agent ./
RUN go mod download
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/clawmanager-agent ./cmd/clawmanager-agent

FROM ${WEBTOP_IMAGE} AS workbuddy-builder

ARG DEBIAN_FRONTEND=noninteractive
ARG WORKBUDDY_LINUX_REF=3fa7d6523bfa17bfb6aa41a48ffc7fa1ecc072cd

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl nodejs sudo tar; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src/workbuddy-linux

RUN set -eux; \
    curl -fsSL "https://github.com/JipZeonGit/workbuddy-linux/archive/${WORKBUDDY_LINUX_REF}.tar.gz" \
      | tar -xz --strip-components=1; \
    bash scripts/install-deps.sh

# The proprietary WorkBuddy DMG is mounted read-only from a named BuildKit
# context only for this step. It is not copied into an image layer. The
# generated app still contains proprietary WorkBuddy code and must only be
# used/distributed under Tencent's terms.
RUN --mount=type=bind,from=workbuddy_input,source=.,target=/workbuddy-input,ro \
    --mount=type=cache,id=workbuddy-electron-cache,target=/var/cache/workbuddy-electron \
    --mount=type=cache,id=workbuddy-npm-cache,target=/var/cache/npm \
    set -eux; \
    mapfile -d '' dmg_files < <(find /workbuddy-input -maxdepth 1 -type f -name '*.dmg' -print0); \
    test "${#dmg_files[@]}" -eq 1; \
    sed -i \
      's/curl -L --fail --continue-at -/curl --http1.1 -L --fail --retry 8 --retry-all-errors --retry-delay 3 --continue-at -/' \
      scripts/lib/electron.sh; \
    WORKBUDDY_ELECTRON_CACHE_DIR=/var/cache/workbuddy-electron \
    NPM_CONFIG_CACHE=/var/cache/npm \
      bash install.sh "${dmg_files[0]}"; \
    PACKAGE_VERSION="$(node -e 'const fs=require("fs");const p="workbuddy-app/.workbuddy-linux/build-info.json";process.stdout.write(JSON.parse(fs.readFileSync(p,"utf8")).upstreamVersion||"0.0.0")')" \
      bash scripts/build-deb.sh; \
    find dist -maxdepth 1 -type f -name 'workbuddy_*.deb' -print -quit | grep -q .

FROM ${WEBTOP_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=workbuddy-builder /src/workbuddy-linux/dist/workbuddy_*.deb /tmp/workbuddy.deb
COPY --from=clawmanager-agent-builder /out/clawmanager-agent /usr/local/bin/clawmanager-agent

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      /tmp/workbuddy.deb \
      dbus-x11 \
      xdg-utils; \
    rm -f /tmp/workbuddy.deb; \
    rm -rf /var/lib/apt/lists/*

COPY workbuddy/start-workbuddy /usr/local/bin/start-workbuddy
COPY workbuddy/s6-workbuddy-run /etc/s6-overlay/s6-rc.d/svc-workbuddy/run
COPY workbuddy/s6-workbuddy-agent-run /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/run
COPY workbuddy/startwm-x11.sh /defaults/startwm.sh

RUN set -eux; \
    sed -i 's/\r$//' \
      /usr/local/bin/start-workbuddy \
      /etc/s6-overlay/s6-rc.d/svc-workbuddy/run \
      /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/run \
      /defaults/startwm.sh; \
    chmod 0755 /usr/local/bin/start-workbuddy; \
    chmod 0755 /usr/local/bin/clawmanager-agent; \
    chmod 0755 /etc/s6-overlay/s6-rc.d/svc-workbuddy/run; \
    chmod 0755 /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/run; \
    chmod 0755 /defaults/startwm.sh; \
    mkdir -p \
      /etc/s6-overlay/s6-rc.d/svc-workbuddy/dependencies.d \
      /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/dependencies.d \
      /etc/s6-overlay/s6-rc.d/user/contents.d; \
    printf 'longrun\n' > /etc/s6-overlay/s6-rc.d/svc-workbuddy/type; \
    printf 'longrun\n' > /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/type; \
    touch /etc/s6-overlay/s6-rc.d/svc-workbuddy/dependencies.d/svc-de; \
    touch /etc/s6-overlay/s6-rc.d/svc-workbuddy-agent/dependencies.d/init-services; \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/svc-workbuddy; \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/svc-workbuddy-agent; \
    rm -f /usr/share/dbus-1/system-services/org.freedesktop.UDisks2.service

ENV PUID=1000 \
    PGID=1000 \
    TZ=Asia/Shanghai \
    START_DOCKER=false \
    PIXELFLUX_WAYLAND=false \
    CLAWMANAGER_AGENT_RUNTIME_TYPE=workbuddy \
    CLAWMANAGER_AGENT_PERSISTENT_DIR=/config \
    CLAWMANAGER_AGENT_SKILL_DIRS=/config/.workbuddy/skills \
    WORKBUDDY_MODEL_CONFIG_PATH=/config/.workbuddy/models.json \
    TITLE="WorkBuddy Linux (Unofficial POC)"

LABEL org.opencontainers.image.title="WorkBuddy Linux Webtop (Unofficial POC)" \
      org.opencontainers.image.description="Unofficial WorkBuddy Linux conversion running in a Webtop KDE desktop" \
      org.opencontainers.image.source="https://github.com/JipZeonGit/workbuddy-linux" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary-WorkBuddy"

EXPOSE 3000 3001
