# ClawManager Agent Runtime Profile Refactor Design

## Context

The current managed runtime agent code lives under `openclaw/internal/runtimeagent`. That makes the agent appear OpenClaw-specific even though the behavior is intended to be shared by OpenClaw, OpenClaw Shell, Hermes v2, and future managed runtimes.

Hermes currently contains an older v1 agent implementation. This refactor must not use that Hermes implementation as the behavior source. The only behavior baseline for the new shared agent is the runtime-agent code currently under OpenClaw.

## Goals

- Move the runtime-agent implementation into a top-level `clawmanager-agent/` component beside `openclaw/`, `hermes/`, and `openclaw-shell/`.
- Build a standalone `/usr/local/bin/clawmanager-agent` binary that runtime images can copy into their final image.
- Keep OpenClaw runtime bootstrapping in `openclaw-agent`; do not keep the shared runtime control plane inside the OpenClaw-specific binary.
- Add a runtime profile architecture so runtime-specific config, workspace, command, env, health, and config writer behavior is selected by runtime type.
- Provide an extension specification so adding a new runtime type is mostly adding a profile plus image wiring.
- Update Docker build contexts, CI manifests, and docs so runtime images can build from the shared component.

## Non-Goals

- Do not port, reuse, or refactor the current Hermes v1 agent implementation.
- Do not implement Hermes-specific v2 behavior beyond making the shared agent architecture ready for a Hermes profile.
- Do not change ClawManager backend APIs in this refactor.
- Do not rewrite the existing OpenClaw bootstrap/supervisor agent except to remove shared runtime-agent mode from it.

## Recommended Approach

Use a shared agent core with runtime-specific profiles:

- The shared core owns lifecycle, registration, heartbeat, metrics, gateway process management, port allocation, control HTTP server, and reporting.
- Runtime profiles own defaults and runtime-specific adaptation.
- A generic profile provides a minimal fallback for future runtimes that can be driven by env and `RUNTIME_GATEWAY_COMMAND`.
- OpenClaw and OpenClaw Shell use an OpenClaw profile that preserves the current OpenClaw gateway config injection behavior.

This is preferred over keeping runtime-specific copies because it avoids drift, and preferred over one large config switch because new runtime behavior remains isolated in small profile packages.

## Proposed Directory Layout

```text
clawmanager-agent/
  go.mod
  go.sum
  README.md
  docs/
    runtime-profile-extension.md
  cmd/
    clawmanager-agent/
      main.go
  internal/
    agent/
      agent.go
      config.go
    control/
      client.go
      server.go
    gateway/
      health.go
      manager.go
      metrics.go
      ports.go
      process_unix.go
      process_windows.go
      skills.go
      workspace.go
      workspace_unix.go
      workspace_windows.go
    runtime/
      profile.go
      registry.go
      generic/
        profile.go
      openclaw/
        config_writer.go
        profile.go
```

OpenClaw-specific runtime bootstrap remains in:

```text
openclaw/
  cmd/openclaw-agent/
  internal/bootstrap/
  internal/browser/
  internal/config/
  internal/supervisor/
```

The old `openclaw/internal/runtimeagent` package is removed after its logic has moved into `clawmanager-agent/`.

## Runtime Profile Contract

The profile interface is the extension boundary:

```go
type RuntimeProfile interface {
    Type() string
    DisplayName() string
    Defaults() RuntimeDefaults
    GatewayCommand(authMode string) []string
    GatewayEnv(base []string, cfg Config, req CreateGatewayRequest, workspacePath string, port int) []string
    PrepareWorkspace(cfg Config, req CreateGatewayRequest, workspacePath string) error
    WriteGatewayConfig(cfg Config, req CreateGatewayRequest, workspacePath string) error
    HealthChecker(cfg Config) GatewayHealthChecker
}
```

`RuntimeDefaults` contains stable defaults:

```go
type RuntimeDefaults struct {
    WorkspaceRoot string
    AgentDataDir string
    GatewayPortStart int
    GatewayPortEnd int
    GatewayPortBlockSize int
    GatewayCapacity int
    GatewayAuthMode string
    GatewayStartupTimeout time.Duration
}
```

The agent loads `CLAWMANAGER_RUNTIME_TYPE`, looks up a profile, and merges config in this order:

1. Platform-injected env.
2. Runtime profile defaults.
3. Generic fallback defaults.

Unknown runtime types are allowed only when enough env is supplied to run safely, especially `RUNTIME_GATEWAY_COMMAND`.

## Profile Behavior

`runtime/generic`:

- Validates workspace paths under the configured workspace root.
- Creates per-instance workspace and home directories.
- Starts the configured gateway command.
- Injects standard ClawManager and OpenAI-compatible gateway env.
- Does not write runtime-specific config files.
- Uses an HTTP health checker only when a health URL template or port-based health path is configured; otherwise uses a no-op startup health check.

