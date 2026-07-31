# Hermes Redis Team Plugin

Hermes gateway platform adapter for the ClawManager Redis Streams Team Bus.

This plugin is the Hermes-side counterpart of `plugins/openclaw-redis-team`.
It does not use the OpenClaw plugin SDK. Instead, it registers a Hermes
platform named `redis_team` and the shared team tools:

- `team_artifact_write`
- `team_artifact_read`
- `team_artifact_list`
- `team_artifact_mkdir`
- `team_artifact_preview`
- `team_send`
- `team_status`
- `team_update_progress`
- `team_complete_task`

## Runtime contract

The adapter reads the same Team env contract used by the OpenClaw runtime:

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
CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS=1800
CLAWMANAGER_TEAM_MANAGER_URL=http://clawmanager:8080
CLAWMANAGER_TEAM_TOKEN=...
CLAWMANAGER_TEAM_PREVIEW_ORIGIN=http://clawmanager-egress-proxy.<namespace>.svc.cluster.local:3128
```

## Redis keys

```text
claw:team:<teamId>:inbox:<memberId>
claw:team:<teamId>:events
claw:team:<teamId>:presence
claw:team:<teamId>:dlq
```

The adapter emits the current Worker-side protocol-v4 completion proposal and
waits briefly for ClawManager's completion acknowledgement. A substantive
Hermes final response is proposed automatically, while Monitor/status turns and
empty or question-only responses remain non-terminal. ClawManager remains the
authority that accepts or rejects completion.

## AgentsRuntime packaging

`plugins/hermes-redis-team` is the canonical source and is copied directly into
the Hermes image. The historical `hermes/vendor-plugins/redis_team` directory is
not used by current image builds.

