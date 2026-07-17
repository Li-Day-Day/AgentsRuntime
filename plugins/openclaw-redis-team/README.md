# OpenClaw Redis Team Plugin

This plugin connects an OpenClaw runtime managed by ClawManager to a Redis Streams based Team Bus.

## Capabilities

- Starts a background Redis Streams consumer when Team env is present.
- Exposes `team_send` for assigning work to another team member.
- Exposes `team_status` for reading member status snapshots from the shared Team directory.
- Exposes `team_update_progress` and `team_complete_task` for structured progress/result reporting.
- Exposes `team_artifact_write`, `team_artifact_read`, `team_artifact_list`, and
  `team_artifact_mkdir` for current-Team scoped artifact operations.
- Writes small events to Redis and writes durable task/status/result files under the shared Team directory.
- Attempts to run inbound tasks through OpenClaw embedded agent runtime helper when available.
- Persists one member-scoped active assignment lease under the private runtime
  state directory so tool execution can recover the authenticated Redis
  envelope even when OpenClaw runs tools in a different plugin instance.

## Required environment

```text
CLAWMANAGER_TEAM_ENABLED=true
CLAWMANAGER_TEAM_ID=team_xxx
CLAWMANAGER_TEAM_MEMBER_ID=developer
CLAWMANAGER_TEAM_ROLE=developer
CLAWMANAGER_TEAM_REDIS_URL=redis://redis:6379/0
CLAWMANAGER_TEAM_SHARED_DIR=/team
```

In pooled Lite runtimes, `/team` is a canonical link returned to ClawManager,
not a container-global mount. Artifact tools resolve the physical current-Team
directory, reject traversal and symlink escapes, and create cooperative
`2775` directories and `0664` files.

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

Redis Team protocol v3 keeps the original wire schema (`v: 1`) for older
ClawManager releases and adds a backend acknowledgement handshake. A normal
assistant reply or successful agent turn is progress, not task completion. An
explicit `team_complete_task` call emits `completion_proposed`; the runtime
marks the task terminal and locks the stable `completionId` only after
ClawManager returns an `accepted` acknowledgement. Deferred and rejected
attempts keep the assignment retryable. Runtime errors still emit a structured
`task_failed` event.

Task/work ids supplied by the model are aliases only. The authenticated Redis
envelope is authoritative. When no alias is supplied, the plugin first uses the
private active-assignment lease and then, for backward compatibility, the one
non-terminal task named by the current member status. It never scans sibling
Teams or guesses between multiple assignments. Once an assignment is terminal,
late progress and stale completion acknowledgements cannot return its local
status to running.

Every explicit completion atomically writes `results/<taskId>/result.json` and
`results/<taskId>/result.md`, even when the caller only provides a summary. The
proposal carries a deterministic `completionId`, a per-attempt `attemptId`,
`completionSource`, `explicitCompletion`, workflow/ledger versions, and
canonical `/team/...` artifact references. Leader completions additionally
declare `workflowFinal`, `finalAnswerReady`, and `remainingActions`. These
additive fields allow ClawManager to prevent a completed collection phase from
prematurely closing a multi-stage root task.

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
