import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import tls from "node:tls";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { dispatchInboundDirectDmWithRuntime } from "openclaw/plugin-sdk/direct-dm";

const PLUGIN_ID = "redis-team";
const CHANNEL_ID = "redis-team";
const DEFAULT_SHARED_DIR = "/team";
const DEFAULT_GROUP = "team-members";
const DEFAULT_EMBEDDED_TIMEOUT_SECONDS = 1800;
const STATUS_INTERVAL_MS = 15000;
const READ_BLOCK_MS = 15000;
const SCHEMA_VERSION = 1;
const SYSTEM_REPLY_TARGETS = new Set([
  "clawmanager",
  "manager",
  "admin",
  "user",
  "requester",
  "caller",
  "system",
]);

function trim(value) {
  return typeof value === "string" ? value.trim() : "";
}
function boolFrom(value, fallback = false) {
  if (typeof value === "boolean") return value;
  const v = trim(value).toLowerCase();
  if (["1", "true", "yes", "on"].includes(v)) return true;
  if (["0", "false", "no", "off"].includes(v)) return false;
  return fallback;
}
function intFrom(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  const raw = trim(value);
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}
function safeName(value) {
  return String(value || "unknown").replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 160);
}
function taskIdAliases(value) {
  const raw = trim(value);
  if (!raw) return [];
  const aliases = new Set([raw]);
  const teamTask = raw.match(/^team-[^-]+-task-(.+)$/);
  if (teamTask?.[1]) {
    aliases.add(teamTask[1]);
    aliases.add("task-" + teamTask[1]);
  }
  const shortTask = raw.match(/^task-(.+)$/);
  if (shortTask?.[1]) aliases.add(shortTask[1]);
  return Array.from(aliases);
}
function taskIdsMatch(left, right) {
  if (!left || !right) return false;
  const rightAliases = new Set(taskIdAliases(right));
  return taskIdAliases(left).some((alias) => rightAliases.has(alias));
}
function nowIso() {
  return new Date().toISOString();
}
function redisClientName(cfg, purpose) {
  return ["redis-team", safeName(cfg.teamId), safeName(cfg.memberId), purpose].join(":").slice(0, 512);
}
function deriveTeamIdFromKey(value) {
  const raw = trim(value);
  const match = raw.match(/^claw:team:([^:]+):/);
  return match ? match[1] : "";
}
function isTeamBroadcastTarget(value, cfg = {}) {
  const raw = trim(value) || "broadcast";
  const lower = raw.toLowerCase();
  const teamId = trim(cfg.teamId).toLowerCase();
  if (lower === "broadcast" || lower === "team") return true;
  if (!teamId) return /^team[-_:][a-z0-9_.-]+$/i.test(raw);
  return (
    lower === "team-" + teamId ||
    lower === "team:" + teamId ||
    lower === "team_" + teamId ||
    lower === "claw:team:" + teamId
  );
}
function isActiveCompletionTarget(value, cfg = {}) {
  const raw = trim(value) || "broadcast";
  const lower = raw.toLowerCase();
  return SYSTEM_REPLY_TARGETS.has(lower) || isTeamBroadcastTarget(raw, cfg);
}
function normalizeRedisTeamTarget(value, cfg = {}) {
  const raw = trim(value) || "broadcast";
  const lower = raw.toLowerCase();
  const system = SYSTEM_REPLY_TARGETS.has(lower);
  const group = !system && isTeamBroadcastTarget(raw, cfg);
  return {
    to: system || group ? "broadcast" : raw,
    originalTo: raw,
    system,
    group,
    completion: system || group,
  };
}
function isSafeMemberTarget(value) {
  const raw = trim(value);
  return !!raw && /^[A-Za-z0-9_.@-]{1,160}$/.test(raw);
}
async function resolveRedisTeamTarget(cfg, value) {
  const target = normalizeRedisTeamTarget(value, cfg);
  if (target.completion) return Object.assign(target, { route: "completion" });
  if (!isSafeMemberTarget(target.to)) {
    return Object.assign(target, { route: "unknown", error: "unknown Redis Team target: " + target.originalTo });
  }
  const statuses = await readStatuses(cfg);
  if (!statuses.length || target.to === cfg.memberId) {
    return Object.assign(target, { route: "member" });
  }
  const known = statuses.some((status) => {
    const memberId = trim(status?.memberId);
    return memberId === target.to || safeName(memberId) === target.to;
  });
  if (known) return Object.assign(target, { route: "member" });
  return Object.assign(target, { route: "unknown", error: "unknown Redis Team target: " + target.originalTo });
}

// ============ Redis Transport ============
function encodeResp(args) {
  const chunks = [];
  chunks.push(Buffer.from("*" + args.length + "\r\n"));
  for (const arg of args) {
    const value = Buffer.isBuffer(arg) ? arg : Buffer.from(String(arg));
    chunks.push(Buffer.from("$" + value.length + "\r\n"));
    chunks.push(value);
    chunks.push(Buffer.from("\r\n"));
  }
  return Buffer.concat(chunks);
}

class RespParser {
  constructor() {
    this.buffer = Buffer.alloc(0);
  }
  push(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
  }
  line(offset) {
    const end = this.buffer.indexOf("\r\n", offset);
    if (end < 0) return null;
    return [this.buffer.toString("utf8", offset, end), end + 2];
  }
  parseAt(offset) {
    if (offset >= this.buffer.length) return null;
    const t = String.fromCharCode(this.buffer[offset]);
    if (t === "+" || t === "-" || t === ":") {
      const line = this.line(offset + 1);
      if (!line) return null;
      const text = line[0];
      const next = line[1];
      if (t === "-") return [{ error: text }, next];
      if (t === ":") return [Number(text), next];
      return [text, next];
    }
    if (t === "$") {
      const line = this.line(offset + 1);
      if (!line) return null;
      const len = Number(line[0]);
      const start = line[1];
      if (len < 0) return [{ redisNull: true }, start];
      const end = start + len;
      if (this.buffer.length < end + 2) return null;
      return [this.buffer.toString("utf8", start, end), end + 2];
    }
    if (t === "*") {
      const line = this.line(offset + 1);
      if (!line) return null;
      const len = Number(line[0]);
      let cursor = line[1];
      if (len < 0) return [{ redisNull: true }, cursor];
      const arr = [];
      for (let i = 0; i < len; i += 1) {
        const parsed = this.parseAt(cursor);
        if (!parsed) return null;
        arr.push(this.isRedisNull(parsed[0]) ? null : parsed[0]);
        cursor = parsed[1];
      }
      return [arr, cursor];
    }
    return [{ error: "unsupported RESP type " + t }, this.buffer.length];
  }
  isRedisNull(value) {
    return !!(value && typeof value === "object" && !Array.isArray(value) && value.redisNull);
  }
  take() {
    const parsed = this.parseAt(0);
    if (!parsed) return null;
    this.buffer = this.buffer.subarray(parsed[1]);
    return parsed[0];
  }
}

