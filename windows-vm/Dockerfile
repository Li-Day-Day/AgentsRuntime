# syntax=docker/dockerfile:1.7

ARG WINDOWS_BASE_IMAGE=dockurr/windows:latest

FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS clawmanager-agent-builder

ARG TARGETOS=linux
ARG TARGETARCH

WORKDIR /src/clawmanager-agent
COPY clawmanager-agent ./
RUN go mod download
RUN set -eux; \
  arch="${TARGETARCH:-amd64}"; \
  os="${TARGETOS:-linux}"; \
  CGO_ENABLED=0 GOOS="${os}" GOARCH="${arch}" \
    go build -trimpath -ldflags="-s -w" \
    -o /out/clawmanager-agent ./cmd/clawmanager-agent

FROM ${WINDOWS_BASE_IMAGE}

COPY --from=clawmanager-agent-builder /out/clawmanager-agent /usr/local/bin/clawmanager-agent
COPY windows-vm/windows-vm-entrypoint /usr/local/bin/windows-vm-entrypoint
COPY windows-vm/windows-vm-gateway /usr/local/bin/windows-vm-gateway

RUN chmod 0755 \
  /usr/local/bin/clawmanager-agent \
  /usr/local/bin/windows-vm-entrypoint \
  /usr/local/bin/windows-vm-gateway

ENV CLAWMANAGER_RUNTIME_TYPE=windows-vm \
    RUNTIME_GATEWAY_PORT_START=8006 \
    RUNTIME_GATEWAY_PORT_END=8006 \
    RUNTIME_GATEWAY_PORT_BLOCK_SIZE=1 \
    RUNTIME_GATEWAY_CAPACITY=1 \
    RUNTIME_GATEWAY_STARTUP_HEALTH_TIMEOUT=5m \
    RUNTIME_GATEWAY_STOP_TIMEOUT=2m

EXPOSE 3389 8006 19090

ENTRYPOINT ["/usr/bin/tini", "-s", "/usr/local/bin/windows-vm-entrypoint"]
