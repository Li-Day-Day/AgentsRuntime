import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const distPath = path.resolve(import.meta.dirname, "..", "dist", "index.js");
const source = (await fs.readFile(distPath, "utf8"))
  .replace('import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";', 'const definePluginEntry = (entry) => entry;')
  .replace('import { dispatchInboundDirectDmWithRuntime } from "openclaw/plugin-sdk/direct-dm";', 'const dispatchInboundDirectDmWithRuntime = async () => ({});');
const plugin = (await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`)).default;

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

try {
  await fs.mkdir(shared, { recursive: true });
  await seedActive("developer", "developer", "dev-01");
  const developerTools = createHarness("developer", "developer");
  const progress = toolResult(await developerTools.get("team_update_progress").execute("progress-1", {
    status: "running",
    progress: 25,
    eventKind: "worker_plan",
    summary: "\u5f00\u59cb\u5b9e\u73b0\u8f7b\u91cf\u770b\u677f\u9875\u9762",
  }));
  assert.equal(progress.ok, true);
  assert.equal(progress.status.currentTaskId, "team-75-task-150");
  assert.equal(progress.status.currentAssignmentId, "dev-01");

  await fs.writeFile(path.join(shared, "index.html"), "<!doctype html><title>Team 75</title>", "utf8");
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

  console.log("Team75 Redis Team contract test passed");
} finally {
  await fs.rm(root, { recursive: true, force: true });
}