class RedisClient {
  constructor(url) {
    this.url = new URL(url);
    this.parser = new RespParser();
    this.pending = [];
    this.closed = false;
  }
  async connect() {
    const port = Number(this.url.port || (this.url.protocol === "rediss:" ? 6380 : 6379));
    const host = this.url.hostname || "127.0.0.1";
    this.socket =
      this.url.protocol === "rediss:"
        ? tls.connect({ host, port, servername: host })
        : net.connect({ host, port });
    this.socket.on("data", (chunk) => {
      this.parser.push(chunk);
      this.drain();
    });
    this.socket.on("error", (err) => this.rejectAll(err));
    this.socket.on("close", () => this.rejectAll(new Error("redis connection closed")));
    await new Promise((resolve, reject) => {
      this.socket.once("connect", resolve);
      this.socket.once("error", reject);
    });
    const user = decodeURIComponent(this.url.username || "");
    const pass = decodeURIComponent(this.url.password || "");
    if (pass) {
      if (user) await this.command("AUTH", user, pass);
      else await this.command("AUTH", pass);
    }
    const db = this.url.pathname.replace(/^\//, "");
    if (db) await this.command("SELECT", db);
  }
  drain() {
    while (this.pending.length) {
      const value = this.parser.take();
      if (value === null) return;
      const p = this.pending.shift();
      if (this.parser.isRedisNull(value)) p.resolve(null);
      else if (value && typeof value === "object" && !Array.isArray(value) && value.error)
        p.reject(new Error(value.error));
      else p.resolve(value);
    }
  }
  rejectAll(err) {
    while (this.pending.length) this.pending.shift().reject(err);
  }
  command(...args) {
    if (this.closed) return Promise.reject(new Error("redis client is closed"));
    return new Promise((resolve, reject) => {
      this.pending.push({ resolve, reject });
      this.socket.write(encodeResp(args));
    });
  }
  close() {
    this.closed = true;
    if (this.socket) this.socket.destroy();
  }
}

// ============ Config ============
function readChannelConfig(cfg, accountId = "default") {
  const channel = cfg?.channels?.[CHANNEL_ID];
  const account = channel?.accounts?.[accountId] || {};
  const env = process.env;
  const fromEnv = account.fromEnv !== false;
  return {
    enabled: boolFrom(account.enabled ?? (fromEnv ? env.CLAWMANAGER_TEAM_ENABLED : undefined), false),
    redisUrl:
      trim(account.redisUrl) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_REDIS_URL) : ""),
    teamId:
      trim(account.teamId) ||
      (fromEnv ? trim(env.CLAWMANAGER_TEAM_ID) : "") ||
      deriveTeamIdFromKey(trim(account.inboxKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_INBOX_KEY) : "")) ||
      deriveTeamIdFromKey(trim(account.eventsKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_EVENTS_KEY) : "")) ||
      deriveTeamIdFromKey(trim(account.presenceKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_PRESENCE_KEY) : "")),
    memberId:
      trim(account.memberId) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_MEMBER_ID) : ""),
    role: trim(account.role) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_ROLE) : "") || "member",
    sharedDir:
      trim(account.sharedDir) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_SHARED_DIR) : "") || DEFAULT_SHARED_DIR,
    managerUrl:
      trim(account.managerUrl) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_MANAGER_URL) : ""),
    autoRun:
      boolFrom(
        account.autoRun ?? (fromEnv ? env.CLAWMANAGER_TEAM_AUTORUN : undefined),
        true,
      ),
    consumerGroup:
      trim(account.consumerGroup) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_CONSUMER_GROUP) : "") || DEFAULT_GROUP,
    inboxKey:
      trim(account.inboxKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_INBOX_KEY) : ""),
    eventsKey:
      trim(account.eventsKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_EVENTS_KEY) : ""),
    presenceKey:
      trim(account.presenceKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_PRESENCE_KEY) : ""),
    dlqKey:
      trim(account.dlqKey) || (fromEnv ? trim(env.CLAWMANAGER_TEAM_DLQ_KEY) : ""),
    embeddedTimeoutSeconds:
      intFrom(
        account.embeddedTimeoutSeconds ??
          (fromEnv ? env.CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS : undefined),
        DEFAULT_EMBEDDED_TIMEOUT_SECONDS,
      ),
  };
}

function keyPrefix(cfg) {
  return "claw:team:" + cfg.teamId;
}
function inboxKey(cfg, memberId = cfg.memberId) {
  if (memberId === cfg.memberId && cfg.inboxKey) return cfg.inboxKey;
  return keyPrefix(cfg) + ":inbox:" + memberId;
}
function eventsKey(cfg) {
  if (cfg.eventsKey) return cfg.eventsKey;
  return keyPrefix(cfg) + ":events";
}
function presenceKey(cfg) {
  if (cfg.presenceKey) return cfg.presenceKey;
  return keyPrefix(cfg) + ":presence";
}
function dlqKey(cfg) {
  if (cfg.dlqKey) return cfg.dlqKey;
  return keyPrefix(cfg) + ":dlq";
}
function hasRequiredRedisTeamKeys(cfg) {
  return !!(
    (cfg.teamId || cfg.inboxKey) &&
    (cfg.teamId || cfg.eventsKey) &&
    (cfg.teamId || cfg.presenceKey)
  );
}

// ============ Helpers ============
async function ensureDirs(cfg) {
  await fs.mkdir(path.join(cfg.sharedDir, "inbox"), { recursive: true });
  await fs.mkdir(path.join(cfg.sharedDir, "status"), { recursive: true });
  await fs.mkdir(path.join(cfg.sharedDir, "tasks"), { recursive: true });
  await fs.mkdir(path.join(cfg.sharedDir, "results"), { recursive: true });
  await fs.mkdir(path.join(cfg.sharedDir, ".openclaw-redis-team"), { recursive: true });
}

async function writeJson(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const tmp = file + "." + process.pid + "." + Date.now() + "." + randomUUID() + ".tmp";
  await fs.writeFile(tmp, JSON.stringify(value, null, 2) + "\n", "utf8");
  await fs.rename(tmp, file);
}

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return undefined;
  }
}

async function writeLocalStatus(cfg, patch = {}) {
  const file = path.join(cfg.sharedDir, "status", safeName(cfg.memberId) + ".json");
  const previous = (await readJson(file)) || {};
  const status = Object.assign(
    {
      teamId: cfg.teamId,
      memberId: cfg.memberId,
      role: cfg.role,
      liveness: "online",
      runtime: "openclaw",
      runtimeStatus: "running",
      availability: "idle",
      lastSeenAt: nowIso(),
    },
    previous,
    {
      teamId: cfg.teamId,
      memberId: cfg.memberId,
      role: cfg.role,
      lastSeenAt: nowIso(),
    },
    patch,
  );
  await writeJson(file, status);
  return status;
}

async function readStatuses(cfg, memberId) {
  const dir = path.join(cfg.sharedDir, "status");
  if (memberId) return (await readJson(path.join(dir, safeName(memberId) + ".json"))) || null;
  let entries = [];
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const out = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
    const value = await readJson(path.join(dir, entry.name));
    if (value) out.push(value);
  }
  out.sort((a, b) => String(a.memberId).localeCompare(String(b.memberId)));
  return out;
}

async function writeTaskEnvelope(cfg, envelope) {
  if (!envelope?.taskId) return;
  await ensureDirs(cfg);
  const aliases = new Set(taskIdAliases(envelope.taskId));
  aliases.add(envelope.taskId);
  for (const alias of aliases) {
    await writeJson(path.join(cfg.sharedDir, "tasks", safeName(alias) + ".json"), envelope);
  }
}

async function readTaskEnvelope(cfg, taskId) {
  await ensureDirs(cfg);
  for (const alias of taskIdAliases(taskId)) {
    const envelope = await readJson(path.join(cfg.sharedDir, "tasks", safeName(alias) + ".json"));
    if (envelope) return envelope;
  }
  return null;
}

function fieldsToObject(fields) {
  const out = {};
  if (!Array.isArray(fields)) return out;
  for (let i = 0; i < fields.length; i += 2)
    if (typeof fields[i] === "string") out[fields[i]] = fields[i + 1];
  return out;
}

function parseStreamMessage(id, fields) {
  const obj = fieldsToObject(fields);
  const flat = Object.assign({}, obj);
  delete flat.payload;
  if (typeof obj.payload === "string") {
    try {
      return Object.assign({ redisId: id }, flat, JSON.parse(obj.payload));
    } catch {
      return Object.assign({ redisId: id, rawPayload: obj.payload }, flat);
    }
  }
  return Object.assign({ redisId: id }, obj);
}

function parseReadGroupResponse(value) {
  const out = [];
  if (!Array.isArray(value)) return out;
  for (const stream of value) {
    if (!Array.isArray(stream) || !Array.isArray(stream[1])) continue;
    for (const item of stream[1])
      if (Array.isArray(item)) out.push(parseStreamMessage(item[0], item[1]));
  }
  return out;
}

