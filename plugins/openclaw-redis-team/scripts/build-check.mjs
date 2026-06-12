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
  "completeActiveTaskFromText",
  "failActiveTask",
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
  "attachedResults",
  "sendText",
  "completionMessageId",
  "resultMarkdown",
  "message_failed",
  "dispatch finished without reply/completion",
]) {
  if (!dist.includes(token)) throw new Error(`dist/index.js missing Redis Team completion token: ${token}`);
}
if (dist.includes("leaving task running")) {
  throw new Error("dist/index.js must not leave Redis Team tasks running after dispatch returns");
}
if (dist.includes("params.taskId === activeEnvelope.taskId")) {
  throw new Error("dist/index.js must match active Redis Team task ids through aliases");
}
if (!dist.includes("activeTaskCompleted = true;")) {
  throw new Error("dist/index.js must mark active Redis Team tasks completed after an explicit completion/reply");
}
if (!dist.includes('const runtimeStatus = params.status === "succeeded" ? "succeeded" : "failed"')) {
  throw new Error("dist/index.js must set runtimeStatus when completing Redis Team tasks");
}
console.log("openclaw-redis-team build check passed");
