import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const redisUrl = process.env.REDIS_TEAM_TEST_URL;
if (!redisUrl) throw new Error("REDIS_TEAM_TEST_URL is required");

const root = await fs.mkdtemp(path.join(os.tmpdir(), "redis-team-dispatch-"));
const state = path.join(root, "state");
const shared = path.join(root, "shared");
process.env.XDG_STATE_HOME = state;

const distPath = path.resolve(import.meta.dirname, "..", "dist", "index.js");
const source = (await fs.readFile(distPath, "utf8"))
  .replace('import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";', "const definePluginEntry = (entry) => entry;")
  .replace('import { dispatchInboundDirectDmWithRuntime } from "openclaw/plugin-sdk/direct-dm";', "const dispatchInboundDirectDmWithRuntime = async () => ({});");
const testSource = source + "\nexport { createRuntime, RedisClient, rootWorkflowStateKey };\n";
const pluginModule = await import(`data:text/javascript;base64,${Buffer.from(testSource).toString("base64")}`);

const teamId = "501";
const rootTaskId = "team-501-task-1";
const prefix = `claw:team:${teamId}`;
const config = {
  channels: {
    "redis-team": {
      accounts: {
        default: {
          fromEnv: false,
          enabled: true,
          redisUrl,
          teamId,
          memberId: "leader",
          role: "leader",
          sharedDir: shared,
        },
      },
    },
  },
};
const cfg = config.channels["redis-team"].accounts.default;
const runtime = pluginModule.createRuntime({ config, logger: { warn() {}, info() {}, error() {} } });
const redis = new pluginModule.RedisClient(redisUrl);

try {
  await fs.mkdir(shared, { recursive: true });
  await fs.writeFile(path.join(shared, "team.json"), JSON.stringify({
    teamId,
    communicationMode: "leader_mediated",
    members: [
      { memberId: "leader", role: "leader", isLeader: true },
      { memberId: "developer", role: "developer" },
      { memberId: "reviewer", role: "reviewer" },
    ],
  }), "utf8");
  await redis.connect();
  await redis.command(
    "DEL",
    `${prefix}:inbox:developer`,
    `${prefix}:inbox:reviewer`,
    `${prefix}:events`,
    `${prefix}:root:${rootTaskId}:state`,
    `${prefix}:root:${rootTaskId}:assignment-dispatch`,
    `${prefix}:root:${rootTaskId}:deferred:leader`,
    `${prefix}:deferred-roots:leader`,
  );

  const envelope = {
    v: 4,
    protocolVersion: 4,
    teamId,
    memberId: "leader",
    role: "leader",
    taskId: rootTaskId,
    rootTaskId,
    rootMessageId: "root-message-1",
    messageId: "root-message-1",
    assignmentId: "leader-final-synthesis",
    workId: "leader-final-synthesis",
    requiresCompletion: true,
  };

  await runtime.withActiveEnvelope(envelope, async () => {
    const developer = await runtime.send({
      to: "developer",
      text: "Implement the page.",
      intent: "assignment",
      assignmentId: "dev-page",
      workId: "dev-page",
      phaseId: "phase-dev",
      revision: 1,
      required: true,
    });
    assert.equal(developer.sent, undefined, "a normal dispatch keeps the established message envelope contract");
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:developer`)), 1);

    const deferred = await runtime.send({
      to: "reviewer",
      text: "Review the page after development.",
      intent: "assignment",
      assignmentId: "review-page",
      workId: "review-page",
      phaseId: "phase-review",
      revision: 1,
      required: true,
      dependsOn: ["dev-page"],
      validationAssignment: true,
      validationTargetAssignmentId: "dev-page",
      validationTargetRevision: 1,
    });
    assert.equal(deferred.sent, true);
    assert.equal(deferred.deferred, true);
    assert.equal(deferred.deliveryState, "registered_waiting_dependencies");
    assert.equal(deferred.noWorkerReplyExpectedUntilDependenciesReady, true);
    assert.match(deferred.leaderGuidance, /Do not resend/);
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:reviewer`)), 0, "a known dependent assignment is registered without starting the Worker");

    const repeatedWaiting = await runtime.send({
      to: "reviewer",
      text: "Review the page after development; include accessibility.",
      intent: "assignment",
      assignmentId: "review-page",
      workId: "review-page",
      phaseId: "phase-review",
      revision: 1,
      required: true,
      dependsOn: ["dev-page"],
      validationAssignment: true,
      validationTargetAssignmentId: "dev-page",
      validationTargetRevision: 1,
    });
    assert.equal(repeatedWaiting.deduplicated, true);
    assert.equal(repeatedWaiting.deferred, true);
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:reviewer`)), 0, "Leader retries must not create a hidden second execution");

    await redis.command("SET", pluginModule.rootWorkflowStateKey(cfg, rootTaskId), JSON.stringify({
      status: "running",
      terminal: false,
      assignments: {
        "dev-page": { assignmentId: "dev-page", revision: 1, status: "succeeded", updatedAt: new Date().toISOString() },
      },
    }));
    const releasedRequest = {
      to: "reviewer",
      text: "Review the page after development; include accessibility.",
      intent: "assignment",
      assignmentId: "review-page",
      workId: "review-page",
      phaseId: "phase-review",
      revision: 1,
      required: true,
      dependsOn: ["dev-page"],
      validationAssignment: true,
      validationTargetAssignmentId: "dev-page",
      validationTargetRevision: 1,
    };
    const [afterDependency, concurrentRetry] = await Promise.all([
      runtime.send(releasedRequest),
      runtime.send({ ...releasedRequest }),
    ]);
    assert.equal(afterDependency.deduplicated, true, "automatic release and Leader retry reconcile to one execution");
    assert.equal(afterDependency.reason, "already_in_progress");
    assert.equal(concurrentRetry.deduplicated, true);
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:reviewer`)), 1, "concurrent release/retry must atomically create one inbox message");

    await runtime.send({
      to: "reviewer",
      text: "Independent evidence check.",
      intent: "assignment",
      assignmentId: "independent-check",
      workId: "independent-check",
      phaseId: "phase-independent",
      revision: 1,
      required: true,
    });
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:reviewer`)), 2, "independent work must remain immediately parallel");

    await runtime.send({
      to: "reviewer",
      text: "Compatibility assignment with an unknown dependency label.",
      intent: "assignment",
      assignmentId: "unknown-dependency-check",
      workId: "unknown-dependency-check",
      phaseId: "phase-compat",
      revision: 1,
      required: true,
      dependsOn: ["legacy-natural-language-label"],
    });
    assert.equal(Number(await redis.command("XLEN", `${prefix}:inbox:reviewer`)), 3, "unknown dependency identity must fail open instead of freezing mixed versions");
  }, cfg);

  console.log("Redis Team dependency supervision regression: OK");
} finally {
  redis.close();
  await fs.rm(root, { recursive: true, force: true });
}