async function xaddJson(redis, stream, event) {
  const fields = ["payload", JSON.stringify(event)];
  for (const key of [
    "event",
    "type",
    "messageId",
    "message_id",
    "completionMessageId",
    "completion_message_id",
    "memberId",
    "member_id",
    "taskId",
    "task_id",
    "availability",
    "runtimeStatus",
    "summary",
    "error",
    "status",
    "to",
    "text",
    "result",
    "resultMarkdown",
    "replyTo",
    "inReplyTo",
    "conversationId",
    "originalTo",
  ]) {
    if (event[key] !== undefined && event[key] !== null) {
      fields.push(key, String(event[key]));
    }
  }
  await redis.command("XADD", stream, "*", ...fields);
}

function eventFor(cfg, event, extra = {}) {
  return Object.assign(
    {
      v: SCHEMA_VERSION,
      event,
      type: event,
      teamId: cfg.teamId,
      team_id: cfg.teamId,
      memberId: cfg.memberId,
      member_id: cfg.memberId,
      role: cfg.role,
      runtime: "openclaw",
      runtimeStatus: "running",
      availability: "idle",
      at: nowIso(),
    },
    extra,
  );
}
function taskEvent(cfg, event, envelope, extra = {}) {
  return eventFor(
    cfg,
    event,
    Object.assign(
      {
        messageId: envelope.messageId,
        message_id: envelope.messageId,
        taskId: envelope.taskId,
        task_id: envelope.taskId,
        availability: "busy",
        runtimeStatus: "running",
        summary: event,
      },
      extra,
    ),
  );
}

// ============ Message Envelope ============
function normalizeEnvelope(raw) {
  if (!raw || typeof raw !== "object") return null;
  const envelope = {
    schemaVersion: raw.v || raw.schemaVersion || SCHEMA_VERSION,
    messageId: raw.messageId || raw.message_id || raw.id || ("msg_" + randomUUID()),
    taskId: raw.taskId || raw.task_id || ("task_" + randomUUID()),
    teamId: raw.teamId || raw.team_id,
    from: raw.from || raw.sender || raw.memberId || raw.member_id || "unknown",
    to: raw.to || raw.recipient || "",
    conversationId: raw.conversationId || raw.conversation_id || raw.taskId || raw.task_id,
    type: raw.type || "message",
    role: raw.role || "teammate",
    text: raw.text || raw.prompt || raw.rawPayload || "",
    priority: raw.priority || "normal",
    createdAt: raw.createdAt || raw.created_at || nowIso(),
    expiresAt: raw.expiresAt || raw.expires_at,
    contextRefs: Array.isArray(raw.contextRefs) ? raw.contextRefs.filter(Boolean) : [],
    artifacts: raw.artifacts || [],
    metadata: raw.metadata || {},
    idempotencyKey: raw.idempotencyKey || raw.messageId,
  };
  return envelope;
}

const seenMessageIds = new Set();
function dedup(key) {
  if (seenMessageIds.has(key)) return true;
  seenMessageIds.add(key);
  if (seenMessageIds.size > 10000) {
    const iter = seenMessageIds.values();
    for (let i = 0; i < 1000; i++) {
      const v = iter.next();
      if (!v.done) seenMessageIds.delete(v.value);
    }
  }
  return false;
}

