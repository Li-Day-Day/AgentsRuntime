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
  "PROTOCOL_VERSION = 2",
  "waiting_completion",
  "resultMarkdown",
  "Math.min(99",
  "await writeText(resultMarkdownPath, resultMarkdown)",
  "message_failed",
  "assignment-",
]) {
  if (!dist.includes(token)) throw new Error(`dist/index.js missing Redis Team completion token: ${token}`);
}
if (dist.includes("params.taskId === activeEnvelope.taskId")) {
  throw new Error("dist/index.js must match active Redis Team task ids through aliases");
}
const deliverStart = dist.indexOf("deliver: async (payload) => {");
const deliverEnd = dist.indexOf("onRecordError:", deliverStart);
if (deliverStart < 0 || deliverEnd < 0) throw new Error("unable to locate Redis Team deliver callback");
const deliverBody = dist.slice(deliverStart, deliverEnd);
if (deliverBody.includes("completeActiveTask") || deliverBody.includes('"task_completed"')) {
  throw new Error("normal Redis Team replies must not complete the business task");
}
if (dist.includes("seenMessageIds")) {
  throw new Error("dist/index.js must use Redis-backed message idempotency instead of process memory");
}
if (!dist.includes('const runtimeStatus = params.status === "succeeded" ? "succeeded" : "failed"')) {
  throw new Error("dist/index.js must set runtimeStatus when completing Redis Team tasks");
}
console.log("openclaw-redis-team build check passed");
