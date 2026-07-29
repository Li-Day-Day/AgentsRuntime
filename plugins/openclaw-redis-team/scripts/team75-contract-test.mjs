import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const distPath = path.resolve(import.meta.dirname, "..", "dist", "index.js");
const source = (await fs.readFile(distPath, "utf8"))
  .replace('import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";', 'const definePluginEntry = (entry) => entry;')
  .replace('import { dispatchInboundDirectDmWithRuntime } from "openclaw/plugin-sdk/direct-dm";', 'const dispatchInboundDirectDmWithRuntime = async () => ({});');
const testSource = source + "\nexport { normalizeEnvelope, normalizePhaseDispositions, appendRedisTeamCompletionGuidance, appendLeaderTeamContext, turnFinishedWithoutCompletionEvent, shouldUseAssistantSessionFallback, normalizeRedisTeamTarget, resolveRedisTeamTarget, canonicalArtifactAlias, canonicalTeamArtifactRefsFromText, mergeTaskEnvelopeArtifactContext, sharedWorkspaceForTarget, verificationTargetUrl, reviewerBrowserToolDecision, browserToolCallFailed, rootWorkflowStateIsTerminal, previewUrlForTeamArtifact };\n";
const pluginModule = await import(`data:text/javascript;base64,${Buffer.from(testSource).toString("base64")}`);
const plugin = pluginModule.default;