// ============ Runtime Operations ============
function createRuntime(api) {
  let runtimeApi = api;
  let activeEnvelope = null;
  let activeTaskCompleted = false;
  let lastOutbound = null;

  async function withRedis(cfg, existingRedis, fn) {
    if (existingRedis) return fn(existingRedis);
    const redis = new RedisClient(cfg.redisUrl);
    await redis.connect();
    try {
      return await fn(redis);
    } finally {
      redis.close();
    }
  }

  function activeTaskMatches(taskId) {
    if (!activeEnvelope) return false;
    if (!taskId) return true;
    return taskIdsMatch(taskId, activeEnvelope.taskId);
  }

  function taskMatchesEnvelope(envelope, taskId) {
    if (!envelope) return false;
    if (!taskId) return true;
    return taskIdsMatch(taskId, envelope.taskId);
  }

  function firstText(...values) {
    for (const value of values) {
      if (typeof value === "string" && value.trim()) return value.trim();
      if (value && typeof value === "object") {
        const nested = firstText(value.text, value.content, value.result, value.resultMarkdown, value.summary);
        if (nested) return nested;
      }
    }
    return "";
  }

  function textFromDispatchResult(activeResult) {
    return firstText(
      activeResult?.outbound?.message?.text,
      activeResult?.result?.text,
      activeResult?.result?.message?.text,
      activeResult?.result?.response?.text,
      activeResult?.result?.content,
      activeResult?.result?.result,
      activeResult?.result?.resultMarkdown,
    );
  }

  function summarizeText(text, fallback = "Redis Team task completed") {
    const firstLine = String(text || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find(Boolean);
    return (firstLine || fallback).slice(0, 160);
  }

  async function completeActiveTaskFromText(text, meta = {}) {
    const cfg = meta.cfg || readChannelConfig(runtimeApi.config || {}, meta.accountId || "default");
    const envelope = meta.envelope || activeEnvelope;
    const result = firstText(text, meta.resultMarkdown, meta.result, meta.summary);
    if (!envelope || !taskMatchesEnvelope(envelope, meta.taskId || envelope.taskId) || !result) return false;
    const taskId = trim(meta.taskId) || envelope.taskId;
    const completionMessageId = trim(meta.messageId);
    const messageId = trim(meta.eventMessageId) || envelope.messageId || completionMessageId || ("msg_" + randomUUID());
    const inReplyTo = trim(meta.inReplyTo) || envelope.messageId;
    const resultMarkdown = typeof meta.resultMarkdown === "string" && meta.resultMarkdown.trim()
      ? meta.resultMarkdown
      : result;
    const summary = trim(meta.summary) || summarizeText(result);
    const artifactRefs = Array.isArray(meta.artifactRefs) ? meta.artifactRefs : [];

    await ensureDirs(cfg);
    await writeLocalStatus(cfg, {
      availability: "idle",
      runtimeStatus: "succeeded",
      currentTaskId: taskId,
      progress: 100,
      lastSummary: summary,
      artifactRefs,
    });
    await withRedis(cfg, meta.redis, async (redis) => {
      await xaddJson(redis, eventsKey(cfg), taskEvent(cfg, "task_completed", envelope, {
        messageId,
        message_id: messageId,
        completionMessageId: completionMessageId || undefined,
        completion_message_id: completionMessageId || undefined,
        sourceMessageId: envelope.messageId,
        source_message_id: envelope.messageId,
        taskId,
        task_id: taskId,
        inReplyTo,
        replyTo: inReplyTo,
        to: trim(meta.to) || undefined,
        availability: "idle",
        runtimeStatus: "succeeded",
        status: "succeeded",
        summary,
        result,
        resultMarkdown,
        artifactRefs,
      }));
    });
    if (!activeEnvelope || taskMatchesEnvelope(activeEnvelope, taskId)) activeTaskCompleted = true;
    return true;
  }

  async function failActiveTask(error, meta = {}) {
    const cfg = meta.cfg || readChannelConfig(runtimeApi.config || {}, meta.accountId || "default");
    const envelope = meta.envelope || activeEnvelope;
    const errorText = trim(error?.message) || trim(error) || "Redis Team task failed";
    const messageId = trim(meta.messageId) || envelope?.messageId || ("msg_" + randomUUID());
    const taskId = trim(meta.taskId) || envelope?.taskId || "";
    const inReplyTo = trim(meta.inReplyTo) || envelope?.messageId || undefined;
    const summary = trim(meta.summary) || errorText;
    const base = {
      messageId,
      message_id: messageId,
      sourceMessageId: envelope?.messageId,
      source_message_id: envelope?.messageId,
      taskId,
      task_id: taskId,
      inReplyTo,
      replyTo: inReplyTo,
      to: trim(meta.to) || undefined,
      availability: "blocked",
      runtimeStatus: "failed",
      status: "failed",
      summary,
      error: errorText,
    };

    await ensureDirs(cfg);
    if (envelope) {
      await writeLocalStatus(cfg, {
        availability: "blocked",
        runtimeStatus: "failed",
        currentTaskId: taskId || envelope.taskId,
        lastSummary: summary,
      });
    }

    await withRedis(cfg, meta.redis, async (redis) => {
      if (meta.eventName === "message_failed") {
        await xaddJson(
          redis,
          eventsKey(cfg),
          envelope ? taskEvent(cfg, "message_failed", envelope, base) : eventFor(cfg, "message_failed", base),
        );
      }
      await xaddJson(
        redis,
        eventsKey(cfg),
        envelope ? taskEvent(cfg, "task_failed", envelope, base) : eventFor(cfg, "task_failed", base),
      );
    });
    if (envelope && activeTaskMatches(taskId || envelope.taskId)) activeTaskCompleted = true;
    return false;
  }

  async function sendWithConfig(cfg, params) {
    params = params || {};
    if (!cfg.enabled) throw new Error("Redis Team channel is disabled");
    if (!cfg.redisUrl || !cfg.memberId || !hasRequiredRedisTeamKeys(cfg))
      throw new Error("Redis Team env is incomplete");
    await ensureDirs(cfg);

    const target = await resolveRedisTeamTarget(cfg, params.to);
    const status = await readStatuses(cfg, cfg.memberId);
    const requestedTaskId = trim(params.taskId);
    const statusIsActive =
      String(status?.availability || "").toLowerCase() === "busy" ||
      String(status?.runtimeStatus || "").toLowerCase() === "running";
    const inferredTaskId = requestedTaskId || (statusIsActive ? (status?.currentTaskId || status?.runtimeTaskId) : "") || "";
    const inferredEnvelope =
      activeTaskMatches(inferredTaskId)
        ? activeEnvelope
        : await readTaskEnvelope(cfg, inferredTaskId);
    const message = {
      v: SCHEMA_VERSION,
      messageId: "msg_" + randomUUID(),
      teamId: cfg.teamId,
      from: cfg.memberId,
      to: target.to,
      originalTo: target.originalTo,
      intent: trim(params.intent) || "send",
      taskId: requestedTaskId || inferredEnvelope?.taskId || activeEnvelope?.taskId || "task_" + randomUUID(),
      conversationId:
        inferredEnvelope?.conversationId ||
        inferredEnvelope?.taskId ||
        activeEnvelope?.conversationId ||
        activeEnvelope?.taskId ||
        undefined,
      title: trim(params.title) || "Team Message",
      text: trim(params.text) || trim(params.prompt) || "",
      contextRefs: Array.isArray(params.contextRefs) ? params.contextRefs.filter(Boolean) : [],
      ttlSeconds: typeof params.ttlSeconds === "number" ? params.ttlSeconds : 3600,
      priority: trim(params.priority) || "normal",
      metadata: params.metadata || {},
      createdAt: nowIso(),
    };

    const redis = new RedisClient(cfg.redisUrl);
    await redis.connect();
    try {
      if (target.route === "unknown") {
        await failActiveTask(target.error, {
          cfg,
          redis,
          envelope: inferredEnvelope,
          eventName: "message_failed",
          messageId: message.messageId,
          taskId: message.taskId,
          to: target.originalTo,
          summary: target.error,
        });
        lastOutbound = { message, target, failed: true, error: target.error };
        return Object.assign({}, message, { failed: true, error: target.error });
      }

      await xaddJson(redis, inboxKey(cfg, message.to), message);
      const outbound = {
        messageId: message.messageId,
        taskId: message.taskId,
        conversationId: message.conversationId,
        to: message.to,
        originalTo: message.originalTo,
        text: message.text,
        summary: message.title,
      };
      const completesActiveTask =
        !!inferredEnvelope &&
        taskMatchesEnvelope(inferredEnvelope, message.taskId) &&
        isActiveCompletionTarget(target.originalTo, cfg);
      const eventName = completesActiveTask || target.system ? "reply" : "outbound";
      await xaddJson(redis, eventsKey(cfg), eventFor(cfg, eventName, Object.assign({}, outbound, {
        to: target.originalTo,
        inReplyTo: inferredEnvelope?.messageId || activeEnvelope?.messageId,
      })));
      if (completesActiveTask) {
        if (message.text) {
          await completeActiveTaskFromText(message.text, {
            cfg,
            redis,
            envelope: inferredEnvelope,
            messageId: message.messageId,
            taskId: message.taskId,
            inReplyTo: inferredEnvelope?.messageId,
            summary: message.title,
            to: target.originalTo,
          });
        } else {
          await failActiveTask("completion target received empty text", {
            cfg,
            redis,
            envelope: inferredEnvelope,
            eventName: "message_failed",
            messageId: message.messageId,
            taskId: message.taskId,
            to: target.originalTo,
          });
        }
      }
      lastOutbound = { message, target };
    } finally {
      redis.close();
    }
    return message;
  }

  async function isTaskTerminal(cfg, envelope) {
    const status = await readStatuses(cfg, cfg.memberId);
    if (!status || !envelope?.taskId) return false;
    const statusTaskId = status.currentTaskId || status.runtimeTaskId;
    if (!taskIdsMatch(statusTaskId, envelope.taskId)) return false;
    return ["succeeded", "failed"].includes(String(status.runtimeStatus || "").toLowerCase());
  }

  return {
    async withActiveEnvelope(envelope, fn) {
      const prevEnvelope = activeEnvelope;
      const prevCompleted = activeTaskCompleted;
      const prevOutbound = lastOutbound;
      activeEnvelope = envelope;
      activeTaskCompleted = false;
      lastOutbound = null;
      try {
        const result = await fn();
        return { result, completed: activeTaskCompleted, outbound: lastOutbound };
      } finally {
        activeEnvelope = prevEnvelope;
        activeTaskCompleted = prevCompleted;
        lastOutbound = prevOutbound;
      }
    },

    async send(params) {
      const cfg = readChannelConfig(runtimeApi.config || {});
      return sendWithConfig(cfg, params);
    },

    async sendChannelText({ cfg, accountId, to, text }) {
      const config = readChannelConfig(cfg, accountId || "default");
      return sendWithConfig(config, {
        to,
        text,
        intent: "message",
        title: "Team Message",
      });
    },

    async status(memberId) {
      const cfg = readChannelConfig(runtimeApi.config || {});
      await ensureDirs(cfg);
      return readStatuses(cfg, memberId);
    },

    async updateProgress(params) {
      const cfg = readChannelConfig(runtimeApi.config || {});
      await ensureDirs(cfg);
      const status = await writeLocalStatus(cfg, {
        availability: params.status === "idle" ? "idle" : params.status,
        currentTaskId: params.taskId,
        progress: typeof params.progress === "number" ? params.progress : undefined,
        lastSummary: params.summary || params.status,
        artifactRefs: Array.isArray(params.artifactRefs) ? params.artifactRefs : [],
      });

      if (cfg.enabled && cfg.redisUrl && cfg.memberId && hasRequiredRedisTeamKeys(cfg)) {
        const redis = new RedisClient(cfg.redisUrl);
        await redis.connect();
        try {
          await xaddJson(redis, eventsKey(cfg), eventFor(cfg, "progress", params));
        } finally {
          redis.close();
        }
      }
      return status;
    },

    async completeTask(params) {
      const cfg = readChannelConfig(runtimeApi.config || {});
      await ensureDirs(cfg);
      const resultDir = path.join(cfg.sharedDir, "results", safeName(params.taskId));
      await fs.mkdir(resultDir, { recursive: true });
      const artifactRefs = Array.isArray(params.artifactRefs) ? params.artifactRefs.slice() : [];
      if (params.resultMarkdown) {
        await fs.writeFile(path.join(resultDir, "result.md"), params.resultMarkdown, "utf8");
        artifactRefs.push(path.join(resultDir, "result.md"));
      }
      await writeJson(
        path.join(resultDir, "result.json"),
        Object.assign({}, params, { artifactRefs, completedAt: nowIso() }),
      );
      const runtimeStatus = params.status === "succeeded" ? "succeeded" : "failed";
      const status = await writeLocalStatus(cfg, {
        availability: params.status === "succeeded" ? "idle" : "blocked",
        runtimeStatus,
        currentTaskId: params.taskId,
        progress: params.status === "succeeded" ? 100 : undefined,
        lastSummary: params.summary,
        artifactRefs,
      });

      if (cfg.enabled && cfg.redisUrl && cfg.memberId && hasRequiredRedisTeamKeys(cfg)) {
        const redis = new RedisClient(cfg.redisUrl);
        await redis.connect();
        try {
          const eventName = params.status === "succeeded" ? "task_completed" : "task_failed";
          const terminalEnvelope = activeTaskMatches(params.taskId)
            ? activeEnvelope
            : await readTaskEnvelope(cfg, params.taskId);
          if (terminalEnvelope && taskMatchesEnvelope(terminalEnvelope, params.taskId)) {
            if (params.status === "succeeded") {
              await completeActiveTaskFromText(params.resultMarkdown || params.summary, {
                cfg,
                redis,
                envelope: terminalEnvelope,
                taskId: params.taskId,
                summary: params.summary,
                resultMarkdown: params.resultMarkdown || params.summary,
                artifactRefs,
              });
            } else {
              await failActiveTask(params.summary || "Redis Team task failed", {
                cfg,
                redis,
                envelope: terminalEnvelope,
                taskId: params.taskId,
                summary: params.summary,
              });
            }
          } else {
            await xaddJson(redis, eventsKey(cfg), eventFor(cfg, eventName, Object.assign({}, params, {
              artifactRefs,
              availability: params.status === "succeeded" ? "idle" : "blocked",
              runtimeStatus,
            })));
          }
        } finally {
          redis.close();
        }
      }
      return { status, artifactRefs };
    },

    completeActiveTaskFromText,
    failActiveTask,
    isTaskTerminal,
    textFromDispatchResult,
  };
}

// ============ Consumer Logic ============
async function startConsumer(cfg, onMessage, log) {
  if (!cfg.enabled) {
    log.info("redis-team: disabled; skipping consumer");
    return null;
  }
  if (!cfg.redisUrl || !cfg.memberId || !hasRequiredRedisTeamKeys(cfg)) {
    log.warn("redis-team: missing redisUrl/memberId or Redis Team stream keys; consumer will not start");
    return null;
  }

  await ensureDirs(cfg);
  const redis = new RedisClient(cfg.redisUrl);
  const presenceRedis = new RedisClient(cfg.redisUrl);
  let running = true;
  let timer = null;

  try {
    await redis.connect();
    try {
      await redis.command("CLIENT", "SETNAME", redisClientName(cfg, "consumer"));
    } catch {}
    await presenceRedis.connect();
    try {
      await presenceRedis.command("CLIENT", "SETNAME", redisClientName(cfg, "presence"));
    } catch {}
    try {
      await presenceRedis.command("XGROUP", "CREATE", inboxKey(cfg), cfg.consumerGroup, "0", "MKSTREAM");
    } catch (err) {
      if (!String(err && err.message).includes("BUSYGROUP")) throw err;
    }
  } catch (err) {
    running = false;
    if (timer) clearInterval(timer);
    redis.close();
    presenceRedis.close();
    throw err;
  }

  async function emitPresence() {
    try {
      const status = await writeLocalStatus(cfg, {
        liveness: "online",
      });
      await presenceRedis.command("HSET", presenceKey(cfg), cfg.memberId, JSON.stringify(status));
    } catch (err) {
      log.warn("redis-team: presence update failed: " + (err.message || err));
    }
  }

  timer = setInterval(emitPresence, STATUS_INTERVAL_MS);
  await emitPresence();

  async function loop() {
    let readID = "0";
    let pendingDrainBatches = 3;
    while (running) {
      try {
        const response = await redis.command(
          "XREADGROUP",
          "GROUP",
          cfg.consumerGroup,
          cfg.memberId,
          "COUNT",
          10,
          "BLOCK",
          READ_BLOCK_MS,
          "STREAMS",
          inboxKey(cfg),
          readID,
        );
        const messages = parseReadGroupResponse(response);
        if (readID !== ">") {
          if (messages.length === 0) {
            readID = ">";
            log.info("redis-team: pending/history drain complete; switching to new messages");
          } else if (--pendingDrainBatches <= 0) {
            readID = ">";
            log.warn(
              "redis-team: pending/history drain limit reached; switching to new messages to avoid stale pending blocking the inbox",
            );
          }
        }
        for (const msg of messages) {
          try {
            const envelope = normalizeEnvelope(msg);
            if (!envelope) continue;
            if (dedup(envelope.idempotencyKey)) {
              log.info("redis-team: dedup skipped " + envelope.messageId);
              await redis.command("XACK", inboxKey(cfg), cfg.consumerGroup, msg.redisId);
              continue;
            }
            await writeTaskEnvelope(cfg, envelope);
            await xaddJson(
              redis,
              eventsKey(cfg),
              taskEvent(cfg, "task_received", envelope, {
                availability: "busy",
                runtimeStatus: "running",
                summary: "Redis Team task received",
              }),
            );
            await onMessage(envelope);
            await redis.command("XACK", inboxKey(cfg), cfg.consumerGroup, msg.redisId);
          } catch (err) {
            const error = err instanceof Error ? err.message : String(err);
            log.error("redis-team: message processing failed: " + error);
            const envelope = normalizeEnvelope(msg) || {};
            await xaddJson(
              redis,
              eventsKey(cfg),
              taskEvent(cfg, "task_failed", envelope, {
                availability: "blocked",
                runtimeStatus: "failed",
                summary: error,
                error,
              }),
            );
            await xaddJson(redis, dlqKey(cfg), eventFor(cfg, "dlq", { redisId: msg.redisId, error, message: msg }));
            try {
              await redis.command("XACK", inboxKey(cfg), cfg.consumerGroup, msg.redisId);
            } catch (ackErr) {
              log.warn("redis-team: XACK after dlq failed: " + (ackErr.message || String(ackErr)));
            }
          }
        }
      } catch (err) {
        if (!running) return;
        log.error("redis-team: consumer loop error: " + (err.message || String(err)));
        await new Promise((resolve) => setTimeout(resolve, 5000));
      }
    }
  }

  const loopPromise = loop();

  return {
    async stop() {
      running = false;
      if (timer) clearInterval(timer);
      redis.close();
      try {
        const status = await writeLocalStatus(cfg, {
          liveness: "offline",
        });
        await presenceRedis.command("HSET", presenceKey(cfg), cfg.memberId, JSON.stringify(status));
      } catch {}
      presenceRedis.close();
      await loopPromise.catch(() => {});
      log.info("redis-team: consumer stopped");
    },
  };
}

// ============ Tool Parameters ============
const teamSendParameters = {
  type: "object",
  additionalProperties: false,
  required: ["to", "text"],
  properties: {
    to: { type: "string", description: "Recipient member ID or 'broadcast'" },
    text: { type: "string", description: "Message content" },
    intent: { type: "string", description: "Message intent" },
    taskId: { type: "string" },
    title: { type: "string" },
    contextRefs: { type: "array", items: { type: "string" } },
    ttlSeconds: { type: "number", minimum: 1 },
    priority: { type: "string", enum: ["low", "normal", "high", "urgent"] },
    metadata: { type: "object" },
  },
};

const teamStatusParameters = {
  type: "object",
  additionalProperties: false,
  properties: {
    memberId: { type: "string" },
  },
};

const progressParameters = {
  type: "object",
  additionalProperties: false,
  required: ["taskId", "status"],
  properties: {
    taskId: { type: "string" },
    status: {
      type: "string",
      enum: ["idle", "busy", "blocked", "waiting_review", "succeeded", "failed"],
    },
    progress: { type: "number", minimum: 0, maximum: 100 },
    summary: { type: "string" },
    artifactRefs: { type: "array", items: { type: "string" } },
  },
};

const completeParameters = {
  type: "object",
  additionalProperties: false,
  required: ["taskId", "status", "summary"],
  properties: {
    taskId: { type: "string" },
    status: { type: "string", enum: ["succeeded", "failed", "blocked"] },
    summary: { type: "string" },
    resultMarkdown: { type: "string" },
    artifactRefs: { type: "array", items: { type: "string" } },
  },
};

// ============ Plugin Entry ============
export default definePluginEntry({
  id: PLUGIN_ID,
  name: "Redis Team",
  description: "Connects OpenClaw runtimes to a ClawManager Redis Streams team bus.",
  configSchema: {
    type: "object",
    additionalProperties: false,
    properties: {
      fromEnv: { type: "boolean", default: true },
      enabled: { type: "boolean" },
      redisUrl: { type: "string" },
      teamId: { type: "string" },
      memberId: { type: "string" },
      role: { type: "string" },
      sharedDir: { type: "string" },
      autoRun: { type: "boolean" },
      consumerGroup: { type: "string" },
      inboxKey: { type: "string" },
      eventsKey: { type: "string" },
      presenceKey: { type: "string" },
      dlqKey: { type: "string" },
      embeddedTimeoutSeconds: { type: "number", minimum: 1, default: 1800 },
      managerUrl: { type: "string" },
    },
  },
  register(api) {
    const runtime = createRuntime(api);
    const consumerHandles = new Map();

    function createConsumerEntry() {
      let resolveStopped = () => {};
      const stopped = new Promise((resolve) => {
        resolveStopped = resolve;
      });
      return { handle: null, starting: null, stopped, resolveStopped };
    }

    function resolveConsumerStopped(entry) {
      try {
        entry?.resolveStopped?.();
      } catch {}
    }

    async function waitForConsumerStop(accountId, entry, abortSignal) {
      if (abortSignal?.aborted) {
        await stopConsumer(accountId);
        return;
      }
      await new Promise((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          abortSignal?.removeEventListener?.("abort", onAbort);
          resolve();
        };
        const onAbort = () => {
          void stopConsumer(accountId).finally(finish);
        };
        abortSignal?.addEventListener?.("abort", onAbort, { once: true });
        entry.stopped.then(finish, finish);
      });
    }

    async function stopConsumer(accountId) {
      const key = accountId || "default";
      const entry = consumerHandles.get(key);
      if (!entry) return;
      consumerHandles.delete(key);
      try {
        const handle = entry.starting ? await entry.starting : entry.handle;
        if (handle) await handle.stop();
      } catch {}
      finally {
        resolveConsumerStopped(entry);
      }
    }

    // --- Register Tools (backward compatible) ---
    api.registerTool({
      name: "team_send",
      label: "Team Send",
      description: "Send a message to another team member via Redis Streams.",
      parameters: teamSendParameters,
      async execute(_id, params) {
        return { content: [{ type: "text", text: JSON.stringify({ ok: true, sent: await runtime.send(params || {}) }, null, 2) }] };
      },
    });
    api.registerTool({
      name: "team_status",
      label: "Team Status",
      description: "Read team member status snapshots.",
      parameters: teamStatusParameters,
      async execute(_id, params) {
        return { content: [{ type: "text", text: JSON.stringify({ ok: true, status: await runtime.status(params?.memberId) }, null, 2) }] };
      },
    });
    api.registerTool({
      name: "team_update_progress",
      label: "Team Update Progress",
      description: "Update this member's structured task status.",
      parameters: progressParameters,
      async execute(_id, params) {
        return { content: [{ type: "text", text: JSON.stringify({ ok: true, status: await runtime.updateProgress(params || {}) }, null, 2) }] };
      },
    });
    api.registerTool({
      name: "team_complete_task",
      label: "Team Complete Task",
      description: "Mark a team task complete or failed.",
      parameters: completeParameters,
      async execute(_id, params) {
        return { content: [{ type: "text", text: JSON.stringify({ ok: true, ...(await runtime.completeTask(params || {})) }, null, 2) }] };
      },
    });

    // --- Register Channel Plugin ---
    api.registerChannel({
      plugin: {
        id: CHANNEL_ID,
        meta: {
          id: CHANNEL_ID,
          label: "Redis Team",
          selectionLabel: "Redis Team",
          docsPath: "/docs/redis-team",
          blurb: "Connect to ClawManager Redis Streams Team Bus",
          order: 200,
        },
        capabilities: {
          chatTypes: ["direct"],
          media: false,
          polls: false,
          voice: false,
          voiceNote: false,
          video: false,
          webPagePreview: false,
          formattedText: true,
          messageActions: false,
          typingIndicators: false,
          presence: true,
          status: true,
          accountManagement: true,
          qrLogin: false,
          threadSupport: false,
        },
        config: {
          listAccountIds: (cfg) => {
            const accounts = cfg?.channels?.[CHANNEL_ID]?.accounts;
            return accounts ? Object.keys(accounts) : [];
          },
          resolveAccount: (cfg, accountId) => {
            return readChannelConfig(cfg, accountId || "default");
          },
          defaultAccountId: () => "default",
          isEnabled: (account) => account?.enabled ?? false,
          isConfigured: (account) => !!(account?.redisUrl && account?.memberId && hasRequiredRedisTeamKeys(account)),
          describeAccount: (account) => ({
            accountId: account?.accountId || "default",
            name: account?.teamId + "/" + account?.memberId,
            enabled: account?.enabled ?? false,
            configured: !!(account?.redisUrl && account?.teamId && account?.memberId),
          }),
        },
        configSchema: {
          type: "object",
          additionalProperties: false,
          properties: {
            enabled: { type: "boolean", default: false },
            accounts: {
              type: "object",
              additionalProperties: {
                type: "object",
                properties: {
                  redisUrl: { type: "string", description: "Redis connection URL" },
                  teamId: { type: "string", description: "Team identifier" },
                  memberId: { type: "string", description: "Your member ID in the team" },
                  role: { type: "string", default: "member" },
                  sharedDir: { type: "string", default: "/team" },
                  autoRun: { type: "boolean", default: true },
                  consumerGroup: { type: "string", default: "team-members" },
                  inboxKey: { type: "string" },
                  eventsKey: { type: "string" },
                  presenceKey: { type: "string" },
                  dlqKey: { type: "string" },
                  embeddedTimeoutSeconds: { type: "number", minimum: 1, default: 1800 },
                  fromEnv: { type: "boolean", default: true },
                },
              },
            },
          },
        },
        setup: {
          applyAccountConfig: ({ cfg, accountId, input }) => {
            const next = JSON.parse(JSON.stringify(cfg || {}));
            if (!next.channels) next.channels = {};
            if (!next.channels[CHANNEL_ID]) next.channels[CHANNEL_ID] = {};
            if (!next.channels[CHANNEL_ID].accounts) next.channels[CHANNEL_ID].accounts = {};

            const existing = next.channels[CHANNEL_ID].accounts[accountId] || {};
            next.channels[CHANNEL_ID].accounts[accountId] = {
              ...existing,
              redisUrl: input.redisUrl || existing.redisUrl || "",
              teamId: input.teamId || existing.teamId || "",
              memberId: input.memberId || existing.memberId || "",
              role: input.role || existing.role || "member",
              sharedDir: input.sharedDir || existing.sharedDir || "/team",
              autoRun: input.autoRun !== undefined ? input.autoRun : (existing.autoRun !== undefined ? existing.autoRun : true),
              consumerGroup: input.consumerGroup || existing.consumerGroup || "team-members",
              inboxKey: input.inboxKey || existing.inboxKey || "",
              eventsKey: input.eventsKey || existing.eventsKey || "",
              presenceKey: input.presenceKey || existing.presenceKey || "",
              dlqKey: input.dlqKey || existing.dlqKey || "",
              embeddedTimeoutSeconds: input.embeddedTimeoutSeconds || existing.embeddedTimeoutSeconds || 1800,
              fromEnv: input.fromEnv !== undefined ? input.fromEnv : (existing.fromEnv !== undefined ? existing.fromEnv : true),
            };

            return next;
          },
        },
        gateway: {
          startAccount: async (ctx) => {
            const accountId = ctx.accountId || "default";
            const cfg = readChannelConfig(ctx.cfg, ctx.accountId);
            if (!cfg.enabled) {
              ctx.log?.info?.("redis-team: channel disabled");
              return;
            }
            if (!cfg.redisUrl || !cfg.memberId || !hasRequiredRedisTeamKeys(cfg)) {
              ctx.log?.warn?.("redis-team: missing configuration");
              return;
            }

            ctx.setStatus({
              accountId: ctx.accountId,
              running: true,
              connected: false,
              statusState: "connecting",
            });

            const existing = consumerHandles.get(accountId);
            if (existing?.handle) {
              ctx.log?.info?.("redis-team: consumer already running for account " + accountId);
              ctx.setStatus({
                accountId: ctx.accountId,
                running: true,
                connected: true,
                lastConnectedAt: Date.now(),
                statusState: "online",
              });
              await waitForConsumerStop(accountId, existing, ctx.abortSignal);
              return;
            }
            if (existing?.starting) {
              ctx.log?.info?.("redis-team: consumer already starting for account " + accountId);
              await existing.starting;
              ctx.setStatus({
                accountId: ctx.accountId,
                running: true,
                connected: true,
                lastConnectedAt: Date.now(),
                statusState: "online",
              });
              await waitForConsumerStop(accountId, existing, ctx.abortSignal);
              return;
            }

            const entry = createConsumerEntry();
            consumerHandles.set(accountId, entry);
            try {
              entry.starting = startConsumer(
                cfg,
                async (envelope) => {
                  ctx.log?.info?.(
                    "redis-team: received message " + envelope.messageId + " type=" + envelope.type,
                  );
                  const emitTaskEvent = async (event, extra = {}) => {
                    const r = new RedisClient(cfg.redisUrl);
                    await r.connect();
                    try {
                      await xaddJson(r, eventsKey(cfg), taskEvent(cfg, event, envelope, extra));
                    } finally {
                      r.close();
                    }
                  };

                  if (!ctx.channelRuntime) {
                    ctx.log?.warn?.(
                      "redis-team: channelRuntime unavailable; start gateway with plugin runtime or open Web UI node",
                    );
                    await writeLocalStatus(cfg, {
                      availability: "blocked",
                      runtimeStatus: "failed",
                      currentTaskId: envelope.taskId,
                      lastSummary:
                        "Received (no channel runtime): " +
                        String(envelope.text || "").slice(0, 100),
                    });
                    await emitTaskEvent("task_failed", {
                      availability: "blocked",
                      runtimeStatus: "failed",
                      summary: "Redis Team task failed: channel runtime unavailable",
                      error: "channelRuntime unavailable",
                    });
                    return;
                  }

                  if (!cfg.autoRun) {
                    ctx.log?.info?.("redis-team: autoRun disabled; skipping agent dispatch");
                    await writeLocalStatus(cfg, {
                      availability: "blocked",
                      runtimeStatus: "failed",
                      currentTaskId: envelope.taskId,
                      lastSummary: "Received (autoRun off): " + String(envelope.text || "").slice(0, 120),
                    });
                    await emitTaskEvent("task_failed", {
                      availability: "blocked",
                      runtimeStatus: "failed",
                      summary: "Redis Team task failed: autorun disabled",
                      error: "CLAWMANAGER_TEAM_AUTORUN is disabled",
                    });
                    return;
                  }

                  const peerId = String(envelope.from || "unknown");
                  const createdMs = Date.parse(envelope.createdAt);
                  const ts = Number.isFinite(createdMs) ? createdMs : undefined;
                  const textIn = String(envelope.text || "");
                  const taskId = String(envelope.taskId || "");
                  const conversationId = String(envelope.conversationId || cfg.teamId || "");

                  await writeLocalStatus(cfg, {
                    availability: "busy",
                    runtimeStatus: "running",
                    currentTaskId: taskId,
                    lastSummary: "Redis Team task started",
                  });
                  await emitTaskEvent("task_started", {
                    availability: "busy",
                    runtimeStatus: "running",
                    summary: "Redis Team task started",
                  });

                  let deliveredReply = false;
                  let dispatchFailed = false;
                  const activeResult = await runtime.withActiveEnvelope(envelope, async () => {
                    await dispatchInboundDirectDmWithRuntime({
                    cfg: ctx.cfg,
                    runtime: { channel: ctx.channelRuntime },
                    channel: CHANNEL_ID,
                    channelLabel: "Redis Team",
                    accountId: ctx.accountId,
                    peer: { kind: "group", id: cfg.teamId },
                    senderId: peerId,
                    senderAddress: peerId,
                    recipientAddress: cfg.memberId,
                    conversationLabel: "Team " + cfg.teamId + " · task " + envelope.taskId,
                    rawBody: textIn,
                    messageId: envelope.messageId,
                    timestamp: ts,
                    commandAuthorized: true,
                    bodyForAgent: textIn,
                    provider: CHANNEL_ID,
                    surface: "Redis Team",
                    originatingChannel: CHANNEL_ID,
                    originatingTo: peerId,
                    extraContext: {
                      ChatType: "group",
                      NativeChannelId: conversationId,
                      RedisTeamTaskId: taskId,
                      UntrustedContext: [
                        "Redis Team context:",
                        "- teamId: " + cfg.teamId,
                        "- taskId: " + (taskId || "(none)"),
                        "- from: " + peerId,
                        "- to: " + cfg.memberId,
                        "- conversationId: " + conversationId,
                      ],
                    },
                    deliver: async (payload) => {
                      deliveredReply = true;
                      ctx.log?.info?.("redis-team: delivering reply for " + envelope.messageId);
                      const replyText = payload?.text || "";
                      const replyMessageId = "msg_" + randomUUID();
                      const r = new RedisClient(cfg.redisUrl);
                      await r.connect();
                      try {
                        await xaddJson(r, eventsKey(cfg), eventFor(cfg, "reply", {
                          messageId: replyMessageId,
                          message_id: replyMessageId,
                          inReplyTo: envelope.messageId,
                          taskId: envelope.taskId,
                          task_id: envelope.taskId,
                          text: payload?.text || "",
                          mediaUrls: payload?.mediaUrls,
                          mediaUrl: payload?.mediaUrl,
                        }));
                        if (replyText) {
                          await runtime.completeActiveTaskFromText(replyText, {
                            cfg,
                            redis: r,
                            envelope,
                            messageId: replyMessageId,
                            taskId: envelope.taskId,
                            inReplyTo: envelope.messageId,
                            summary: "Redis Team task completed",
                          });
                        } else {
                          await runtime.failActiveTask("dispatch delivered an empty reply", {
                            cfg,
                            redis: r,
                            envelope,
                            eventName: "message_failed",
                            messageId: replyMessageId,
                            taskId: envelope.taskId,
                            inReplyTo: envelope.messageId,
                          });
                        }
                      } finally {
                        r.close();
                      }
                    },
                    onRecordError: (err) => {
                      ctx.log?.error?.(
                        "redis-team: record inbound session failed: " + (err?.message || String(err)),
                      );
                    },
                    onDispatchError: (err, info) => {
                      dispatchFailed = true;
                      ctx.log?.error?.(
                        "redis-team: agent dispatch failed (" +
                          info.kind +
                          "): " +
                          (err?.message || String(err)),
                      );
                      void runtime.failActiveTask(err?.message || String(err), {
                        cfg,
                        envelope,
                        taskId: envelope.taskId,
                        summary: "Redis Team task dispatch failed",
                      }).catch((emitErr) => {
                        ctx.log?.warn?.(
                          "redis-team: failed to emit task_failed: " +
                            (emitErr?.message || String(emitErr)),
                        );
                      });
                    },
                    });
                  });

                  if (!deliveredReply && !dispatchFailed && !activeResult?.completed) {
                    if (await runtime.isTaskTerminal(cfg, envelope)) {
                      ctx.log?.info?.(
                        "redis-team: task " + envelope.taskId + " already terminal after dispatch",
                      );
                    } else {
                      const result = runtime.textFromDispatchResult(activeResult);
                      if (result) {
                        await runtime.completeActiveTaskFromText(result, {
                          cfg,
                          envelope,
                          taskId: envelope.taskId,
                          inReplyTo: envelope.messageId,
                          summary: "Redis Team task completed",
                        });
                      } else {
                        const error = "dispatch finished without reply/completion";
                        ctx.log?.warn?.(
                          "redis-team: " + error + " for " + envelope.messageId + "; marking task failed",
                        );
                        await runtime.failActiveTask(error, {
                          cfg,
                          envelope,
                          taskId: envelope.taskId,
                          summary: "Redis Team task failed",
                        });
                      }
                    }
                  }

                  ctx.setStatus({
                    accountId: ctx.accountId,
                    running: true,
                    connected: true,
                    lastConnectedAt: Date.now(),
                    statusState: "online",
                  });
                },
                ctx.log || console,
              );
              const handle = await entry.starting;
              if (consumerHandles.get(accountId) !== entry) {
                if (handle) await handle.stop();
                return;
              }
              entry.handle = handle;
              entry.starting = null;

              ctx.setStatus({
                accountId: ctx.accountId,
                running: true,
                connected: true,
                lastConnectedAt: Date.now(),
                statusState: "online",
              });
              await waitForConsumerStop(accountId, entry, ctx.abortSignal);
            } catch (err) {
              consumerHandles.delete(accountId);
              resolveConsumerStopped(entry);
              ctx.log?.error?.("redis-team: failed to start consumer: " + (err.message || String(err)));
              ctx.setStatus({
                accountId: ctx.accountId,
                running: true,
                connected: false,
                statusState: "error",
              });
              throw err;
            }
          },
          stopAccount: async (ctx) => {
            await stopConsumer(ctx.accountId);
            ctx.setStatus({
              accountId: ctx.accountId,
              running: false,
              connected: false,
              statusState: "offline",
            });
          },
          logoutAccount: async (ctx) => {
            await stopConsumer(ctx.accountId);
            ctx.setStatus({
              accountId: ctx.accountId,
              running: false,
              connected: false,
              statusState: "not configured",
            });
            return { cleared: true };
          },
        },
        status: {
          probeAccount: async ({ account, timeoutMs, cfg }) => {
            const config = readChannelConfig(cfg, account?.accountId || "default");
            if (!config.enabled) return { ok: false, reason: "disabled" };
            if (!config.redisUrl) return { ok: false, reason: "missing redisUrl" };
            try {
              const client = new RedisClient(config.redisUrl);
              await client.connect();
              await client.command("PING");
              client.close();
              return { ok: true, reason: "connected" };
            } catch (err) {
              return { ok: false, reason: err.message || "connection failed" };
            }
          },
          buildAccountSnapshot: ({ account, cfg }) => {
            const accountId = account?.accountId || "default";
            const config = readChannelConfig(cfg, accountId);
            const configured = !!(config.redisUrl && config.memberId && hasRequiredRedisTeamKeys(config));
            const consumer = consumerHandles.get(accountId);
            const active = !!(consumer?.handle || consumer?.starting);
            return {
              accountId,
              name: config.teamId + "/" + config.memberId,
              enabled: config.enabled,
              configured,
              linked: configured,
              running: config.enabled && configured && active,
              connected: config.enabled && configured && active,
              statusState: config.enabled && configured && active ? "online" : configured ? "offline" : "not configured",
            };
          },
        },
        security: {
          dm: {
            channelKey: CHANNEL_ID,
            resolvePolicy: () => "allow",
            resolveAllowFrom: () => [],
          },
        },
        lifecycle: {
          onAccountConfigChanged: async () => {
            // Config changes picked up on next restart
          },
          onAccountRemoved: async ({ accountId } = {}) => {
            await stopConsumer(accountId);
          },
        },
        outbound: {
          deliveryMode: "direct",
          chunker: null,
          textChunkLimit: 20000,
          sendText: async ({ cfg, accountId, to, text }) => {
            const sent = await runtime.sendChannelText({ cfg, accountId, to, text });
            return {
              channel: CHANNEL_ID,
              messageId: sent.messageId,
              chatId: sent.conversationId || sent.to,
              conversationId: sent.conversationId,
              meta: {
                taskId: sent.taskId,
                to: sent.to,
                originalTo: sent.originalTo,
                failed: sent.failed,
                error: sent.error,
              },
            };
          },
          base: {
            deliveryMode: "direct",
            chunker: null,
            textChunkLimit: 20000,
          },
          attachedResults: {
            channel: CHANNEL_ID,
            sendText: async ({ cfg, accountId, to, text }) => {
              return await runtime.sendChannelText({ cfg, accountId, to, text });
            },
          },
        },
        // Message adapter for standardized inbound/outbound
        message: {
          durableFinal: false,
          send: {
            text: async ({ cfg, accountId, to, text }) => {
              const sent = await runtime.sendChannelText({ cfg, accountId, to, text });
              return {
                messageId: sent.messageId,
                failed: sent.failed,
                error: sent.error,
              };
            },
          },
          receive: {
            defaultAckPolicy: "manual",
            supportedAckPolicies: ["manual"],
          },
        },
        messaging: {
          inferTargetChatType: ({ to }) => {
            const target = normalizeRedisTeamTarget(to);
            return target.completion ? "group" : "direct";
          },
          resolveOutboundSessionRoute: ({ cfg, accountId, target, resolvedTarget }) => {
            const config = readChannelConfig(cfg, accountId || "default");
            const normalized = normalizeRedisTeamTarget(target || resolvedTarget?.to, config);
            const chatType = resolvedTarget?.kind === "user" ? "direct" : "group";
            const peer = {
              kind: chatType,
              id: normalized.to,
            };
            const baseSessionKey = [
              "redis-team",
              safeName(accountId || "default"),
              safeName(chatType),
              safeName(normalized.to),
            ].join(":");
            return {
              sessionKey: baseSessionKey,
              baseSessionKey,
              peer,
              chatType,
              from: chatType === "direct" ? "redis-team:" + normalized.to : "redis-team:group:" + normalized.to,
              to: chatType === "direct" ? "user:" + normalized.to : "channel:" + normalized.to,
            };
          },
          normalizeTarget: (target) => {
            return normalizeRedisTeamTarget(target).to;
          },
          targetResolver: {
            looksLikeId: (raw, normalized) => {
              const value = trim(normalized) || trim(raw);
              return isActiveCompletionTarget(value) || isSafeMemberTarget(value);
            },
            hint: "<clawmanager|broadcast|team|member>",
            resolveTarget: async ({ cfg, accountId, input, normalized }) => {
              const config = readChannelConfig(cfg, accountId || "default");
              const target = normalizeRedisTeamTarget(normalized || input, config);
              if (target.completion) {
                return {
                  to: target.to,
                  kind: "group",
                  display: target.originalTo,
                  source: "normalized",
                };
              }
              if (!isSafeMemberTarget(target.to)) return null;
              return {
                to: target.to,
                kind: "user",
                display: target.to,
                source: "normalized",
              };
            },
          },
        },
      },
    });
  },
});
