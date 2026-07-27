import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const distPath = path.resolve(import.meta.dirname, "..", "dist", "index.js");
const source = (await fs.readFile(distPath, "utf8"))
  .replace('import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";', 'const definePluginEntry = (entry) => entry;')
  .replace('import { dispatchInboundDirectDmWithRuntime } from "openclaw/plugin-sdk/direct-dm";', 'const dispatchInboundDirectDmWithRuntime = async () => ({});');
const testSource = source + "\nexport { normalizeEnvelope, appendRedisTeamCompletionGuidance, canonicalTeamArtifactRefsFromText, mergeTaskEnvelopeArtifactContext };\n";
const pluginModule = await import(`data:text/javascript;base64,${Buffer.from(testSource).toString("base64")}`);
const plugin = pluginModule.default;

const root = await fs.mkdtemp(path.join(os.tmpdir(), "redis-team-75-"));
const state = path.join(root, "state");
const shared = path.join(root, "shared");
process.env.XDG_STATE_HOME = state;

function createHarness(memberId, role) {
  const registered = new Map();
  const config = {
    channels: {
      "redis-team": {
        accounts: {
          default: {
            fromEnv: false,
            enabled: true,
            teamId: "75",
            memberId,
            role,
            sharedDir: shared,
          },
        },
      },
    },
  };
  plugin.register({
    config,
    registerTool(tool) {
      registered.set(tool.name, tool);
    },
    registerChannel() {},
  });
  return registered;
}

async function seedActive(memberId, role, assignmentId) {
  const dir = path.join(state, "teams", "75", memberId);
  await fs.mkdir(dir, { recursive: true });
  const envelope = {
    teamId: "75",
    memberId,
    role,
    taskId: "team-75-task-150",
    rootTaskId: "team-75-task-150",
    messageId: `msg-${memberId}`,
    rootMessageId: "team-75-task-1784165223585610285",
    assignmentId,
    workId: assignmentId,
    phaseId: role === "reviewer" ? "phase-review" : "phase-dev",
    responseLocale: "zh-CN",
    activeAssignmentContext: {
      teamId: "75",
      memberId,
      recordedAt: new Date().toISOString(),
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
      terminal: false,
    },
  };
  await fs.writeFile(path.join(dir, "active-assignment.json"), JSON.stringify(envelope), "utf8");
}

function toolResult(result) {
  return JSON.parse(result.content[0].text);
}

function resultContentHash(content, refs) {
  const normalized = String(content || "").trim().split(/\s+/).filter(Boolean).join(" ");
  return "sha256:" + createHash("sha256")
    .update(normalized + "\nrefs=" + [...refs].sort().join("|"))
    .digest("hex");
}