const root = await fs.mkdtemp(path.join(os.tmpdir(), "redis-team-75-"));
const state = path.join(root, "state");
const shared = path.join(root, "shared");
process.env.XDG_STATE_HOME = state;
process.env.CLAWMANAGER_TEAM_TOKEN = "team-75-preview-secret";
process.env.CLAWMANAGER_TEAM_ID = "75";
process.env.CLAWMANAGER_TEAM_PREVIEW_ORIGIN = "http://clawmanager-egress-proxy.clawmanager-hxc-peer-system.svc.cluster.local:3128";
process.env.CLAWMANAGER_BROWSER_PROXY_URL = "http://clawmanager-egress-proxy.clawmanager-hxc-peer-system.svc.cluster.local:3128";

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
    on() {},
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
  await fs.writeFile(path.join(shared, "team.json"), JSON.stringify({
    version: 1,
    teamId: "75",
    rosterHash: "sha256:team-75-roster-v1",
    leaderMemberId: "leader",
    communicationMode: "leader_mediated",
    members: [
      { memberId: "leader", displayName: "Leader", role: "leader", effectiveRole: "leader", isLeader: true },
      { memberId: "developer", displayName: "Developer", role: "developer", effectiveRole: "developer" },
    ],
  }), "utf8");
  await fs.writeFile(
    path.join(shared, "team-introduction.md"),
    "# Team 启动介绍\n\nLeader coordinates the Developer.\n",
    "utf8",
  );
  const leaderContextEnvelope = pluginModule.normalizeEnvelope({
    teamId: "75",
    from: "clawmanager",
    to: "leader",
    taskId: "team-75-task-149",
    rootTaskId: "team-75-task-149",
    messageId: "root-149",
    metadata: {},
  });
  const fullLeaderContext = await pluginModule.appendLeaderTeamContext(
    "Please inspect the team.",
    { teamId: "75", memberId: "leader", role: "leader", sharedDir: shared },
    leaderContextEnvelope,
  );
  assert.match(fullLeaderContext, /Managed Team operating introduction/);
  assert.match(fullLeaderContext, /Leader coordinates the Developer/);
  assert.match(fullLeaderContext, /sha256:team-75-roster-v1/);
  const reviewerBrowserWithoutDeclaredTarget = pluginModule.reviewerBrowserToolDecision(
    { role: "reviewer", taskId: "team-75-task-149", text: "Review the local HTML artifact." },
    { toolName: "browser", params: { action: "start" } },
    {},
    1000,
  );
  assert.notEqual(reviewerBrowserWithoutDeclaredTarget.block, true);
  assert.equal(
    pluginModule.verificationTargetUrl({
      role: "reviewer",
      text: "Read https://example.com as source material, but no verification target was declared.",
    }),
    "",
    "ordinary URLs in assignment prose must not trigger Browser verification",
  );
  const browserState = {};
  const browserEnvelope = pluginModule.normalizeEnvelope({
    role: "reviewer",
    taskId: "team-75-task-149",
    reviewedAssignmentId: "dev-01",
    reviewedRevision: 2,
    verificationUrl: "https://example.com/review",
  });
  assert.equal(browserEnvelope.reviewedAssignmentId, "dev-01");
  assert.equal(browserEnvelope.reviewedRevision, 2);
  assert.equal(browserEnvelope.verificationUrl, "https://example.com/review");
  for (let index = 0; index < 4; index += 1) {
    const decision = pluginModule.reviewerBrowserToolDecision(
      browserEnvelope,
      { toolName: "browser", params: { action: index === 1 ? "open" : "snapshot", url: index === 1 ? "https://example.com/review" : undefined } },
      browserState,
      1000 + index,
    );
    assert.notEqual(decision.block, true);
  }
  const exhaustedBrowser = pluginModule.reviewerBrowserToolDecision(
    browserEnvelope,
    { toolName: "browser", params: { action: "snapshot" } },
    browserState,
    1005,
  );
  assert.equal(exhaustedBrowser.block, true);
  assert.equal(pluginModule.browserToolCallFailed({
    result: {
      content: [{ type: "text", text: JSON.stringify({ status: "error", tool: "browser", error: "navigation blocked" }) }],
    },
  }), true);
  assert.equal(pluginModule.browserToolCallFailed({
    result: { content: [{ type: "text", text: JSON.stringify({ status: "ok", title: "Rendered" }) }] },
  }), false);
  assert.deepEqual(
    pluginModule.normalizePhaseDispositions([
      { phaseId: "phase-2", decision: "skipped", reason: "Phase 1 fully satisfied the goal." },
      { phaseId: "phase-2", decision: "cancelled", reason: "duplicate must be ignored" },
      { phaseId: "phase-3", decision: "completed", reason: "invalid disposition" },
      { phaseId: "phase-4", decision: "superseded", reason: "" },
    ]),
    [{ phaseId: "phase-2", decision: "skipped", reason: "Phase 1 fully satisfied the goal." }],
  );
  for (let index = 0; index < 8; index += 1) {
    const developerBrowser = pluginModule.reviewerBrowserToolDecision(
      { role: "developer", taskId: "team-75-task-149" },
      { toolName: "browser", params: { action: "snapshot" } },
      {},
      2000 + index,
    );
    assert.notEqual(developerBrowser.block, true, "Developer Browser must not inherit the Reviewer budget");
  }
  assert.equal(pluginModule.rootWorkflowStateIsTerminal({ terminal: true }), true);
  assert.equal(pluginModule.rootWorkflowStateIsTerminal({ status: "succeeded" }), true);
  assert.equal(pluginModule.rootWorkflowStateIsTerminal({ status: "running" }), false);
  const compactLeaderContext = await pluginModule.appendLeaderTeamContext(
    "Please inspect the team again.",
    { teamId: "75", memberId: "leader", role: "leader", sharedDir: shared },
    { ...leaderContextEnvelope, taskId: "team-75-task-150", rootTaskId: "team-75-task-150", messageId: "root-150" },
  );
  assert.match(compactLeaderContext, /Managed Team roster snapshot/);
  assert.doesNotMatch(compactLeaderContext, /Managed Team operating introduction/);
  const workerContext = await pluginModule.appendLeaderTeamContext(
    "Implement it.",
    { teamId: "75", memberId: "developer", role: "developer", sharedDir: shared },
    { ...leaderContextEnvelope, to: "developer", taskId: "team-75-task-151", rootTaskId: "team-75-task-151" },
  );
  assert.equal(workerContext, "Implement it.", "worker prompts must not receive Leader roster preloading");
  const turnFinished = pluginModule.turnFinishedWithoutCompletionEvent(
    { metadata: { completionRecoveryAttempt: 1 } },
    { assistantNarratives: [{ text: "Final answer without tool receipt." }] },
  );
  assert.equal(turnFinished.eventKind, "turn_finished_without_completion");
  assert.equal(turnFinished.activeTurnFinished, true);
  assert.equal(turnFinished.hadAssistantNarrative, true);
  assert.equal(turnFinished.hadOutboundAssignment, false);
  assert.equal(turnFinished.completionRecoveryAttempt, 1);
  const controlTarget = await pluginModule.resolveRedisTeamTarget(
    { teamId: "75", memberId: "leader", sharedDir: shared },
    "clawmanager-monitor",
  );
  assert.equal(controlTarget.route, "control");
  assert.equal(controlTarget.completion, false);
  assert.equal(
    await pluginModule.shouldUseAssistantSessionFallback(
      { teamId: "75", memberId: "leader", role: "leader", sharedDir: shared },
      leaderContextEnvelope,
      "A natural-language answer that did not call the completion tool.",
    ),
    true,
    "a complete Leader turn must be submitted through the same completion proposal path",
  );

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
  const context = toolResult(await leaderTools.get("team_artifact_write").execute("context-write", {
    scope: "team",
    kind: "context",
    path: "issue-146.md",
    content: "# Issue 146 snapshot\n",
  }));
  assert.equal(context.ok, true);
  assert.equal(context.artifact.path, "/team/results/team-75-task-150/context/issue-146.md");
  assert.equal(
    pluginModule.canonicalArtifactAlias(
      { sharedDir: shared },
      "/team/plan/collaboration-plan.md",
      "team-75-task-150",
    ),
    "/team/results/team-75-task-150/plan/collaboration-plan.md",
    "legacy plan aliases must resolve inside the current root task only",
  );
  const assignedWorkspace = pluginModule.sharedWorkspaceForTarget(
    { sharedDir: shared, memberId: "leader" },
    {},
    "developer",
    "team-75-task-150",
    "dev-01",
  );
  assert.equal(
    assignedWorkspace.assignmentArtifactCanonicalRoot,
    "/team/artifacts/team-75-task-150/members/developer/dev-01",
  );
  assert.equal(
    assignedWorkspace.taskWorkCanonicalRoot,
    "/team/work/team-75-task-150",
    "cross-member mutable inputs must be isolated by root task",
  );
  assert.equal(
    assignedWorkspace.taskContextCanonicalRoot,
    "/team/results/team-75-task-150/context",
    "durable research context must remain inside the current root task",
  );
  const persistedLeaderEnvelope = JSON.parse(await fs.readFile(
    path.join(state, "teams", "75", "leader", "tasks", "team-75-task-150.json"),
    "utf8",
  ));
  assert.deepEqual(
    persistedLeaderEnvelope.artifactRefs,
    [plan.artifact.path, context.artifact.path],
    "Leader plan and research context refs must survive tool turns in the persisted root envelope",
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
    [plan.artifact.path, context.artifact.path, upstreamRef],
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
  const preview = toolResult(await developerTools.get("team_artifact_preview").execute("preview-index", {
    scope: "team",
    path: "index.html",
  }));
  assert.equal(preview.ok, true);
  assert.equal(preview.artifact.path, "/team/index.html");
  assert.match(
    preview.artifact.previewUrl,
    /^http:\/\/clawmanager-egress-proxy\.clawmanager-hxc-peer-system\.svc\.cluster\.local:3128\/v1\/75\/_\/[A-Za-z0-9_-]+\/index\.html$/,
  );
  assert.equal(new URL(preview.artifact.previewUrl).search, "", "preview links must not carry an expiry");
  process.env.CLAWMANAGER_TEAM_PREVIEW_ORIGIN = "http://clawmanager-team-preview.invalid";
  const legacyPreviewUrl = pluginModule.previewUrlForTeamArtifact(
    { teamId: "75", sharedDir: shared },
    path.join(shared, "index.html"),
  );
  assert.equal(
    new URL(legacyPreviewUrl.url).hostname,
    "clawmanager-egress-proxy.clawmanager-hxc-peer-system.svc.cluster.local",
    "a new Runtime must repair the legacy .invalid origin with the managed proxy address",
  );
  assert.equal(new URL(legacyPreviewUrl.url).port, "3128");
  process.env.CLAWMANAGER_TEAM_PREVIEW_ORIGIN = "http://attacker.example";
  assert.throws(
    () => pluginModule.previewUrlForTeamArtifact(
      { teamId: "75", sharedDir: shared },
      path.join(shared, "index.html"),
    ),
    /not managed by ClawManager/,
    "an arbitrary preview origin must never receive a Team-signed artifact path",
  );
  process.env.CLAWMANAGER_TEAM_PREVIEW_ORIGIN = "http://clawmanager-egress-proxy.clawmanager-hxc-peer-system.svc.cluster.local:3128";
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
