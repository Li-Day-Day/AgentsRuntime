---
name: redis-team-protocol
description: ClawManager Redis Team Bus collaboration protocol for Hermes runtime members.
version: 2.0.0
metadata:
  hermes:
    source: bundled_by_agentsruntime
    skill_id: redis-team-protocol
---

# ClawManager Redis Team Protocol v2

Use the Redis Team tools for every task delivered through the `redis_team`
platform. The ClawManager control plane is authoritative for task state. Redis
Streams carries events, while the shared workspace carries durable artifacts.

## Workspace contract

- Read the shared root from `CLAWMANAGER_TEAM_SHARED_DIR`; do not assume its
  physical path is `/team`.
- Prefer `team_artifact_write`, `team_artifact_read`, `team_artifact_list`, and
  `team_artifact_mkdir`. They constrain paths to the current Team and enforce
  cooperative file permissions.
- Member deliverables belong below
  `/team/artifacts/<rootTaskId>/members/<memberId>/`; only the Leader writes the
  root final synthesis.
- Report artifact links with the canonical `/team/<relative-path>` prefix.
- In pooled Lite runtimes, `/team` is a canonical report prefix rather than a
  container-global mount. Use the resolved physical path or the artifact tools.
- Never write API keys, Redis credentials, or team tokens into shared files.

## Event semantics

- A normal assistant response is a `reply`. It never completes a task.
- `team_update_progress` publishes non-terminal progress only. Do not report a
  terminal status through this tool.
- A successful model turn is not proof that the business task is complete.
- `team_complete_task` is the only successful completion authority. Call it
  exactly once after the requested result is ready.
- Runtime failures may publish `task_failed`; ordinary empty or intermediate
  replies must not be converted into success or failure.

## Completion sequence

1. Perform the assigned work and periodically call `team_update_progress` with
   `running`, `blocked`, `waiting_review`, or `waiting_completion` when useful.
2. Write durable artifacts under the shared results directory when the task
   requires files.
3. Call `team_complete_task` with `taskId`, `status`, `summary`,
   `resultMarkdown`, and any `artifactRefs`.
4. Do not send a second copy of the result after completion. ClawManager's
   reliable confirmation outbox notifies the Leader.

Do not call `team_update_progress(progress=100)` before completion. The
completion tool records terminal progress after result files are durable.

## Leader-mediated collaboration

- User root tasks enter through the Leader.
- Every delegated business assignment must use a stable `workId` (and the same
  `assignmentId`) in `team_send`. Reuse that ID in progress, result, and review
  messages so ClawManager can project one business card instead of counting
  transport events.
- If the user names a member, the Leader delegates to that exact member and
  waits for the real result.
- Broad tasks are decomposed into owned assignments; the Leader reconciles all
  required outputs before final synthesis.
- Workers do not hand work directly to other workers in leader-mediated mode.
- A worker completion closes only its assignment. Only the Leader can complete
  the root task after required outputs and review evidence are present.
- The Leader may answer a genuinely small, self-contained control-plane task
  directly when no named worker or multi-member evidence is required.

## Result quality

- Preserve `rootTaskId`, `rootMessageId`, task IDs, and artifact references from
  the inbound envelope.
- Report concrete results and blockers, not only statements such as "done" or
  "result delivered".
- Do not invent missing peer replies, status, files, or verification evidence.
- A dispatch, plan, handoff, or process narration is not a final result.
- Follow the inbound `responseLocale` for every user-visible plan, progress
  summary, assignment, result, and synthesis. Preserve code and technical names.
