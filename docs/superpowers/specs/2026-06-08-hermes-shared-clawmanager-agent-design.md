# Hermes Shared ClawManager Agent Design

## Goal

Implement Hermes managed runtime support through the shared `clawmanager-agent`, without creating or maintaining a separate Hermes runtime agent.

The work has two related outcomes:

- Add a Hermes Runtime Pod Agent profile so one runtime Pod can create and manage many `hermes gateway` child processes through the existing `/v1/gateways` control plane.
- Move the current Hermes instance-agent capabilities from `hermes/internal/agent` into `clawmanager-agent`, so Hermes desktop and Hermes gateway images use the common agent binary for ClawManager integration.

## Non-Goals

- Do not build a second Hermes-specific pod agent.
- Do not duplicate the existing `clawmanager-agent` gateway manager, port allocator, report client, control HTTP server, metrics collector, or workspace validator.
- Do not change Hermes desktop behavior as part of the gateway runtime work.
- Do not make `/config/.hermes` the workspace for pooled gateway instances.
- Do not keep evolving `hermes-agent` as a separate production binary after its capabilities move into `clawmanager-agent`.

## Current State

OpenClaw has two agent paths in the image:

- `openclaw-agent` manages the desktop or single-instance path. It uses `/config`, Webtop, browser launch, and the instance-level `/api/v1/agent/*` protocol.
- `clawmanager-agent` manages the shared Runtime Pod path. It uses `RUNTIME_AGENT_CONTROL_TOKEN` and `RUNTIME_AGENT_REPORT_TOKEN`, exposes `/v1/gateways`, allocates ports, prepares `/workspaces/{runtime}/user-{user_id}/instance-{instance_id}`, and starts many runtime gateway child processes.

Hermes currently has its own instance agent under `hermes/internal/agent`. It handles registration, heartbeat, state reports, commands, skill inventory, and skill package upload for the desktop-style Hermes image. It is not part of `clawmanager-agent`.

The target state is that Hermes uses `clawmanager-agent` for both the shared Runtime Pod control plane and Hermes instance-agent control plane behavior.

## Architecture

### Shared Agent Modes

`clawmanager-agent` will support two modes behind the same binary:

1. Runtime Pod Agent mode
   - Triggered by `RUNTIME_AGENT_CONTROL_TOKEN` or `RUNTIME_AGENT_REPORT_TOKEN`.
   - Existing behavior remains the source of truth for pod registration, heartbeat, metrics, `/v1/gateways`, drain, gateway reports, and pod-level skill reports.
   - Hermes support is added through a runtime profile.

2. Instance Agent mode
   - Triggered by `CLAWMANAGER_AGENT_ENABLED=true` and a supported runtime type such as `hermes`.
   - Implements `/api/v1/agent/*` outbound behavior: register, heartbeat, state report, command polling, skill inventory, and skill package upload.
   - Uses runtime-specific adapters for process state, system info shape, skill paths, command handling, and bootstrap config.

If both mode families are configured in the same process environment, Runtime Pod Agent mode wins. This prevents a shared runtime Pod from being misreported as one instance.

### Package Boundaries

`clawmanager-agent/internal/agent` stays focused on Runtime Pod Agent mode.

New instance-agent code should live separately:

```text
clawmanager-agent/internal/instanceagent/
  agent.go          common loop and orchestration
  client.go         /api/v1/agent HTTP client
  config.go         shared instance-agent env loading
  session.go        session cache handling
  types.go          protocol payloads
  commands.go       command lifecycle helpers
  skills.go         reusable skill scan/package helpers
  system.go         reusable system metric helpers

clawmanager-agent/internal/instanceagent/hermes/
  adapter.go        Hermes adapter implementation
  config.go         Hermes defaults and env aliases
  skills.go         Hermes skill inventory rules
  system.go         Hermes runtime state collection
  commands.go       Hermes command handling
```

The existing `hermes/internal/agent` code is the migration source, not a long-term dependency.

### Runtime Profile

Add:

```text
clawmanager-agent/internal/runtime/hermes/
```

The Hermes profile implements `gateway.RuntimeProfile` and is registered in `clawmanager-agent/internal/agent/config.go`.

Profile responsibilities:

- Runtime type: `hermes`.
- Display name: `Hermes`.
- Defaults: workspace root `/workspaces`, port range `20000-20099`, capacity `100`, block size `1`, trusted-proxy auth mode, startup timeout around `90s`.
- Gateway command: start a single Hermes gateway process. Prefer a thin script if Hermes needs env normalization before `hermes gateway`.
- Gateway env: set standard ClawManager env, `HOME={workspace}/home`, `HERMES_HOME={workspace}/home/.hermes`, `HOST=0.0.0.0`, `PORT={allocated_port}`, and only whitelisted LLM env from the create request.
- Workspace prep: use the shared workspace validator and creator, then create Hermes-specific directories under `{workspace}/home/.hermes`.
- Config writer: write Hermes config inside `{workspace}/home/.hermes`, preserving user fields and injecting ClawManager-controlled LLM, model, proxy, gateway, and skill settings.
- Health checker: use the shared HTTP/TCP health checker unless Hermes requires a specific endpoint or header.

### Gateway Workspace

Pooled Hermes gateway instances must use:

```text
/workspaces/hermes/user-{user_id}/instance-{instance_id}/
  home/
    .hermes/
      config.yaml
      .env
      gateway.json
      skills/
```

The pooled runtime must not use `/config/.hermes`, because `/config` belongs to the desktop or single-instance image path.

### Hermes Config Injection

The config writer should reuse the current Hermes env contract where practical:

- LLM model variables: `CLAWMANAGER_LLM_MODEL`, `OPENAI_MODEL`.
- LLM provider variables: `CLAWMANAGER_LLM_PROVIDER`.
- Base URL variables: `CLAWMANAGER_LLM_BASE_URL`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`.
- API key variables: `CLAWMANAGER_LLM_API_KEY`, `OPENAI_API_KEY`.
- Bootstrap resources: `CLAWMANAGER_HERMES_*`, `CLAWMANAGER_RUNTIME_*`, and legacy `CLAWMANAGER_OPENCLAW_*` aliases.

Sensitive values must be written with restrictive permissions and must not be logged.

If `hermes-apply-runtime-config` remains useful, make it work from `HERMES_HOME` so both desktop and pooled gateway paths can use it. Do not leave hidden `/config/.hermes` assumptions in the pooled gateway path.

### Skill Reporting

Runtime Pod Agent skill reports must include Hermes skill roots:

```text
{workspace}/skills
{workspace}/home/.hermes/skills
```

Instance Agent mode for Hermes should preserve the existing behavior from `hermes/internal/agent`:

- Scan `HERMES_SKILL_DIRS`, defaulting to Hermes skill directories.
- Compute `content_md5` using the existing directory-content rules.
- Upload skill packages for `collect_skill_package`.
- Support full inventory for `sync_skill_inventory` and `refresh_skill_inventory`.

### Commands

Hermes Instance Agent mode should preserve these currently implemented command behaviors:

- `collect_system_info`
- `health_check`
- `sync_skill_inventory`
- `refresh_skill_inventory`
- `collect_skill_package`

Compatibility commands `start_openclaw`, `stop_openclaw`, and `restart_openclaw` should remain harmless for Hermes unless the platform later maps them to Hermes-specific lifecycle semantics.

Unknown commands must finish as `failed` with `unsupported command type: <type>`.

### Image Wiring

Hermes images should use `/usr/local/bin/clawmanager-agent` for ClawManager integration.

For the desktop image:

- Keep Webtop, autostart, and desktop behavior intact.
- Replace the s6 `hermes-agent` service target with `/usr/local/bin/clawmanager-agent` in Instance Agent mode.
- Do not make desktop startup depend on Runtime Pod Agent tokens.

For pooled Hermes gateway runtime images:

- Install `/usr/local/bin/clawmanager-agent`.
- Set `CLAWMANAGER_RUNTIME_TYPE=hermes`.
- Start `clawmanager-agent` only when Runtime Pod Agent tokens are present.
- Do not start a global `hermes-gateway` service that occupies one fixed instance path.

The Dockerfile strategy can be either one image with guarded services or a separate gateway-only Dockerfile. The implementation plan should choose the least disruptive path after checking current CI image discovery and release expectations.

## Error Handling

- Missing Runtime Pod tokens keep the Runtime Pod Agent service sleeping, matching current `clawmanager-agent-run`.
- Missing Instance Agent env disables Instance Agent mode or fails fast with a clear configuration error when explicitly enabled.
- Gateway creation remains idempotent by `instance_id + generation`.
- Higher generation replaces older local generation for the same instance.
- Missing Hermes LLM token must prevent a gateway from being marked `running`; it should report `error` with a short non-sensitive message.
- Config write failure releases the allocated port and reports gateway `error`.
- Hermes command execution must call command finish even on failure.

## Testing

Unit tests should cover:

- `CLAWMANAGER_RUNTIME_TYPE=hermes` selects the Hermes runtime profile.
- Hermes profile defaults are `20000-20099`, capacity `100`, block size `1`, trusted-proxy auth.
- Hermes gateway env sets `HOME`, `HERMES_HOME`, `HOST`, `PORT`, and forwards only whitelisted LLM env.
- Hermes config writer writes under `{workspace}/home/.hermes`, not `/config/.hermes`.
- Hermes config writer preserves unknown user config fields and does not log secrets.
- Runtime Pod skill scan includes `{workspace}/home/.hermes/skills`.
- Instance Agent mode loads Hermes env and session state.
- Hermes instance heartbeat and state report preserve current protocol fields, including `openclaw_status` compatibility fields.
- Hermes skill inventory and `collect_skill_package` behavior match existing tests.
- Mode selection prefers Runtime Pod Agent mode when both token families are present.

Verification commands:

```text
go test ./...      # from clawmanager-agent/
go test ./...      # from hermes/, while migration compatibility tests still exist
```

Docker verification should at least confirm build context paths and service scripts reference `/usr/local/bin/clawmanager-agent`.

## Migration Steps

1. Add failing tests for Hermes runtime profile and mode selection in `clawmanager-agent`.
2. Add `internal/runtime/hermes` and register it.
3. Add Hermes gateway env and config writer behavior.
4. Extend pod-level skill scanning for Hermes roots.
5. Introduce `internal/instanceagent` and migrate reusable protocol/client/session/loop behavior from `hermes/internal/agent`.
6. Add the Hermes instance-agent adapter and port existing Hermes tests into `clawmanager-agent`.
7. Rewire Hermes image service scripts to use `clawmanager-agent`.
8. Remove or deprecate the standalone `hermes-agent` build path only after shared-agent tests pass.
9. Update docs to state that Hermes integration uses the shared `clawmanager-agent`.

## Acceptance Criteria

- `clawmanager-agent` can run Runtime Pod Agent mode for `hermes`.
- A Hermes Runtime Pod can create multiple independent `hermes gateway` child processes with unique ports and isolated workspaces.
- Hermes desktop behavior is unchanged except that ClawManager instance-agent integration uses the shared binary.
- Hermes instance-agent capabilities from the old `hermes-agent` are available through `clawmanager-agent`.
- No separate Hermes pod agent or Hermes instance agent remains as the production integration path.
- Tests pass for the shared agent and the migrated Hermes behavior.