`runtime/openclaw`:

- Supports `openclaw` and `openclaw-shell` runtime types.
- Keeps the current OpenClaw trusted-proxy and token auth config behavior.
- Writes `home/.openclaw/openclaw.json` inside the gateway workspace.
- Injects OpenClaw gateway env names such as `OPENCLAW_HOST`, `OPENCLAW_PORT`, and `OPENCLAW_GATEWAY_PORT`.
- Preserves LLM provider injection from ClawManager env and request env.

Future runtime profiles:

- Must be small packages under `internal/runtime/<type>/`.
- Must not duplicate lifecycle, HTTP client, server, metrics, port, or gateway manager logic.
- Must only implement runtime-specific defaults and adaptation.

## Image Integration

Runtime images that need the shared agent must build from a context that can access `clawmanager-agent/`.

OpenClaw:

- Change `openclaw/Dockerfile.openclaw` to expect repository-root context.
- Build `clawmanager-agent/cmd/clawmanager-agent`.
- Copy `/out/clawmanager-agent` into `/usr/local/bin/clawmanager-agent`.
- Continue to build/copy OpenClaw-specific `/usr/local/bin/openclaw-agent`.
- Add an s6 service or entrypoint hook for `clawmanager-agent` when the runtime image needs pooled runtime gateway management.

OpenClaw Shell:

- Build `clawmanager-agent` from `clawmanager-agent/` instead of reusing OpenClaw's `openclaw-agent` implementation.
- Keep the shell entrypoint responsible for shell setup and then run the appropriate runtime process.

Hermes:

- Do not use its old v1 agent implementation as a source.
- Future Hermes v2 image integration should copy `/usr/local/bin/clawmanager-agent` and provide either a `hermes` profile or enough env for the generic profile.

CI:

- Runtime image manifests must set `context` to `.` for images that need the shared component.
- Dockerfiles must use paths relative to repository root when context is `.`.

## Extension Specification

The refactor must create `clawmanager-agent/docs/runtime-profile-extension.md` with:

- A checklist for adding a new runtime type.
- The `RuntimeProfile` interface contract.
- Config precedence rules.
- Required environment variables and optional overrides.
- Workspace, data directory, and skill inventory directory conventions.
- Dockerfile snippets for building and copying `clawmanager-agent`.
- `.runtime-image.json` snippets for root-context builds.
- Test requirements for profile config, workspace validation, gateway env, config writer, and health behavior.
- A copyable profile skeleton.
- Acceptance criteria for a new runtime profile.

Minimum new runtime steps:

1. Add `internal/runtime/<type>/profile.go`.
2. Implement or compose `RuntimeProfile`.
3. Register the profile in the runtime registry.
4. Add profile tests.
5. Update the runtime Dockerfile to build/copy `/usr/local/bin/clawmanager-agent`.
6. Update `.runtime-image.json` if the image needs repository-root context.
7. Run agent unit tests and a Docker build check.

## Testing Strategy

Unit tests:

- Config loading chooses the correct profile by `CLAWMANAGER_RUNTIME_TYPE`.
- OpenClaw and OpenClaw Shell preserve their current default commands and config writer behavior.
- Generic runtime requires `RUNTIME_GATEWAY_COMMAND` when no known profile exists.
- Gateway env injection forwards ClawManager LLM and OpenAI-compatible env.
- Workspace validation rejects paths outside the instance workspace.
- Registry rejects duplicate profile registration.

Integration-level static checks:

- `go test ./...` under `clawmanager-agent/`.
- `go test ./...` under `openclaw/` after removing the old runtime-agent mode.
- Dockerfiles reference build-context paths that exist.

## Risks

- Moving OpenClaw Docker build context from `./openclaw` to repository root can break existing local build commands. README and `.runtime-image.json` updates must make this explicit.
- OpenClaw currently has a combined binary path. Splitting `openclaw-agent` and `clawmanager-agent` requires careful service wiring so existing runtime startup still works.
- Unknown runtime fallback must not silently start with unsafe defaults. It should require explicit command and directories.

## Acceptance Criteria

- `clawmanager-agent/` exists as a standalone top-level component.
- Runtime-agent code no longer lives under `openclaw/internal/runtimeagent`.
- `openclaw-agent` no longer imports the shared runtime-agent package or runs runtime-agent mode.
- `clawmanager-agent` can build as a standalone binary.
- Runtime behavior can be selected by `CLAWMANAGER_RUNTIME_TYPE`.
- OpenClaw-specific config writing is isolated in an OpenClaw profile package.
- The extension specification exists and includes a copyable new-runtime template.
- Unit tests cover profile selection, defaults, env merging, workspace validation, and OpenClaw config writing.
- README and CI build metadata describe root-context builds where needed.
