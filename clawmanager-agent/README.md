# ClawManager Agent

`clawmanager-agent` is the shared managed-runtime agent for ClawManager runtime images.

It owns the common runtime pod control-plane behavior:

- register and heartbeat with ClawManager runtime-agent APIs
- expose local runtime control endpoints
- create, stop, and report gateway processes
- allocate ports and prepare workspaces
- report metrics, gateway state, and skill inventory

Runtime-specific behavior is selected by `CLAWMANAGER_RUNTIME_TYPE` through runtime profiles. The current built-in profiles are:

- `openclaw`
- `openclaw-shell`
- generic fallback for explicitly configured runtime types

Unknown runtime types are accepted only when `RUNTIME_GATEWAY_COMMAND` is set. This prevents accidental startup with unsafe or guessed defaults.

## Build

```bash
go build ./cmd/clawmanager-agent
```

Runtime images should install the binary at:

```text
/usr/local/bin/clawmanager-agent
```

## Key Environment Variables

- `CLAWMANAGER_RUNTIME_TYPE`: runtime profile selector
- `RUNTIME_AGENT_CONTROL_TOKEN`: token accepted by the local control server
- `RUNTIME_AGENT_REPORT_TOKEN`: token used to report to ClawManager
- `CLAWMANAGER_BACKEND_URL`: ClawManager gateway/API base URL
- `CLAWMANAGER_RUNTIME_IMAGE_REF`: image reference reported to ClawManager
- `RUNTIME_GATEWAY_COMMAND`: required for unknown or generic runtime types
- `RUNTIME_WORKSPACE_ROOT`: workspace root, defaults to the selected profile
- `RUNTIME_AGENT_DATA_DIR`: local agent data directory
- `CLAWMANAGER_OPENCLAW_NPM_RUNTIME_MODE`: OpenClaw npm bootstrap mode. The
  default `shared` mode creates instance-owned package directories with
  read-only links to image-provided packages, avoiding a full `node_modules`
  copy for every gateway. Set it to `copy` to restore the legacy behavior.

The shared npm mode applies only when a new instance npm directory is seeded.
Existing instance npm directories are preserved. Image-provided packages are
read-only through their links; installing or replacing a package writes an
instance-owned override without changing the image defaults.

Gateway state changes trigger an immediate report to ClawManager. An OpenClaw
instance becomes `running` only after its existing gateway health check passes;
required config and plugin registry preparation still finish before launch.

## Extending

Read [Runtime Profile Extension Guide](docs/runtime-profile-extension.md) before adding a new runtime profile.

Read [OpenClaw Lite Channel Adapter Guide](docs/openclaw-lite-channel-adaptation.md)
before adding or upgrading a channel plugin in the Lite image. It documents the
shared npm layout, cross-UID permissions, Node peer resolution, readiness
boundary, rollout checks, and known failure modes.
