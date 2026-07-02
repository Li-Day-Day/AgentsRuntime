# OpenClaw Redis Team Plugin

This plugin connects an OpenClaw runtime managed by ClawManager to a Redis Streams based Team Bus.

## Capabilities

- Starts a background Redis Streams consumer when Team env is present.
- Exposes `team_send` for assigning work to another team member.
- Exposes `team_status` for reading member status snapshots from the shared Team directory.
- Exposes `team_update_progress` and `team_complete_task` for structured progress/result reporting.
- Writes small events to Redis and writes durable task/status/result files under the shared Team directory.
- Attempts to run inbound tasks through OpenClaw embedded agent runtime helper when available.

## Required environment

```text
CLAWMANAGER_TEAM_ENABLED=true
CLAWMANAGER_TEAM_ID=team_xxx
CLAWMANAGER_TEAM_MEMBER_ID=developer
CLAWMANAGER_TEAM_ROLE=developer
CLAWMANAGER_TEAM_REDIS_URL=redis://redis:6379/0
CLAWMANAGER_TEAM_SHARED_DIR=/team
```

Optional:

```text
CLAWMANAGER_TEAM_AUTORUN=true
CLAWMANAGER_TEAM_CONSUMER_GROUP=team-members
CLAWMANAGER_TEAM_INBOX_KEY=claw:team:<teamId>:inbox:<memberId>
CLAWMANAGER_TEAM_EVENTS_KEY=claw:team:<teamId>:events
CLAWMANAGER_TEAM_PRESENCE_KEY=claw:team:<teamId>:presence
CLAWMANAGER_TEAM_DLQ_KEY=claw:team:<teamId>:dlq
CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS=1800
CLAWMANAGER_TEAM_MANAGER_URL=http://clawmanager:8080
CLAWMANAGER_TEAM_TOKEN=...
```

When autorun is enabled, inbound Redis tasks are started through OpenClaw's
embedded agent helper. The plugin passes the configured
`agents.defaults.model.primary` selection, such as `auto/auto`, into the
embedded run so ClawManager-injected gateway URL and token settings are reused.
It reads the current runtime config through `api.runtime.config.current()` and
passes that config into `runEmbeddedAgent`, so embedded auth sees the same
`models.providers.auto.apiKey` as the OpenClaw gateway.
The plugin emits `task_failed` if the helper is unavailable or does not return
before `CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS`, so ClawManager does not leave
the task in `running` forever.
When ClawManager provides explicit stream keys, the plugin uses those keys
instead of deriving them from `CLAWMANAGER_TEAM_ID`. Events include
`task_received`, `task_started`, and structured progress events with both
camelCase and snake_case id fields.

## Completion protocol

Redis Team protocol v2 keeps the original wire schema (`v: 1`) for older
ClawManager releases and adds `protocolVersion: 2`. A normal assistant reply or
successful agent turn is progress, not task completion. Only an explicit
`team_complete_task` call emits a successful `task_completed` event. Runtime
errors emit a structured `task_failed` event.

Every explicit completion atomically writes `results/<taskId>/result.json` and
`results/<taskId>/result.md`, even when the caller only provides a summary. The
event carries a deterministic `completionId`, `completionSource`,
`explicitCompletion`, and canonical `/team/...` artifact references. These
additive fields allow new ClawManager releases to enforce idempotent completion
while old releases continue to consume the familiar event names and fields.

## Redis keys

```text
claw:team:<teamId>:inbox:<memberId>
claw:team:<teamId>:events
claw:team:<teamId>:presence
claw:team:<teamId>:dlq
```

## Packaging for AgentsRuntime

`plugins/openclaw-redis-team` is the canonical source. The image copies this
directory into its build context, runs `npm pack`, and installs the generated
archive. Prepacked archives under `openclaw/vendor-plugins` remain only for
older build consumers and are not authoritative.