try {
  await fs.mkdir(shared, { recursive: true });
  await seedActive("leader", "leader", "leader-final-synthesis");
  const leaderTools = createHarness("leader", "leader");
  const plan = toolResult(await leaderTools.get("team_artifact_write").execute("plan-write", {
    scope: "team",
    kind: "plan",
    path: "collaboration-plan.md",
    content: "# Collaboration plan\n\nDeveloper then Reviewer.\n",
  }));
  assert.equal(plan.ok, true);
  assert.equal(plan.artifact.path, "/team/results/team-75-task-150/plan/collaboration-plan.md");
  const persistedLeaderEnvelope = JSON.parse(await fs.readFile(
    path.join(state, "teams", "75", "leader", "tasks", "team-75-task-150.json"),
    "utf8",
  ));
  assert.deepEqual(
    persistedLeaderEnvelope.artifactRefs,
    [plan.artifact.path],
    "Leader plan refs must survive the tool turn in the persisted root envelope",
  );
  const upstreamRef = "/team/artifacts/team-75-task-150/members/researcher/research-01/result.md";
  await fs.mkdir(path.join(shared, "artifacts", "team-75-task-150", "members", "researcher", "research-01"), { recursive: true });
  await fs.writeFile(
    path.join(shared, upstreamRef.slice("/team/".length)),
    "# Developer result\n",
    "utf8",
  );
  const mergedLeaderEnvelope = await pluginModule.mergeTaskEnvelopeArtifactContext(
    { teamId: "75", memberId: "leader", sharedDir: shared },
    persistedLeaderEnvelope,
    [upstreamRef],
  );
  assert.deepEqual(
    mergedLeaderEnvelope.artifactRefs,
    [plan.artifact.path, upstreamRef],
    "context-only member results must accumulate without replacing the persisted Leader plan",
  );
  const legacyPlanWrite = toolResult(await leaderTools.get("team_artifact_write").execute("plan-write-legacy", {
    scope: "team",
    kind: "plan",
    path: "plan/legacy-plan.md",
    content: "# Legacy-compatible plan path\n",
  }));
  assert.equal(legacyPlanWrite.ok, true);
  assert.equal(
    legacyPlanWrite.artifact.path,
    "/team/results/team-75-task-150/plan/legacy-plan.md",
    "legacy kind=plan writes must not create a duplicated plan/plan directory",
  );

  await seedActive("developer", "developer", "dev-01");
  const developerTools = createHarness("developer", "developer");
  const canonicalPlanRead = toolResult(await developerTools.get("team_artifact_read").execute("plan-read-canonical", {
    path: plan.artifact.path,
  }));
  assert.equal(canonicalPlanRead.ok, true);
  assert.match(canonicalPlanRead.artifact.content, /Developer then Reviewer/);
  const legacyPlanRead = toolResult(await developerTools.get("team_artifact_read").execute("plan-read-legacy", {
    scope: "team",
    kind: "plan",
    path: "plan/collaboration-plan.md",
  }));
  assert.equal(legacyPlanRead.ok, true, "kind=plan must not duplicate a leading plan/ segment");
  assert.equal(legacyPlanRead.artifact.path, plan.artifact.path);
  const teamRelativePlanRead = toolResult(await developerTools.get("team_artifact_read").execute("plan-read-relative", {
    scope: "team",
    kind: "plan",
    path: "results/team-75-task-150/plan/collaboration-plan.md",
  }));
  assert.equal(teamRelativePlanRead.ok, true, "legacy Team-relative canonical paths must remain readable");

  const normalizedEnvelope = pluginModule.normalizeEnvelope({
    teamId: "75",
    memberId: "developer",
    taskId: "team-75-task-150",
    artifactRefs: [plan.artifact.path],
    contextRefs: ["design-notes"],
  });
  assert.deepEqual(normalizedEnvelope.artifactRefs, [plan.artifact.path]);
  const guidedPrompt = pluginModule.appendRedisTeamCompletionGuidance("Implement the page.", normalizedEnvelope);
  assert.match(guidedPrompt, /read these exact canonical paths/);
  assert.match(guidedPrompt, /\/team\/results\/team-75-task-150\/plan\/collaboration-plan\.md/);
  assert.deepEqual(
    pluginModule.canonicalTeamArtifactRefsFromText(
      { sharedDir: shared },
      `env: $CLAWMANAGER_TEAM_SHARED_DIR/index.html physical: ${path.join(shared, "index.html")}`,
    ),
    ["/team/index.html"],
    "current-Team env and physical paths must normalize to one canonical artifact ref",
  );
  assert.deepEqual(
    pluginModule.canonicalTeamArtifactRefsFromText(
      { sharedDir: shared },
      "/workspaces/teams/user-1/team-999-shared/private.html",
    ),
    [],
    "a sibling Team physical path must never be imported into the current Team context",
  );

  const progress = toolResult(await developerTools.get("team_update_progress").execute("progress-1", {
    status: "running",
    progress: 25,
    eventKind: "worker_plan",
    summary: "\u5f00\u59cb\u5b9e\u73b0\u8f7b\u91cf\u770b\u677f\u9875\u9762",
  }));
  assert.equal(progress.ok, true);
  assert.equal(progress.status.currentTaskId, "team-75-task-150");
  assert.equal(progress.status.currentAssignmentId, "dev-01");
  const englishProgress = toolResult(await developerTools.get("team_update_progress").execute("progress-en", {
    status: "running",
    progress: 30,
    eventKind: "worker_progress",
    summary: "Running static checks before delivery",
  }));
  assert.equal(englishProgress.ok, true, "locale mismatch must remain non-blocking");

  await fs.writeFile(path.join(shared, "index.html"), "<!doctype html><title>Team 75</title>", "utf8");
  const truncatedRead = toolResult(await developerTools.get("team_artifact_read").execute("read-truncated", {
    scope: "team",
    path: "index.html",
    maxBytes: 10,
  }));
  assert.equal(truncatedRead.ok, true);
  assert.equal(truncatedRead.artifact.truncated, true);
  assert.equal(truncatedRead.artifact.nextOffset, 10);
  assert.equal(Buffer.byteLength(truncatedRead.artifact.content, "utf8"), 10);
  const completion = toolResult(await developerTools.get("team_complete_task").execute("complete-1", {
    status: "succeeded",
    summary: "\u8f7b\u91cf\u770b\u677f\u7f51\u9875\u5f00\u53d1\u5b8c\u6210",
    resultMarkdown: "\u4ea4\u4ed8\u6587\u4ef6\uff1a/team/index.html",
  }));
  assert.deepEqual(completion.artifactRefs, ["/team/index.html"]);

  const developerStatusPath = path.join(shared, "status", "developer.json");
  const acceptedStatus = {
    teamId: "75",
    memberId: "developer",
    role: "developer",
    currentTaskId: "team-75-task-150",
    currentAssignmentId: "dev-01",
    runtimeStatus: "succeeded",
    availability: "idle",
    progress: 100,
    lastSummary: "Development result accepted",
    artifactRefs: ["/team/index.html"],
    resultContentHash: resultContentHash("\u4ea4\u4ed8\u6587\u4ef6\uff1a/team/index.html", ["/team/index.html"]),
  };
  await fs.writeFile(developerStatusPath, JSON.stringify(acceptedStatus), "utf8");
  const lateProgress = toolResult(await developerTools.get("team_update_progress").execute("progress-late", {
    status: "running",
    progress: 99,
    eventKind: "worker_progress",
    summary: "\u8fdf\u5230\u7684\u8fd0\u884c\u72b6\u6001",
  }));
  assert.equal(lateProgress.status.runtimeStatus, "succeeded");
  assert.equal(lateProgress.status.lastSummary, "Development result accepted");
  assert.equal(JSON.parse(await fs.readFile(developerStatusPath, "utf8")).lastSummary, "Development result accepted");
  const legacyAcceptedStatus = { ...acceptedStatus };
  delete legacyAcceptedStatus.resultContentHash;
  await fs.writeFile(developerStatusPath, JSON.stringify(legacyAcceptedStatus), "utf8");
  const legacyChangedCompletion = toolResult(await developerTools.get("team_complete_task").execute("complete-legacy-changed", {
    status: "succeeded",
    summary: "\u65e7\u7248\u72b6\u6001\u4e0b\u7684\u4fee\u6b63",
    resultMarkdown: "\u65e7\u7248\u72b6\u6001\u4e0b\u4e0d\u5e94\u731c\u6d4b\u662f\u5426\u9700\u8981\u91cd\u5f00\uff1a/team/index.html",
  }));
  assert.equal(legacyChangedCompletion.completion.reason, "already_terminal");
  assert.equal(legacyChangedCompletion.completion.published, false);
  await fs.writeFile(developerStatusPath, JSON.stringify(acceptedStatus), "utf8");
  const duplicateCompletion = toolResult(await developerTools.get("team_complete_task").execute("complete-duplicate", {
    status: "succeeded",
    summary: "\u8f7b\u91cf\u770b\u677f\u7f51\u9875\u5f00\u53d1\u5b8c\u6210",
    resultMarkdown: "\u4ea4\u4ed8\u6587\u4ef6\uff1a/team/index.html",
  }));
  assert.equal(duplicateCompletion.completion.reason, "already_terminal");
  assert.equal(duplicateCompletion.completion.published, false);
  const correctedCompletion = toolResult(await developerTools.get("team_complete_task").execute("complete-corrected", {
    status: "succeeded",
    summary: "\u8f7b\u91cf\u770b\u677f\u7f51\u9875\u4fee\u6b63\u5b8c\u6210",
    resultMarkdown: "\u4fee\u6b63\u540e\u4ea4\u4ed8\u6587\u4ef6\uff1a/team/index.html",
  }));
  assert.equal(correctedCompletion.status.runtimeStatus, "completion_pending");

  await seedActive("reviewer", "reviewer", "review-01");
  const reviewerTools = createHarness("reviewer", "reviewer");
  const review = toolResult(await reviewerTools.get("team_artifact_write").execute("review-1", {
    scope: "team",
    kind: "review",
    path: "review-report.md",
    content: "# Review report\n\nPASS\n",
  }));
  assert.equal(review.ok, true);
  assert.equal(review.artifact.path, "/team/results/team-75-task-150/reviews/review-01/review-report.md");
  await fs.access(path.join(shared, "results", "team-75-task-150", "reviews", "review-01", "review-report.md"));

  await seedActive("leader", "leader", "review-01");
  const leaderIdentityTools = createHarness("leader", "leader");
  const leaderSynthesis = toolResult(await leaderIdentityTools.get("team_update_progress").execute("leader-synthesis", {
    status: "running",
    progress: 95,
    eventKind: "leader_synthesis",
    summary: "\u6b63\u5728\u6574\u7406\u6700\u7ec8\u4ea4\u4ed8",
  }));
  assert.equal(
    leaderSynthesis.status.currentAssignmentId,
    "leader-final-synthesis",
    "Leader synthesis must not inherit the Reviewer's assignment identity",
  );

  assert.match(source, /function analyzeResponseLocale\(/);
  assert.doesNotMatch(source, /must use \$\{locale \|\| "zh-CN"\}/);
  assert.match(source, /workflowReminderIsStale/);
  assert.match(source, /ignored message post-processing failure after terminal assignment/);

  console.log("Team75 Redis Team contract test passed");
} finally {
  await fs.rm(root, { recursive: true, force: true });
}
