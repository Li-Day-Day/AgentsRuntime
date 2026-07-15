import fs from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const required = ["package.json", "openclaw.plugin.json", "dist/index.js", "README.md"];
for (const rel of required) {
  const full = path.join(root, rel);
  if (!fs.existsSync(full)) throw new Error(`missing required file: ${rel}`);
}
const manifest = JSON.parse(fs.readFileSync(path.join(root, "openclaw.plugin.json"), "utf8"));
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const dist = fs.readFileSync(path.join(root, "dist", "index.js"), "utf8");
if (manifest.id !== "redis-team") throw new Error(`unexpected plugin id: ${manifest.id}`);
if (!pkg.openclaw?.extensions?.includes("./dist/index.js")) {
  throw new Error("package.json openclaw.extensions must include ./dist/index.js");
}
for (const token of [
  "CLAWMANAGER_TEAM_INBOX_KEY",
  "CLAWMANAGER_TEAM_EVENTS_KEY",
  "CLAWMANAGER_TEAM_PRESENCE_KEY",
  "CLAWMANAGER_TEAM_DLQ_KEY",
  "task_received",
  "task_started",
  "runtimeStatus",
  "availability",
]) {
  if (!dist.includes(token)) throw new Error(`dist/index.js missing Redis Team protocol token: ${token}`);
}
for (const token of [
  "completeActiveTask",
  "failActiveTask",
  "xaddTerminalOnce",
  "completionKey",
  "processedMessageKey",
  "stableAssignmentId",
  "validateArtifactRefs",
  "isActiveCompletionTarget",
  "taskIdAliases",
  "writeTaskEnvelope",
  "readTaskEnvelope",
  "runtimeStateDir",
  "privateTaskEnvelopePath",
  "writeJsonBestEffort",
  "TEAM_SHARED_DIR_MODE = 0o2775",
  "isTaskTerminal",
  "statusIsActive",
  "pendingDrainBatches",
  "pending/history drain limit reached",
  "waitForConsumerStop",
  "resolveConsumerStopped",
  "targetResolver",
  "inferTargetChatType",
  "baseSessionKey",
  "completionMessageId",
  "completionId",
  "completionSource",
  "explicitCompletion",
  "WIRE_SCHEMA_VERSION = 1",
  "PROTOCOL_VERSION = 3",
  "completion_proposed",
  "waitForCompletionAcknowledgement",
  "waitForTerminalCompletionState",
  "completion-state:",
  "artifact_changed",
  "waivers",
  "skippedAssignments",
  "completion_pending",
  "waiting_completion",
  "resultMarkdown",
  "Math.min(99",
  "await writeText(resultMarkdownPath, resultMarkdown)",
  "message_failed",
  "assignment-",
  "team_artifact_write",
  "team_artifact_read",
  "team_artifact_list",
  "team_artifact_mkdir",
  "assertNoArtifactSymlinkTraversal",
  "assertTeamArtifactWriteScope",
  "assertResponseLocale",
  "sharedWorkspaceForTarget",
  "artifactRootTaskId",
  "collectRootTaskArtifactRefs",
  "kind=plan, kind=review, or kind=final",
  "eventKind: \"agent_narrative\"",
  "suppressed duplicate reply after submitted completion",
]) {
  if (!dist.includes(token)) throw new Error(`dist/index.js missing Redis Team completion token: ${token}`);
}
if (dist.includes("params.taskId === activeEnvelope.taskId")) {
  throw new Error("dist/index.js must match active Redis Team task ids through aliases");
}
if (dist.includes('path.join(cfg.sharedDir, ".openclaw-redis-team", "tasks"')) {
  throw new Error("member-scoped Redis Team envelopes must not require a shared NFS .openclaw-redis-team/tasks directory");
}
if (dist.includes('|| "unscoped"')) {
  throw new Error("member Team artifacts must reject missing root task context instead of writing an unscoped path");
}
const deliverStart = dist.indexOf("deliver: async (payload) => {");
const deliverEnd = dist.indexOf("onRecordError:", deliverStart);
if (deliverStart < 0 || deliverEnd < 0) throw new Error("unable to locate Redis Team deliver callback");
const deliverBody = dist.slice(deliverStart, deliverEnd);
if (deliverBody.includes("completeActiveTask") || deliverBody.includes('"task_completed"')) {
  throw new Error("normal Redis Team replies must not complete the business task");
}
if (!deliverBody.includes("runtime.isActiveTaskCompleted")) {
  throw new Error("normal Redis Team replies must be suppressed after explicit completion");
}
for (const tool of ["team_artifact_write", "team_artifact_read", "team_artifact_list", "team_artifact_mkdir"]) {
  if (!manifest.contracts?.tools?.includes(tool)) {
    throw new Error(`openclaw.plugin.json missing tool contract: ${tool}`);
  }
}
if (dist.includes("seenMessageIds")) {
  throw new Error("dist/index.js must use Redis-backed message idempotency instead of process memory");
}
if (!dist.includes('const runtimeStatus = "completion_pending"')) {
  throw new Error("dist/index.js must wait for backend acknowledgement before marking Redis Team tasks terminal");
}
console.log("openclaw-redis-team build check passed");
