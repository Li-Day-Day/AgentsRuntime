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

## Extending

Read [Runtime Profile Extension Guide](docs/runtime-profile-extension.md) before adding a new runtime profile.
