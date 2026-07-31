"""Redis Team platform adapter for ClawManager-managed Hermes runtimes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

WIRE_SCHEMA_VERSION = 1
PROTOCOL_VERSION = 4
PROTOCOL_CAPABILITIES = [
    "completion_ack_v1",
    "automatic_turn_completion_v2",
    "assignment_heartbeat_v1",
    "durable_turn_facts_v1",
    "team_artifact_preview_v1",
    "review_contract_v1",
    "validation_contract_v2",
]
COMPLETION_SOURCE = "team_complete_task"
DEFAULT_SHARED_DIR = "/team"
DEFAULT_CONSUMER_GROUP = "team-members"
READ_BLOCK_MS = 5000
STATUS_INTERVAL_SECONDS = 30
TEAM_SHARED_DIR_MODE = 0o2775
TEAM_SHARED_FILE_MODE = 0o664
MAX_ARTIFACT_BYTES = 1024 * 1024


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _trim(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "unknown"


def _redis_key_part(value: Any) -> str:
    raw = _trim(value)
    if not raw:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_:.-]", "-", raw)


def _redis_client_name(settings: "RedisTeamSettings", purpose: str) -> str:
    return f"redis-team:{_safe_name(settings.team_id)}:{_safe_name(settings.member_id)}:{purpose}"[:512]


def _short_text(value: str, limit: int = 500) -> str:
    text = _trim(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
    path.parent.chmod(TEAM_SHARED_DIR_MODE)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000)}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(TEAM_SHARED_FILE_MODE)
    tmp.replace(path)
    path.chmod(TEAM_SHARED_FILE_MODE)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
    path.parent.chmod(TEAM_SHARED_DIR_MODE)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000)}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.chmod(TEAM_SHARED_FILE_MODE)
    tmp.replace(path)
    path.chmod(TEAM_SHARED_FILE_MODE)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@dataclass(frozen=True)
class RedisTeamSettings:
    enabled: bool
    redis_url: str
    team_id: str
    member_id: str
    role: str = "member"
    shared_dir: str = DEFAULT_SHARED_DIR
    auto_run: bool = True
    consumer_group: str = DEFAULT_CONSUMER_GROUP
    embedded_timeout_seconds: int = 1800
    manager_url: str = ""
    preview_origin: str = ""
    team_token: str = ""
    ready_file: str = ""

    @property
    def shared_path(self) -> Path:
        return Path(self.shared_dir)

    @property
    def valid(self) -> bool:
        return bool(self.enabled and self.redis_url and self.team_id and self.member_id)


def load_settings(config: PlatformConfig | None = None) -> RedisTeamSettings:
    extra = dict(getattr(config, "extra", {}) or {})
    from_env = _truthy(extra.get("from_env"), True)

    def pick(key: str, env_name: str, default: Any = "", *aliases: str) -> Any:
        for candidate in (key, *aliases):
            value = extra.get(candidate)
            if value not in (None, ""):
                return value
        if from_env:
            env_value = os.getenv(env_name)
            if env_value not in (None, ""):
                return env_value
        return default

    timeout_raw = pick(
        "embedded_timeout_seconds",
        "CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS",
        1800,
        "embeddedTimeoutSeconds",
    )
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        timeout = 1800

    return RedisTeamSettings(
        enabled=_truthy(pick("enabled", "CLAWMANAGER_TEAM_ENABLED", False), False),
        redis_url=_trim(pick("redis_url", "CLAWMANAGER_TEAM_REDIS_URL", "", "redisUrl")),
        team_id=_trim(pick("team_id", "CLAWMANAGER_TEAM_ID", "", "teamId")),
        member_id=_trim(pick("member_id", "CLAWMANAGER_TEAM_MEMBER_ID", "", "memberId")),
        role=_trim(pick("role", "CLAWMANAGER_TEAM_ROLE", "member")) or "member",
        shared_dir=_trim(pick("shared_dir", "CLAWMANAGER_TEAM_SHARED_DIR", DEFAULT_SHARED_DIR, "sharedDir"))
        or DEFAULT_SHARED_DIR,
        auto_run=_truthy(pick("auto_run", "CLAWMANAGER_TEAM_AUTORUN", True, "autoRun"), True),
        consumer_group=_trim(pick("consumer_group", "CLAWMANAGER_TEAM_CONSUMER_GROUP", DEFAULT_CONSUMER_GROUP, "consumerGroup"))
        or DEFAULT_CONSUMER_GROUP,
        embedded_timeout_seconds=max(1, timeout),
        manager_url=_trim(pick("manager_url", "CLAWMANAGER_TEAM_MANAGER_URL", "", "managerUrl")),
        preview_origin=_trim(pick("preview_origin", "CLAWMANAGER_TEAM_PREVIEW_ORIGIN", "", "previewOrigin")),
        team_token=_trim(pick("team_token", "CLAWMANAGER_TEAM_TOKEN", "", "teamToken")),
        ready_file=_trim(pick("ready_file", "CLAWMANAGER_TEAM_READY_FILE", "", "readyFile")),
    )


def _key_prefix(settings: RedisTeamSettings) -> str:
    return f"claw:team:{settings.team_id}"


def inbox_key(settings: RedisTeamSettings, member_id: Optional[str] = None) -> str:
    return f"{_key_prefix(settings)}:inbox:{member_id or settings.member_id}"


def events_key(settings: RedisTeamSettings) -> str:
    return f"{_key_prefix(settings)}:events"


def presence_key(settings: RedisTeamSettings) -> str:
    return f"{_key_prefix(settings)}:presence"


def dlq_key(settings: RedisTeamSettings) -> str:
    return f"{_key_prefix(settings)}:dlq"


def assignment_activity_key(settings: RedisTeamSettings, root_task_id: str, assignment_id: str) -> str:
    return (
        f"{_key_prefix(settings)}:assignment-activity:"
        f"{_redis_key_part(root_task_id)}:{_redis_key_part(assignment_id)}"
    )


def event_for(settings: RedisTeamSettings, event: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = {
        "v": WIRE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "protocolCapabilities": PROTOCOL_CAPABILITIES,
        "eventId": f"evt_{uuid.uuid4().hex}",
        "event": event,
        "teamId": settings.team_id,
        "memberId": settings.member_id,
        "role": settings.role,
        "at": _now_iso(),
    }
    if extra:
        payload.update(extra)
    return payload


def ensure_team_dirs(settings: RedisTeamSettings) -> None:
    for directory in [
        settings.shared_path,
        *(settings.shared_path / child for child in ("inbox", "status", "tasks", "results", "artifacts", "tmp", ".hermes-redis-team")),
    ]:
        directory.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
        directory.chmod(TEAM_SHARED_DIR_MODE)


def _active_envelope_path(settings: RedisTeamSettings) -> Path:
    return settings.shared_path / ".hermes-redis-team" / f"active-{_safe_name(settings.member_id)}.json"


def _load_active_envelope(settings: RedisTeamSettings) -> dict[str, Any]:
    value = _read_json(_active_envelope_path(settings))
    return value if isinstance(value, dict) else {}


def _persist_active_envelope(settings: RedisTeamSettings, envelope: dict[str, Any]) -> None:
    _atomic_write_json(_active_envelope_path(settings), envelope)


def _envelope_value(settings: RedisTeamSettings, args: dict[str, Any], *keys: str) -> str:
    active = _load_active_envelope(settings)
    for key in keys:
        value = _trim(args.get(key))
        if value:
            return value
        value = _trim(active.get(key))
        if value:
            return value
    return ""


def _artifact_relative_path(value: Any) -> Path:
    raw = _trim(value).replace("\\", "/")
    if raw.startswith("/team/"):
        raw = raw[len("/team/") :]
    if not raw or raw.startswith("/"):
        raise ValueError("Team artifact path must be a non-empty current-Team path")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Team artifact path traversal is not allowed")
    return Path(*parts)


def _assert_no_symlink_traversal(shared_root: Path, candidate: Path) -> None:
    resolved_root = shared_root.resolve()
    try:
        lexical_parts = candidate.relative_to(shared_root).parts
    except ValueError as exc:
        raise ValueError("Team artifact path escaped the current Team workspace") from exc
    current = shared_root
    for component in lexical_parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"Team artifact paths may not traverse symlinks: {current}")
    resolved_candidate = candidate.resolve(strict=False)
    resolved_parent = resolved_candidate.parent
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise ValueError("Team artifact path escaped the current Team workspace")


def _artifact_path(
    settings: RedisTeamSettings,
    args: dict[str, Any],
    *,
    default_scope: str,
    write: bool = False,
) -> Path:
    scope = _trim(args.get("scope") or default_scope).lower()
    relative = _artifact_relative_path(args.get("path"))
    shared_root = settings.shared_path
    if scope == "member":
        root_task_id = _envelope_value(settings, args, "rootTaskId", "root_task_id")
        assignment_id = _envelope_value(settings, args, "assignmentId", "assignment_id", "workId", "work_id")
        if not root_task_id or not assignment_id:
            raise ValueError("Active rootTaskId and assignmentId are required for member artifacts")
        root = (
            settings.shared_path
            / "artifacts"
            / _safe_name(root_task_id)
            / "members"
            / _safe_name(settings.member_id)
            / _safe_name(assignment_id)
        )
        candidate = root / relative
    elif scope == "team":
        if write and "leader" not in settings.role.lower() and "leader" not in settings.member_id.lower():
            raise ValueError("Only the Team Leader may write team-scoped artifacts")
        candidate = settings.shared_path / relative
    else:
        raise ValueError("Team artifact scope must be member or team")
    _assert_no_symlink_traversal(shared_root, candidate)
    return candidate


def canonical_artifact_ref(settings: RedisTeamSettings, path: Path) -> str:
    shared_root = settings.shared_path.resolve()
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(shared_root)
    except ValueError as exc:
        raise ValueError(f"artifact path escaped Redis Team shared directory: {path}") from exc
    return "/team/" + relative.as_posix()


def validate_artifact_refs(settings: RedisTeamSettings, refs: Optional[list[str]]) -> list[str]:
    validated: list[str] = []
    for ref in refs or []:
        raw = _trim(ref)
        if not raw:
            continue
        candidate = settings.shared_path / _artifact_relative_path(raw)
        _assert_no_symlink_traversal(settings.shared_path, candidate)
        if not candidate.is_file():
            raise ValueError(f"artifact reference is not a readable file: {ref}")
        canonical = canonical_artifact_ref(settings, candidate)
        if canonical not in validated:
            validated.append(canonical)
    return validated


def artifact_metadata(settings: RedisTeamSettings, refs: Optional[list[str]]) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    for canonical in validate_artifact_refs(settings, refs):
        target = settings.shared_path / _artifact_relative_path(canonical)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        metadata.append(
            {
                "path": canonical,
                "sha256": digest,
                "bytes": target.stat().st_size,
            }
        )
    return metadata


async def _tool_team_artifact_write(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="member", write=True)
        _atomic_write_text(target, str(args.get("content") or ""))
        return json.dumps(
            {"ok": True, "artifact": {"path": canonical_artifact_ref(settings, target), "bytes": target.stat().st_size}},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


async def _tool_team_artifact_read(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="team")
        max_bytes = min(MAX_ARTIFACT_BYTES, max(1, int(args.get("maxBytes") or 256 * 1024)))
        if not target.is_file():
            raise ValueError("Team artifact is not a file")
        if target.stat().st_size > max_bytes:
            raise ValueError("Team artifact exceeds maxBytes")
        return json.dumps(
            {
                "ok": True,
                "artifact": {
                    "path": canonical_artifact_ref(settings, target),
                    "content": target.read_text(encoding="utf-8"),
                    "bytes": target.stat().st_size,
                },
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


async def _tool_team_artifact_list(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="team")
        limit = min(200, max(1, int(args.get("limit") or 100)))
        if not target.is_dir():
            raise ValueError("Team artifact path is not a directory")
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: item.name)[:limit]:
            if child.is_symlink():
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "path": canonical_artifact_ref(settings, child),
                    "bytes": child.stat().st_size if child.is_file() else None,
                }
            )
        return json.dumps(
            {"ok": True, "path": canonical_artifact_ref(settings, target), "entries": entries},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


async def _tool_team_artifact_mkdir(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="member", write=True)
        target.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
        target.chmod(TEAM_SHARED_DIR_MODE)
        return json.dumps(
            {"ok": True, "artifact": {"path": canonical_artifact_ref(settings, target)}},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _preview_url(settings: RedisTeamSettings, target: Path) -> str:
    origin = settings.preview_origin.rstrip("/")
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    labels = hostname.split(".")
    managed_proxy = (
        parsed.scheme == "http"
        and labels
        and labels[0] == "clawmanager-egress-proxy"
        and len(labels) >= 3
        and labels[2] == "svc"
    )
    if not managed_proxy:
        raise ValueError("Team artifact Browser preview origin is unavailable")
    if not settings.team_token:
        raise ValueError("Team artifact Browser preview token is unavailable")
    canonical = canonical_artifact_ref(settings, target)
    parts = [part for part in canonical[len("/team/") :].split("/") if part]
    if not parts:
        raise ValueError("Team artifact preview requires a file")
    signed_prefix = "/".join(parts[:-1])
    encoded_prefix = (
        base64.urlsafe_b64encode(signed_prefix.encode("utf-8")).decode("ascii").rstrip("=")
        if signed_prefix
        else "_"
    )
    payload = f"team-preview-v1\n{settings.team_id}\n{signed_prefix}"
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(settings.team_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return (
        f"{parsed.scheme}://{parsed.netloc}/v1/"
        f"{quote(settings.team_id, safe='')}/{encoded_prefix}/{signature}/{quote(parts[-1], safe='')}"
    )


async def _tool_team_artifact_preview(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="team")
        if not target.is_file():
            raise ValueError("Team artifact preview requires a readable file")
        return json.dumps(
            {
                "ok": True,
                "artifact": {"path": canonical_artifact_ref(settings, target), "bytes": target.stat().st_size},
                "previewUrl": _preview_url(settings, target),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def write_local_status(settings: RedisTeamSettings, patch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    ensure_team_dirs(settings)
    path = settings.shared_path / "status" / f"{_safe_name(settings.member_id)}.json"
    previous = _read_json(path) or {}
    status = {
        "teamId": settings.team_id,
        "memberId": settings.member_id,
        "role": settings.role,
        "liveness": "online",
        "runtime": "hermes",
        "availability": "idle",
        "lastSeenAt": _now_iso(),
    }
    status.update(previous)
    status.update(
        {
            "teamId": settings.team_id,
            "memberId": settings.member_id,
            "role": settings.role,
            "lastSeenAt": _now_iso(),
        }
    )
    if patch:
        status.update({k: v for k, v in patch.items() if v is not None})
    _atomic_write_json(path, status)
    return status


def _clear_ready_file(settings: RedisTeamSettings) -> None:
    raw = _trim(settings.ready_file)
    if not raw:
        return
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("CLAWMANAGER_TEAM_READY_FILE must be absolute")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _publish_ready_file(settings: RedisTeamSettings, status: dict[str, Any]) -> None:
    raw = _trim(settings.ready_file)
    if not raw:
        return
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("CLAWMANAGER_TEAM_READY_FILE must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "ready": True,
                "teamId": settings.team_id,
                "memberId": settings.member_id,
                "runtime": "hermes",
                "protocolVersion": PROTOCOL_VERSION,
                "readyAt": _now_iso(),
                "presence": {
                    "liveness": status.get("liveness"),
                    "availability": status.get("availability"),
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def read_team_statuses(settings: RedisTeamSettings, member_id: str = "") -> Any:
    ensure_team_dirs(settings)
    status_dir = settings.shared_path / "status"
    if member_id:
        return _read_json(status_dir / f"{_safe_name(member_id)}.json")
    statuses = []
    for path in sorted(status_dir.glob("*.json")):
        value = _read_json(path)
        if value:
            statuses.append(value)
    statuses.sort(key=lambda item: str(item.get("memberId", "")))
    return statuses


def write_task_result(
    settings: RedisTeamSettings,
    task_id: str,
    *,
    envelope: Optional[dict[str, Any]] = None,
    status: str,
    summary: str,
    result_markdown: str = "",
    artifact_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    ensure_team_dirs(settings)
    task_id = task_id or f"task_{uuid.uuid4().hex}"
    refs = validate_artifact_refs(settings, artifact_refs)
    active = envelope if isinstance(envelope, dict) else _load_active_envelope(settings)
    root_task_id = _trim(active.get("rootTaskId")) or task_id
    assignment_id = _trim(active.get("assignmentId") or active.get("workId"))
    if status != "succeeded" and not refs and root_task_id and assignment_id:
        failure_report = (
            settings.shared_path
            / "artifacts"
            / _safe_name(root_task_id)
            / "members"
            / _safe_name(settings.member_id)
            / _safe_name(assignment_id)
            / "failure-result.md"
        )
        _assert_no_symlink_traversal(settings.shared_path, failure_report)
        _atomic_write_text(failure_report, result_markdown or summary)
        refs.append(canonical_artifact_ref(settings, failure_report))
    payload = {
        "taskId": task_id,
        "rootTaskId": root_task_id,
        "assignmentId": assignment_id or None,
        "memberId": settings.member_id,
        "status": status,
        "summary": summary,
        "resultMarkdown": result_markdown,
        "artifactRefs": refs,
        "completedAt": _now_iso(),
        "protocolVersion": PROTOCOL_VERSION,
    }
    completion_dir = settings.shared_path / ".hermes-redis-team" / "completions"
    completion_name = (
        f"{_safe_name(settings.member_id)}-"
        f"{_safe_name(task_id)}-"
        f"{_safe_name(assignment_id or 'unscoped')}.json"
    )
    _atomic_write_json(completion_dir / completion_name, payload)
    write_local_status(
        settings,
        {
            "availability": "idle" if status == "succeeded" else "blocked",
            "currentTaskId": task_id,
            "progress": 100 if status == "succeeded" else None,
            "lastSummary": summary,
            "artifactRefs": refs,
        },
    )
    return payload


def normalize_envelope(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "schemaVersion": raw.get("v") or raw.get("schemaVersion") or WIRE_SCHEMA_VERSION,
        "protocolVersion": raw.get("protocolVersion") or raw.get("protocol_version") or raw.get("v") or WIRE_SCHEMA_VERSION,
        "messageId": raw.get("messageId") or raw.get("id") or f"msg_{uuid.uuid4().hex}",
        "taskId": raw.get("taskId") or raw.get("task_id") or f"task_{uuid.uuid4().hex}",
        "rootTaskId": raw.get("rootTaskId") or raw.get("root_task_id") or raw.get("taskId") or raw.get("task_id"),
        "rootMessageId": raw.get("rootMessageId") or raw.get("root_message_id") or raw.get("messageId") or raw.get("id"),
        "workId": raw.get("workId") or raw.get("work_id") or raw.get("assignmentId") or raw.get("assignment_id"),
        "assignmentId": raw.get("assignmentId") or raw.get("assignment_id") or raw.get("workId") or raw.get("work_id"),
        "phaseId": raw.get("phaseId") or raw.get("phase_id"),
        "revision": raw.get("revision") or 1,
        "teamId": raw.get("teamId"),
        "from": raw.get("from") or raw.get("sender") or "unknown",
        "to": raw.get("to") or raw.get("recipient") or "",
        "conversationId": raw.get("conversationId") or raw.get("conversation_id") or raw.get("taskId") or raw.get("task_id"),
        "type": raw.get("type") or "message",
        "intent": raw.get("intent") or "",
        "role": raw.get("role") or "teammate",
        "text": raw.get("text") or raw.get("prompt") or raw.get("rawPayload") or "",
        "priority": raw.get("priority") or "normal",
        "createdAt": raw.get("createdAt") or raw.get("created_at") or _now_iso(),
        "expiresAt": raw.get("expiresAt") or raw.get("expires_at"),
        "contextRefs": [x for x in raw.get("contextRefs", []) if x] if isinstance(raw.get("contextRefs"), list) else [],
        "artifacts": raw.get("artifacts") or [],
        "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        "sessionKey": raw.get("sessionKey") or raw.get("approvalSessionKey") or "",
        "approval": raw.get("approval") if isinstance(raw.get("approval"), dict) else {},
        "requiresCompletion": raw.get("requiresCompletion", raw.get("requires_completion", True)),
        "responseLocale": raw.get("responseLocale") or raw.get("response_locale") or "zh-CN",
        "sharedWorkspace": raw.get("sharedWorkspace") if isinstance(raw.get("sharedWorkspace"), dict) else {},
        "idempotencyKey": raw.get("idempotencyKey") or raw.get("messageId") or raw.get("id"),
    }


def _reply_target(settings: RedisTeamSettings, metadata: dict[str, Any]) -> str:
    for key in ("reply_to_member", "from", "sender", "leader", "requester"):
        value = _trim(metadata.get(key))
        if value and value != settings.member_id:
            return value
    value = _trim(metadata.get("to"))
    if value and value != settings.member_id:
        return value
    return ""


def _approval_session_key(envelope: dict[str, Any]) -> str:
    metadata = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
    for value in (
        envelope.get("sessionKey"),
        envelope.get("approvalSessionKey"),
        metadata.get("sessionKey"),
        metadata.get("approvalSessionKey"),
    ):
        text = _trim(value)
        if text:
            return text
    approval = metadata.get("approval")
    if isinstance(approval, dict):
        for key in ("sessionKey", "approvalSessionKey"):
            text = _trim(approval.get(key))
            if text:
                return text
    approval = envelope.get("approval")
    if isinstance(approval, dict):
        for key in ("sessionKey", "approvalSessionKey"):
            text = _trim(approval.get(key))
            if text:
                return text
    return ""


def _parse_approval_command(text: str) -> Optional[tuple[str, str, bool]]:
    raw = _trim(text).lower()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    command = parts[0].lstrip("/")
    args = set(parts[1:])
    if command == "approve":
        if "always" in args:
            choice = "always"
        elif "session" in args:
            choice = "session"
        else:
            choice = "once"
        return ("approve", choice, "all" in args)
    if command == "deny":
        return ("deny", "deny", "all" in args)
    return None


def _completion_event_for_status(status: str) -> str:
    normalized = _trim(status).lower()
    if normalized == "succeeded":
        return "task_completed"
    if normalized == "cancelled":
        return "task_cancelled"
    return "task_failed"


def _completion_id(settings: RedisTeamSettings, envelope: dict[str, Any]) -> str:
    root = _safe_name(_trim(envelope.get("rootTaskId") or envelope.get("taskId")))
    assignment = _safe_name(_trim(envelope.get("assignmentId") or envelope.get("workId") or "assignment"))
    revision = max(1, int(envelope.get("revision") or 1))
    return f"completion:{_safe_name(settings.team_id)}:{root}:{_safe_name(settings.member_id)}:{assignment}:r{revision}"


def _task_event(
    settings: RedisTeamSettings,
    event: str,
    envelope: dict[str, Any],
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "messageId": envelope.get("messageId"),
        "sourceMessageId": envelope.get("messageId"),
        "taskId": envelope.get("taskId"),
        "rootTaskId": envelope.get("rootTaskId") or envelope.get("taskId"),
        "rootMessageId": envelope.get("rootMessageId"),
        "workId": envelope.get("assignmentId") or envelope.get("workId"),
        "assignmentId": envelope.get("assignmentId") or envelope.get("workId"),
        "canonicalWorkId": envelope.get("assignmentId") or envelope.get("workId"),
        "phaseId": envelope.get("phaseId"),
        "revision": max(1, int(envelope.get("revision") or 1)),
        "inReplyTo": envelope.get("messageId"),
        "requiresCompletion": envelope.get("requiresCompletion", True),
    }
    if extra:
        payload.update(extra)
    return event_for(settings, event, payload)


def _processed_message_key(settings: RedisTeamSettings, key: str) -> str:
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
    return f"{_key_prefix(settings)}:processed:{_redis_key_part(settings.member_id)}:{digest}"


def _completion_attempt_key(settings: RedisTeamSettings, completion_id: str, attempt_id: str) -> str:
    return (
        f"{_key_prefix(settings)}:completion-attempt:"
        f"{_redis_key_part(completion_id)}:{_redis_key_part(attempt_id)}"
    )


def _completion_ack_key(settings: RedisTeamSettings, completion_id: str, attempt_id: str) -> str:
    return (
        f"{_key_prefix(settings)}:completion-ack:"
        f"{_redis_key_part(completion_id)}:{_redis_key_part(attempt_id)}"
    )


def _completion_state_key(settings: RedisTeamSettings, completion_id: str) -> str:
    return f"{_key_prefix(settings)}:completion-state:{_redis_key_part(completion_id)}"


class RespError(RuntimeError):
    pass


class AsyncRedisClient:
    """Small RESP2 client for the Redis commands used by the Team Bus."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._url = urlparse(redis_url)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._command_lock = asyncio.Lock()

    async def connect(self) -> None:
        scheme = self._url.scheme or "redis"
        if scheme not in {"redis", "rediss"}:
            raise ValueError(f"unsupported Redis URL scheme: {scheme}")
        host = self._url.hostname or "localhost"
        port = self._url.port or (6380 if scheme == "rediss" else 6379)
        ssl_ctx = ssl.create_default_context() if scheme == "rediss" else None
        self._reader, self._writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
        if self._url.password:
            if self._url.username:
                await self.command("AUTH", unquote(self._url.username), unquote(self._url.password))
            else:
                await self.command("AUTH", unquote(self._url.password))
        db = (self._url.path or "").lstrip("/")
        if db:
            await self.command("SELECT", db)

    async def command(self, *parts: Any) -> Any:
        async with self._command_lock:
            if self._reader is None or self._writer is None:
                raise RuntimeError("Redis client is not connected")
            encoded_parts = [str(part).encode("utf-8") for part in parts]
            frame = [f"*{len(encoded_parts)}\r\n".encode("ascii")]
            for part in encoded_parts:
                frame.append(f"${len(part)}\r\n".encode("ascii"))
                frame.append(part)
                frame.append(b"\r\n")
            self._writer.write(b"".join(frame))
            await self._writer.drain()
            return await self._read_value()

    async def _read_value(self) -> Any:
        assert self._reader is not None
        prefix = await self._reader.readexactly(1)
        if prefix == b"+":
            return (await self._reader.readline()).rstrip(b"\r\n").decode("utf-8", "replace")
        if prefix == b"-":
            message = (await self._reader.readline()).rstrip(b"\r\n").decode("utf-8", "replace")
            raise RespError(message)
        if prefix == b":":
            return int((await self._reader.readline()).rstrip(b"\r\n"))
        if prefix == b"$":
            size = int((await self._reader.readline()).rstrip(b"\r\n"))
            if size < 0:
                return None
            data = await self._reader.readexactly(size)
            await self._reader.readexactly(2)
            return data.decode("utf-8", "replace")
        if prefix == b"*":
            size = int((await self._reader.readline()).rstrip(b"\r\n"))
            if size < 0:
                return None
            return [await self._read_value() for _ in range(size)]
        raise RespError(f"unknown Redis RESP prefix: {prefix!r}")

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()


def _stream_fields_to_dict(fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(fields, list):
        return out
    for idx in range(0, len(fields), 2):
        if idx + 1 < len(fields):
            out[str(fields[idx])] = fields[idx + 1]
    return out


def _parse_stream_response(value: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return messages
    for stream in value:
        if not isinstance(stream, list) or len(stream) < 2 or not isinstance(stream[1], list):
            continue
        for item in stream[1]:
            if not isinstance(item, list) or len(item) < 2:
                continue
            fields = _stream_fields_to_dict(item[1])
            payload = fields.get("payload")
            if isinstance(payload, str):
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"rawPayload": payload}
            else:
                parsed = dict(fields)
            if isinstance(parsed, dict):
                parsed["redisId"] = item[0]
                messages.append(parsed)
    return messages


async def xadd_json(redis: AsyncRedisClient, stream: str, event: dict[str, Any]) -> Any:
    return await redis.command("XADD", stream, "*", "payload", json.dumps(event, ensure_ascii=False))


async def _publish_once(
    redis: AsyncRedisClient,
    settings: RedisTeamSettings,
    dedupe_key: str,
    event: dict[str, Any],
    *,
    ttl_seconds: int = 7 * 24 * 60 * 60,
) -> dict[str, Any]:
    claimed = await redis.command("SET", dedupe_key, json.dumps(event, ensure_ascii=False), "NX", "EX", ttl_seconds)
    if claimed != "OK":
        return {"published": False, "streamId": ""}
    stream_id = await xadd_json(redis, events_key(settings), event)
    return {"published": True, "streamId": stream_id}


async def _completion_ack(
    redis: AsyncRedisClient,
    settings: RedisTeamSettings,
    completion_id: str,
    attempt_id: str,
    *,
    wait_seconds: float = 5.0,
) -> Optional[dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        for key in (
            _completion_ack_key(settings, completion_id, attempt_id),
            _completion_state_key(settings, completion_id),
        ):
            raw = await redis.command("GET", key)
            if raw:
                try:
                    value = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    value = None
                if isinstance(value, dict):
                    return value
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _observe_late_completion(
    settings: RedisTeamSettings,
    completion_id: str,
    attempt_id: str,
    task_id: str,
    status: str,
    summary: str,
    artifact_refs: list[str],
) -> None:
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        acknowledgement = await _completion_ack(
            redis,
            settings,
            completion_id,
            attempt_id,
            wait_seconds=300,
        )
    except Exception:
        return
    finally:
        redis.close()
    decision = _trim((acknowledgement or {}).get("decision")).lower()
    if decision not in {"accepted", "rejected"}:
        return
    active = _load_active_envelope(settings)
    if _trim(active.get("taskId") or active.get("rootTaskId")) == task_id:
        active["completionDecision"] = decision
        active["terminal"] = decision == "accepted"
        active["terminalStatus"] = status if decision == "accepted" else ""
        active["completionId"] = completion_id
        if decision == "accepted":
            active["completedAt"] = _now_iso()
        _persist_active_envelope(settings, active)
    write_local_status(
        settings,
        {
            "availability": "idle" if decision == "accepted" and status == "succeeded" else "blocked",
            "runtimeStatus": status if decision == "accepted" else "blocked",
            "currentTaskId": task_id,
            "progress": 100 if decision == "accepted" else 99,
            "lastSummary": _trim((acknowledgement or {}).get("reason")) or summary,
            "artifactRefs": artifact_refs,
        },
    )


def _substantive_final_text(value: Any) -> bool:
    text = _trim(value)
    if len(text) < 12:
        return False
    normalized = " ".join(text.lower().split())
    if normalized.endswith("?") or normalized.endswith("？"):
        return False
    generic = {
        "done",
        "completed",
        "task completed",
        "redis team task processing completed",
        "agent 回合已结束，正在等待显式完成回执。",
    }
    return normalized not in generic


async def _propose_completion(
    settings: RedisTeamSettings,
    envelope: dict[str, Any],
    *,
    status: str,
    summary: str,
    result_markdown: str,
    artifact_refs: Optional[list[str]] = None,
    explicit: bool,
    automatic_turn_result: bool = False,
    review_verdict: str = "",
    reviewed_assignment_id: str = "",
    reviewed_revision: Optional[int] = None,
    reviewed_artifact_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not settings.valid:
        raise ValueError("Redis Team env is incomplete")
    status = _trim(status).lower()
    if status not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("completion status must be succeeded, failed or cancelled")
    task_id = _trim(envelope.get("taskId") or envelope.get("rootTaskId"))
    if not task_id:
        raise ValueError("active task identity is unavailable")
    result = write_task_result(
        settings,
        task_id,
        envelope=envelope,
        status=status,
        summary=summary,
        result_markdown=result_markdown,
        artifact_refs=artifact_refs,
    )
    completion_id = _completion_id(settings, envelope)
    attempt_id = f"attempt_{uuid.uuid4().hex}"
    artifact_meta = artifact_metadata(settings, result["artifactRefs"])
    reviewed_refs = validate_artifact_refs(settings, reviewed_artifact_refs)
    reviewed_meta = artifact_metadata(settings, reviewed_refs)
    result_content_hash = hashlib.sha256(
        (result_markdown + "\n" + "\n".join(result["artifactRefs"])).encode("utf-8")
    ).hexdigest()
    completion = _task_event(
        settings,
        "completion_proposed",
        envelope,
        {
            "completionId": completion_id,
            "attemptId": attempt_id,
            # Runtime-authored natural completion still uses the same strict
            # completion envelope as the explicit tool. automaticTurnResult
            # records how it was produced without weakening control-plane
            # validation or falling back to prose heuristics.
            "completionSource": COMPLETION_SOURCE,
            "explicitCompletion": True,
            "agentInvokedCompletionTool": explicit or None,
            "automaticTurnResult": automatic_turn_result or None,
            "assignmentResultOnly": True,
            "rootTaskTerminal": False,
            "status": status,
            "availability": "idle" if status == "succeeded" else "blocked",
            "runtimeStatus": "completion_pending",
            "workflowFinal": False,
            "finalAnswerReady": False,
            "summary": summary,
            "result": result_markdown,
            "resultMarkdown": result_markdown,
            "artifactRefs": result["artifactRefs"],
            "artifactMetadata": artifact_meta,
            "resultContentHash": result_content_hash,
            "reviewVerdict": review_verdict or None,
            "reviewedAssignmentId": reviewed_assignment_id or None,
            "reviewedRevision": reviewed_revision,
            "reviewedArtifactRefs": reviewed_refs,
            "reviewedArtifactMetadata": reviewed_meta,
            "visibleToChat": False,
            "chatPolicy": "hidden",
        },
    )
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        published = await _publish_once(
            redis,
            settings,
            _completion_attempt_key(settings, completion_id, attempt_id),
            completion,
        )
        acknowledgement = await _completion_ack(redis, settings, completion_id, attempt_id)
    finally:
        redis.close()
    decision = _trim((acknowledgement or {}).get("decision")).lower() or "submitted"
    runtime_status = "completion_pending"
    availability = "busy"
    if decision == "accepted":
        runtime_status = status
        availability = "idle" if status == "succeeded" else "blocked"
    elif decision == "rejected":
        runtime_status = "blocked"
        availability = "blocked"
    active = _load_active_envelope(settings)
    if _trim(active.get("taskId") or active.get("rootTaskId")) == task_id:
        active["completionDecision"] = decision
        active["completionId"] = completion_id
        if explicit:
            active["explicitCompletionSubmitted"] = True
        if decision == "accepted":
            active["terminal"] = True
            active["terminalStatus"] = status
            active["completedAt"] = _now_iso()
        elif decision == "rejected":
            active["terminal"] = False
            active["terminalStatus"] = ""
        _persist_active_envelope(settings, active)
    if decision in {"submitted", "deferred"}:
        asyncio.create_task(
            _observe_late_completion(
                settings,
                completion_id,
                attempt_id,
                task_id,
                status,
                summary,
                result["artifactRefs"],
            )
        )
    write_local_status(
        settings,
        {
            "availability": availability,
            "runtimeStatus": runtime_status,
            "currentTaskId": task_id,
            "currentAssignmentId": envelope.get("assignmentId") or envelope.get("workId"),
            "progress": 100 if decision == "accepted" else 99,
            "lastSummary": _trim((acknowledgement or {}).get("reason")) or summary,
            "artifactRefs": result["artifactRefs"],
        },
    )
    return {
        "ok": True,
        **result,
        **published,
        "completionId": completion_id,
        "attemptId": attempt_id,
        "decision": decision,
        "acknowledgement": acknowledgement,
    }


async def _publish_event(settings: RedisTeamSettings, event: str, payload: dict[str, Any]) -> None:
    if not settings.valid:
        return
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        await xadd_json(redis, events_key(settings), event_for(settings, event, payload))
    finally:
        redis.close()


async def _tool_team_send(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    if not settings.valid:
        return json.dumps({"error": "Redis Team env is incomplete"}, ensure_ascii=False)
    to = _trim(args.get("to"))
    text = _trim(args.get("text") or args.get("prompt"))
    if not to or not text:
        return json.dumps({"error": "to and text are required"}, ensure_ascii=False)
    active = _load_active_envelope(settings)
    task_id = _trim(args.get("taskId")) or _trim(active.get("taskId") or active.get("rootTaskId"))
    message = {
        "v": WIRE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "messageId": f"msg_{uuid.uuid4().hex}",
        "teamId": settings.team_id,
        "from": settings.member_id,
        "to": to,
        "intent": _trim(args.get("intent")) or "send",
        "taskId": task_id,
        "rootTaskId": _trim(active.get("rootTaskId") or task_id),
        "rootMessageId": _trim(active.get("rootMessageId")),
        "workId": _trim(args.get("workId")) or _trim(active.get("assignmentId") or active.get("workId")),
        "assignmentId": _trim(args.get("assignmentId")) or _trim(active.get("assignmentId") or active.get("workId")),
        "phaseId": _trim(args.get("phaseId")) or _trim(active.get("phaseId")),
        "revision": int(args.get("revision") or active.get("revision") or 1),
        "title": _trim(args.get("title")) or "Team Message",
        "text": text,
        "contextRefs": args.get("contextRefs") if isinstance(args.get("contextRefs"), list) else [],
        "ttlSeconds": args.get("ttlSeconds") if isinstance(args.get("ttlSeconds"), int) else 3600,
        "priority": _trim(args.get("priority")) or "normal",
        "metadata": args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
        "createdAt": _now_iso(),
    }
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        redis_id = await xadd_json(redis, inbox_key(settings, to), message)
        await xadd_json(
            redis,
            events_key(settings),
            event_for(settings, "outbound", {"messageId": message["messageId"], "to": to}),
        )
    finally:
        redis.close()
    message["redisId"] = redis_id
    return json.dumps({"ok": True, "sent": message}, ensure_ascii=False)


async def _tool_team_status(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    if not settings.enabled:
        return json.dumps({"error": "Redis Team is disabled"}, ensure_ascii=False)
    return json.dumps(
        {"ok": True, "status": read_team_statuses(settings, _trim(args.get("memberId")))},
        ensure_ascii=False,
    )


async def _tool_team_update_progress(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    if not settings.enabled:
        return json.dumps({"error": "Redis Team is disabled"}, ensure_ascii=False)
    active = _load_active_envelope(settings)
    task_id = _trim(args.get("taskId")) or _trim(active.get("taskId") or active.get("rootTaskId"))
    status_text = _trim(args.get("status"))
    summary = _trim(args.get("summary"))
    if not task_id or not status_text:
        return json.dumps({"error": "taskId and status are required"}, ensure_ascii=False)
    progress = args.get("progress")
    status = write_local_status(
        settings,
        {
            "availability": "idle" if status_text == "idle" else status_text,
            "currentTaskId": task_id,
            "progress": progress if isinstance(progress, (int, float)) else None,
            "lastSummary": summary or status_text,
            "artifactRefs": args.get("artifactRefs") if isinstance(args.get("artifactRefs"), list) else [],
        },
    )
    progress_payload = {
        **dict(args),
        "taskId": task_id,
        "rootTaskId": active.get("rootTaskId") or task_id,
        "rootMessageId": active.get("rootMessageId"),
        "workId": active.get("assignmentId") or active.get("workId"),
        "assignmentId": active.get("assignmentId") or active.get("workId"),
        "phaseId": active.get("phaseId"),
        "revision": active.get("revision") or 1,
        "eventKind": _trim(args.get("eventKind")) or "worker_progress",
    }
    await _publish_event(settings, "task_progress", progress_payload)
    return json.dumps({"ok": True, "status": status}, ensure_ascii=False)


async def _tool_team_complete_task(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    if not settings.enabled:
        return json.dumps({"error": "Redis Team is disabled"}, ensure_ascii=False)
    active = _load_active_envelope(settings)
    task_id = _trim(args.get("taskId")) or _trim(active.get("taskId") or active.get("rootTaskId"))
    status_text = _trim(args.get("status")).lower()
    summary = _trim(args.get("summary"))
    if not task_id or not status_text or not summary:
        return json.dumps({"error": "taskId, status and summary are required"}, ensure_ascii=False)
    if status_text not in {"succeeded", "failed", "cancelled"}:
        return json.dumps(
            {"error": "status must be succeeded, failed or cancelled"},
            ensure_ascii=False,
        )
    active["taskId"] = task_id
    _persist_active_envelope(settings, active)
    try:
        result = await _propose_completion(
            settings,
            active,
            status=status_text,
            summary=summary,
            result_markdown=_trim(args.get("resultMarkdown")) or summary,
            artifact_refs=args.get("artifactRefs") if isinstance(args.get("artifactRefs"), list) else [],
            explicit=True,
            review_verdict=_trim(args.get("reviewVerdict")),
            reviewed_assignment_id=_trim(args.get("reviewedAssignmentId")),
            reviewed_revision=int(args.get("reviewedRevision")) if args.get("reviewedRevision") is not None else None,
            reviewed_artifact_refs=args.get("reviewedArtifactRefs") if isinstance(args.get("reviewedArtifactRefs"), list) else [],
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


class RedisTeamAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("redis_team"))
        self.settings = load_settings(config)
        self._redis: Optional[AsyncRedisClient] = None
        self._consumer_redis: Optional[AsyncRedisClient] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None
        self._lifecycle_lock = asyncio.Lock()
        self._redis_reply_metadata: Dict[str, Dict[str, Any]] = {}
        self._approval_session_by_key: Dict[str, str] = {}
        self._latest_approval_session_key = ""

    @property
    def name(self) -> str:
        return "Redis Team"

    async def connect(self, is_reconnect: bool = False, **_kwargs: Any) -> bool:
        async with self._lifecycle_lock:
            try:
                _clear_ready_file(self.settings)
            except Exception as exc:
                logger.error("Redis Team: invalid readiness path: %s", exc)
                self._set_fatal_error("readiness_path_invalid", str(exc), retryable=False)
                return False
            if not self.settings.enabled:
                logger.info("Redis Team: disabled")
                return False
            if not self.settings.valid:
                logger.error("Redis Team: CLAWMANAGER_TEAM_REDIS_URL, TEAM_ID and MEMBER_ID are required")
                self._set_fatal_error("config_missing", "Redis Team env is incomplete", retryable=False)
                return False
            if not Path(self.settings.shared_dir).is_absolute():
                logger.error("Redis Team: CLAWMANAGER_TEAM_SHARED_DIR must be absolute")
                self._set_fatal_error("invalid_shared_dir", "CLAWMANAGER_TEAM_SHARED_DIR must be absolute", retryable=False)
                return False
            if self._consumer_task and not self._consumer_task.done():
                logger.info("Redis Team: consumer already running for member=%s", self.settings.member_id)
                return True

            await self._disconnect_unlocked(mark_offline=False)
            ensure_team_dirs(self.settings)
            write_local_status(self.settings, {"availability": "idle"})
            self._redis = AsyncRedisClient(self.settings.redis_url)
            self._consumer_redis = AsyncRedisClient(self.settings.redis_url)
            try:
                await self._redis.connect()
                try:
                    await self._redis.command("CLIENT", "SETNAME", _redis_client_name(self.settings, "presence"))
                except Exception:
                    pass
                await self._consumer_redis.connect()
                try:
                    await self._consumer_redis.command("CLIENT", "SETNAME", _redis_client_name(self.settings, "consumer"))
                except Exception:
                    pass
                try:
                    await self._redis.command(
                        "XGROUP",
                        "CREATE",
                        inbox_key(self.settings),
                        self.settings.consumer_group,
                        "0",
                        "MKSTREAM",
                    )
                except RespError as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise
                initial_status = write_local_status(self.settings, {"availability": "idle"})
                await self._redis.command(
                    "HSET",
                    presence_key(self.settings),
                    self.settings.member_id,
                    json.dumps(initial_status, ensure_ascii=False),
                )
            except Exception as exc:
                logger.error("Redis Team: failed to connect: %s", exc)
                self._set_fatal_error("connect_failed", str(exc), retryable=True)
                try:
                    _clear_ready_file(self.settings)
                except Exception:
                    pass
                if self._consumer_redis:
                    self._consumer_redis.close()
                    self._consumer_redis = None
                if self._redis:
                    self._redis.close()
                    self._redis = None
                return False

            self._mark_connected()
            self._presence_task = asyncio.create_task(self._presence_loop())
            self._consumer_task = asyncio.create_task(self._consumer_loop())
            try:
                _publish_ready_file(self.settings, initial_status)
            except Exception as exc:
                logger.error("Redis Team: failed to publish readiness: %s", exc)
                self._set_fatal_error("readiness_publish_failed", str(exc), retryable=False)
                await self._disconnect_unlocked(mark_offline=False)
                return False
            logger.info(
                "Redis Team: connected team=%s member=%s group=%s",
                self.settings.team_id,
                self.settings.member_id,
                self.settings.consumer_group,
            )
            return True

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            await self._disconnect_unlocked(mark_offline=True)

    async def _disconnect_unlocked(self, *, mark_offline: bool) -> None:
        self._mark_disconnected()
        try:
            _clear_ready_file(self.settings)
        except Exception:
            pass
        for task in (self._consumer_task, self._presence_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._consumer_task = None
        self._presence_task = None
        if mark_offline and self._redis:
            try:
                await self._redis.command(
                    "HSET",
                    presence_key(self.settings),
                    self.settings.member_id,
                    json.dumps(
                        {
                            "teamId": self.settings.team_id,
                            "memberId": self.settings.member_id,
                            "role": self.settings.role,
                            "liveness": "offline",
                            "lastSeenAt": _now_iso(),
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass
        if self._consumer_redis:
            self._consumer_redis.close()
            self._consumer_redis = None
        if self._redis:
            self._redis.close()
            self._redis = None
        if mark_offline:
            write_local_status(self.settings, {"liveness": "offline"})

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        task_id = metadata.get("task_id") or chat_id
        target = _reply_target(self.settings, metadata)
        active = _load_active_envelope(self.settings)
        active["lastAssistantResponse"] = content
        active["lastAssistantResponseAt"] = _now_iso()
        _persist_active_envelope(self.settings, active)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        message_id = f"agent-narrative:{_safe_name(str(active.get('messageId') or task_id))}:{content_hash[:24]}"
        event = _task_event(
            self.settings,
            "reply",
            active,
            {
                "messageId": message_id,
                "taskId": task_id,
                "conversationId": metadata.get("conversation_id") or chat_id,
                "to": target,
                "text": content,
                "content": content,
                "replyTo": reply_to,
                "eventKind": "agent_narrative",
                "messageKind": "narrative",
                "chatPolicy": "visible",
                "visibleToChat": True,
                "nonAuthoritative": True,
                "stateEffect": "none",
                "contentHash": content_hash,
                "narrativeSource": "hermes_deliver_callback",
            },
        )
        try:
            write_local_status(
                self.settings,
                {
                    "availability": "idle",
                    "currentTaskId": task_id,
                    "lastSummary": _short_text(content),
                },
            )
            if self._redis:
                await _publish_once(
                    self._redis,
                    self.settings,
                    _processed_message_key(self.settings, message_id),
                    event,
                )
        except Exception as exc:
            logger.warning("Redis Team: failed to publish reply: %s", exc)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=message_id)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        task_id = metadata.get("task_id") or chat_id
        conversation_id = metadata.get("conversation_id") or chat_id
        target = _reply_target(self.settings, metadata)
        approval_id = f"approval_{uuid.uuid4().hex}"
        self._remember_approval_session(session_key, task_id, conversation_id, target)
        text = (
            "Dangerous command requires approval.\n\n"
            f"Command:\n{command}\n\n"
            f"Reason: {description or 'command flagged'}\n\n"
            f"Approval sessionKey: {session_key}\n\n"
            "Reply with /approve, /approve session, /approve always, or /deny. "
            "Include this approval sessionKey in metadata when replying if the "
            "reply does not reuse the original taskId and conversationId."
        )
        approval_payload = {
            "v": WIRE_SCHEMA_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "messageId": approval_id,
            "teamId": self.settings.team_id,
            "from": self.settings.member_id,
            "to": target,
            "taskId": task_id,
            "conversationId": conversation_id,
            "type": "approval_request",
            "intent": "approval_request",
            "role": self.settings.role,
            "text": text,
            "approval": {
                "sessionKey": session_key,
                "command": command,
                "description": description,
                "choices": ["approve", "approve session", "approve always", "deny"],
            },
            "metadata": {
                "sessionKey": session_key,
                "approvalSessionKey": session_key,
                "taskId": task_id,
                "conversationId": conversation_id,
                "commandPreview": _short_text(command, 300),
                "description": description,
            },
            "createdAt": _now_iso(),
        }
        event = event_for(
            self.settings,
            "approval_request",
            {
                "messageId": approval_id,
                "taskId": task_id,
                "conversationId": conversation_id,
                "to": target,
                "text": text,
                "summary": _short_text(f"Approval required: {description or command}"),
                "sessionKey": session_key,
                "approvalSessionKey": session_key,
                "approval": approval_payload["approval"],
                "metadata": approval_payload["metadata"],
            },
        )
        try:
            write_local_status(
                self.settings,
                {
                    "availability": "waiting_approval",
                    "currentTaskId": task_id,
                    "lastSummary": _short_text(f"Waiting for approval: {description or command}"),
                    "approvalSessionKey": session_key,
                },
            )
            if self._redis:
                await xadd_json(self._redis, events_key(self.settings), event)
                if target:
                    await xadd_json(self._redis, inbox_key(self.settings, target), approval_payload)
        except Exception as exc:
            logger.warning("Redis Team: failed to publish approval request: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=True, message_id=approval_id, raw_response=approval_payload)

    def _remember_approval_session(
        self,
        session_key: str,
        task_id: Any = "",
        conversation_id: Any = "",
        target: Any = "",
    ) -> None:
        session_key = _trim(session_key)
        if not session_key:
            return
        self._latest_approval_session_key = session_key
        for value in (task_id, conversation_id, target):
            text = _trim(value)
            if text:
                self._approval_session_by_key[text] = session_key

    def _session_key_for_approval_response(self, envelope: dict[str, Any]) -> str:
        session_key = _approval_session_key(envelope)
        if session_key:
            return session_key
        for value in (
            envelope.get("taskId"),
            envelope.get("conversationId"),
            envelope.get("from"),
            envelope.get("to"),
        ):
            mapped = self._approval_session_by_key.get(_trim(value))
            if mapped:
                return mapped
        return self._latest_approval_session_key

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            write_local_status(
                self.settings,
                {
                    "availability": "running",
                    "currentTaskId": chat_id,
                    "lastSummary": "Hermes is processing the Redis Team task",
                },
            )
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": f"Redis Team task {chat_id}", "type": "dm"}

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        envelope = event.raw_message if isinstance(event.raw_message, dict) else _load_active_envelope(self.settings)
        task_id = str(envelope.get("taskId") or event.source.chat_id)
        message_id = str(envelope.get("messageId") or event.message_id or "")
        if outcome == ProcessingOutcome.SUCCESS:
            active = _load_active_envelope(self.settings)
            response = _trim(active.get("lastAssistantResponse"))
            if not _truthy(envelope.get("requiresCompletion"), True):
                if self._redis:
                    await xadd_json(
                        self._redis,
                        events_key(self.settings),
                        _task_event(
                            self.settings,
                            "turn_finished_without_completion",
                            envelope,
                            {
                                "messageId": message_id,
                                "summary": "Non-terminal Team turn finished",
                                "stateEffect": "none",
                                "visibleToChat": False,
                                "chatPolicy": "hidden",
                            },
                        ),
                    )
                write_local_status(
                    self.settings,
                    {
                        "availability": "idle",
                        "runtimeStatus": "idle",
                        "currentTaskId": task_id,
                        "lastSummary": _short_text(response) or "Non-terminal Team turn finished",
                    },
                )
            elif active.get("terminal"):
                terminal_status = _trim(active.get("terminalStatus")) or "succeeded"
                write_local_status(
                    self.settings,
                    {
                        "availability": "idle" if terminal_status == "succeeded" else "blocked",
                        "runtimeStatus": terminal_status,
                        "currentTaskId": task_id,
                        "progress": 100,
                        "lastSummary": _short_text(response) or f"Completion {terminal_status}",
                    },
                )
            elif _trim(active.get("completionDecision")).lower() == "rejected":
                write_local_status(
                    self.settings,
                    {
                        "availability": "blocked",
                        "runtimeStatus": "blocked",
                        "currentTaskId": task_id,
                        "progress": 99,
                        "lastSummary": _short_text(response) or "Completion was rejected",
                    },
                )
            elif active.get("explicitCompletionSubmitted"):
                write_local_status(
                    self.settings,
                    {
                        "availability": "busy",
                        "runtimeStatus": "completion_pending",
                        "currentTaskId": task_id,
                        "progress": 99,
                        "lastSummary": _short_text(response) or "Completion submitted",
                    },
                )
            elif _substantive_final_text(response):
                await _propose_completion(
                    self.settings,
                    envelope,
                    status="succeeded",
                    summary=_short_text(response, 500),
                    result_markdown=response,
                    explicit=False,
                    automatic_turn_result=True,
                )
            else:
                write_local_status(
                    self.settings,
                    {
                        "availability": "busy",
                        "runtimeStatus": "running",
                        "currentTaskId": task_id,
                        "lastSummary": _short_text(response) or "Turn finished without a final result",
                    },
                )
                if self._redis:
                    await xadd_json(
                        self._redis,
                        events_key(self.settings),
                        _task_event(
                            self.settings,
                            "turn_finished_without_completion",
                            envelope,
                            {
                                "messageId": message_id,
                                "summary": "Turn finished without a substantive final result",
                                "stateEffect": "none",
                                "visibleToChat": False,
                                "chatPolicy": "hidden",
                            },
                        ),
                    )
        else:
            status = "cancelled" if outcome == ProcessingOutcome.CANCELLED else "failed"
            summary = f"Redis Team task attempt {status}"
            write_local_status(
                self.settings,
                {
                    "availability": "busy",
                    "runtimeStatus": "retryable_error",
                    "currentTaskId": task_id,
                    "lastSummary": summary,
                },
            )
            if self._redis:
                await xadd_json(
                    self._redis,
                    events_key(self.settings),
                    _task_event(
                        self.settings,
                        "assignment_attempt_failed",
                        envelope,
                        {
                            "messageId": message_id,
                            "taskId": task_id,
                            "summary": summary,
                            "retryable": True,
                            "stateEffect": "none",
                            "visibleToChat": False,
                            "chatPolicy": "hidden",
                        },
                    ),
                )
        self._redis_reply_metadata.pop(task_id, None)

    async def _presence_loop(self) -> None:
        assert self._redis is not None
        while self.is_connected:
            try:
                status = write_local_status(self.settings)
                await self._redis.command(
                    "HSET",
                    presence_key(self.settings),
                    self.settings.member_id,
                    json.dumps(status, ensure_ascii=False),
                )
                active = _load_active_envelope(self.settings)
                root_task_id = _trim(active.get("rootTaskId") or active.get("taskId"))
                assignment_id = _trim(active.get("assignmentId") or active.get("workId"))
                if root_task_id and assignment_id and not active.get("terminal"):
                    activity = {
                        "teamId": self.settings.team_id,
                        "memberId": self.settings.member_id,
                        "rootTaskId": root_task_id,
                        "assignmentId": assignment_id,
                        "workId": assignment_id,
                        "revision": max(1, int(active.get("revision") or 1)),
                        "state": "running",
                        "observedAt": _now_iso(),
                        "runtime": "hermes",
                    }
                    await self._redis.command(
                        "SET",
                        assignment_activity_key(self.settings, root_task_id, assignment_id),
                        json.dumps(activity, ensure_ascii=False),
                        "EX",
                        120,
                    )
                    await xadd_json(
                        self._redis,
                        events_key(self.settings),
                        _task_event(
                            self.settings,
                            "assignment_heartbeat",
                            active,
                            {
                                "summary": "Hermes Team assignment is active",
                                "stateEffect": "none",
                                "visibleToChat": False,
                                "chatPolicy": "hidden",
                            },
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Redis Team: presence update failed: %s", exc)
            await asyncio.sleep(STATUS_INTERVAL_SECONDS)

    async def _consumer_loop(self) -> None:
        assert self._consumer_redis is not None
        redis = self._consumer_redis
        pending = True
        while self.is_connected:
            try:
                response = await redis.command(
                    "XREADGROUP",
                    "GROUP",
                    self.settings.consumer_group,
                    self.settings.member_id,
                    "COUNT",
                    10,
                    "BLOCK",
                    READ_BLOCK_MS,
                    "STREAMS",
                    inbox_key(self.settings),
                    "0" if pending else ">",
                )
                if pending and not response:
                    pending = False
                    continue
                for raw in _parse_stream_response(response):
                    await self._handle_redis_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Redis Team: consumer loop error: %s", exc)
                await asyncio.sleep(5)

    async def _handle_redis_message(self, raw: dict[str, Any]) -> None:
        assert self._redis is not None
        redis_id = raw.get("redisId")
        envelope = normalize_envelope(raw)
        if not envelope:
            return
        dedup_key = envelope.get("idempotencyKey") or envelope["messageId"]
        if await self._redis.command("GET", _processed_message_key(self.settings, dedup_key)):
            if redis_id:
                await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)
            return

        try:
            await xadd_json(
                self._redis,
                events_key(self.settings),
                event_for(
                    self.settings,
                    "inbound",
                    {
                        "messageId": envelope["messageId"],
                        "taskId": envelope["taskId"],
                        "from": envelope["from"],
                    },
                ),
            )
            if await self._try_resolve_approval_response(envelope):
                if redis_id:
                    await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)
                return
            if self.settings.auto_run:
                envelope["explicitCompletionSubmitted"] = False
                envelope.pop("lastAssistantResponse", None)
                _persist_active_envelope(self.settings, envelope)
                await self._dispatch_envelope(envelope)
            else:
                write_local_status(
                    self.settings,
                    {
                        "availability": "idle",
                        "currentTaskId": envelope["taskId"],
                        "lastSummary": "Redis Team task received; autorun is disabled",
                    },
                )
            await self._redis.command(
                "SET",
                _processed_message_key(self.settings, dedup_key),
                _now_iso(),
                "EX",
                7 * 24 * 60 * 60,
            )
            if redis_id:
                await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)
        except Exception as exc:
            error = str(exc)
            logger.warning("Redis Team: message processing failed: %s", error)
            retry_key = _processed_message_key(self.settings, f"retry:{dedup_key}")
            retry_count = int(await self._redis.command("INCR", retry_key))
            await self._redis.command("EXPIRE", retry_key, 60 * 60)
            if retry_count <= 3:
                await xadd_json(
                    self._redis,
                    events_key(self.settings),
                    _task_event(
                        self.settings,
                        "assignment_attempt_failed",
                        envelope,
                        {
                            "error": error,
                            "summary": "Hermes Team assignment attempt failed and remains retryable",
                            "retryable": True,
                            "retryCount": retry_count,
                            "stateEffect": "none",
                            "visibleToChat": False,
                            "chatPolicy": "hidden",
                        },
                    ),
                )
                write_local_status(
                    self.settings,
                    {
                        "availability": "busy",
                        "runtimeStatus": "retryable_error",
                        "currentTaskId": envelope["taskId"],
                        "lastSummary": error,
                    },
                )
                await asyncio.sleep(min(2**retry_count, 8))
                await self._handle_redis_message(raw)
                return
            await xadd_json(
                self._redis,
                dlq_key(self.settings),
                event_for(self.settings, "dlq", {"redisId": redis_id, "error": error, "message": raw}),
            )
            await xadd_json(
                self._redis,
                events_key(self.settings),
                _task_event(
                    self.settings,
                    "task_failed",
                    envelope,
                    {
                        "status": "failed",
                        "summary": error,
                        "error": error,
                        "completionSource": "runtime_dispatch_error",
                        "explicitCompletion": False,
                    },
                ),
            )
            if redis_id:
                await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)

    async def _try_resolve_approval_response(self, envelope: dict[str, Any]) -> bool:
        parsed = _parse_approval_command(str(envelope.get("text") or ""))
        if parsed is None:
            return False
        session_key = self._session_key_for_approval_response(envelope)
        if not session_key:
            return False

        command_name, choice, resolve_all = parsed
        try:
            from tools.approval import has_blocking_approval, resolve_gateway_approval
        except Exception as exc:
            logger.warning("Redis Team: approval resolver unavailable: %s", exc)
            return False

        if not has_blocking_approval(session_key):
            await xadd_json(
                self._redis,
                events_key(self.settings),
                event_for(
                    self.settings,
                    "approval_response",
                    {
                        "taskId": envelope["taskId"],
                        "conversationId": envelope.get("conversationId"),
                        "from": envelope.get("from"),
                        "status": "no_pending_approval",
                        "sessionKey": session_key,
                    },
                ),
            )
            return True

        count = resolve_gateway_approval(session_key, choice, resolve_all=resolve_all)
        if count:
            self._latest_approval_session_key = ""
            self._approval_session_by_key = {
                key: value for key, value in self._approval_session_by_key.items() if value != session_key
            }
        status_text = "denied" if command_name == "deny" else "approved"
        write_local_status(
            self.settings,
            {
                "availability": "running" if count else "idle",
                "currentTaskId": envelope["taskId"],
                "lastSummary": f"Redis Team approval {status_text}: {count} command(s)",
                "approvalSessionKey": session_key,
            },
        )
        await xadd_json(
            self._redis,
            events_key(self.settings),
            event_for(
                self.settings,
                "approval_response",
                {
                    "taskId": envelope["taskId"],
                    "conversationId": envelope.get("conversationId"),
                    "from": envelope.get("from"),
                    "status": status_text,
                    "choice": choice,
                    "resolvedCount": count,
                    "resolveAll": resolve_all,
                    "sessionKey": session_key,
                },
            ),
        )
        return True

    async def _dispatch_envelope(self, envelope: dict[str, Any]) -> None:
        source = SessionSource(
            platform=Platform("redis_team"),
            chat_id=str(envelope["taskId"]),
            chat_name=f"Team {self.settings.team_id}",
            chat_type="dm",
            user_id=str(envelope["from"]),
            user_name=str(envelope.get("role") or envelope["from"]),
            thread_id=str(envelope.get("conversationId") or envelope["taskId"]),
            message_id=str(envelope["messageId"]),
        )
        text = str(envelope.get("text") or "")
        if not text.strip():
            text = json.dumps(envelope, ensure_ascii=False)
        context_refs = envelope.get("contextRefs") or []
        if context_refs:
            text += "\n\nContext refs:\n" + "\n".join(f"- {ref}" for ref in context_refs)
        root_task_id = _trim(envelope.get("rootTaskId") or envelope.get("taskId"))
        assignment_id = _trim(envelope.get("assignmentId") or envelope.get("workId"))
        member_artifact_root = (
            f"/team/artifacts/{_safe_name(root_task_id)}/members/"
            f"{_safe_name(self.settings.member_id)}/{_safe_name(assignment_id)}"
            if root_task_id and assignment_id
            else ""
        )
        text += (
            "\n\nClawManager Team context:\n"
            f"- rootTaskId: {root_task_id}\n"
            f"- rootMessageId: {_trim(envelope.get('rootMessageId'))}\n"
            f"- assignmentId/workId: {assignment_id}\n"
            f"- revision: {max(1, int(envelope.get('revision') or 1))}\n"
            f"- requiresCompletion: {bool(envelope.get('requiresCompletion', True))}\n"
        )
        if member_artifact_root:
            text += f"- Current member artifact root: {member_artifact_root}\n"
        text += (
            "- The Runtime inherits these IDs for Team tools; omit optional IDs instead of inventing replacements.\n"
            "- A substantive final response is submitted automatically. Use team_complete_task for explicit "
            "failure/cancellation, review metadata, or when you need to control final result fields.\n"
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope,
            message_id=str(envelope["messageId"]),
            internal=True,
        )
        metadata = {
            "task_id": envelope["taskId"],
            "conversation_id": envelope.get("conversationId"),
            "from": envelope.get("from"),
            "to": envelope.get("to"),
            "root_task_id": envelope.get("rootTaskId"),
            "root_message_id": envelope.get("rootMessageId"),
            "work_id": envelope.get("workId"),
            "assignment_id": envelope.get("assignmentId"),
            "phase_id": envelope.get("phaseId"),
            "revision": envelope.get("revision"),
        }
        write_local_status(
            self.settings,
            {
                "availability": "running",
                "currentTaskId": envelope["taskId"],
                "lastSummary": text[:500],
            },
        )
        self._redis_reply_metadata[str(envelope["taskId"])] = metadata
        await self.handle_message(event)

    async def _send_with_retry(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ):
        merged = dict(self._redis_reply_metadata.get(str(chat_id), {}))
        if metadata:
            merged.update(metadata)
        return await super()._send_with_retry(
            chat_id,
            content,
            reply_to=reply_to,
            metadata=merged,
            max_retries=max_retries,
            base_delay=base_delay,
        )


def check_requirements() -> bool:
    return _truthy(os.getenv("CLAWMANAGER_TEAM_ENABLED"), False) and bool(
        os.getenv("CLAWMANAGER_TEAM_REDIS_URL")
        and os.getenv("CLAWMANAGER_TEAM_ID")
        and os.getenv("CLAWMANAGER_TEAM_MEMBER_ID")
    )


def validate_config(config: PlatformConfig) -> bool:
    return load_settings(config).valid


def is_connected(config: PlatformConfig) -> bool:
    return load_settings(config).valid


def _env_enablement() -> Optional[dict[str, Any]]:
    settings = load_settings(None)
    if not settings.valid:
        return None
    return {
        "enabled": settings.enabled,
        "redis_url": settings.redis_url,
        "team_id": settings.team_id,
        "member_id": settings.member_id,
        "role": settings.role,
        "shared_dir": settings.shared_dir,
        "auto_run": settings.auto_run,
        "consumer_group": settings.consumer_group,
        "embedded_timeout_seconds": settings.embedded_timeout_seconds,
        "manager_url": settings.manager_url,
        "home_channel": {
            "chat_id": settings.member_id,
            "name": f"Redis Team {settings.member_id}",
        },
    }


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    settings = load_settings(pconfig)
    if not settings.valid:
        return {"error": "Redis Team standalone send: CLAWMANAGER_TEAM_* env is incomplete"}
    target = chat_id or settings.member_id
    payload = {
        "v": WIRE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "messageId": f"msg_{uuid.uuid4().hex}",
        "teamId": settings.team_id,
        "from": settings.member_id,
        "to": target,
        "taskId": thread_id or f"task_{uuid.uuid4().hex}",
        "conversationId": thread_id,
        "type": "message",
        "role": settings.role,
        "text": message,
        "artifacts": media_files or [],
        "createdAt": _now_iso(),
    }
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        redis_id = await xadd_json(redis, inbox_key(settings, target), payload)
        await xadd_json(redis, events_key(settings), event_for(settings, "outbound", {"messageId": payload["messageId"], "to": target}))
    except Exception as exc:
        return {"error": f"Redis Team standalone send failed: {exc}"}
    finally:
        redis.close()
    return {"success": True, "message_id": str(redis_id)}


def register(ctx) -> None:
    artifact_path_properties = {
        "scope": {"type": "string", "enum": ["member", "team"]},
        "path": {"type": "string", "description": "Current-Team path; traversal and symlinks are rejected"},
        "rootTaskId": {"type": "string"},
        "assignmentId": {"type": "string"},
    }
    ctx.register_tool(
        name="team_artifact_write",
        toolset="redis_team",
        schema={
            "name": "team_artifact_write",
            "description": "Atomically write a UTF-8 artifact inside the current Team workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {**artifact_path_properties, "content": {"type": "string"}},
            },
        },
        handler=_tool_team_artifact_write,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=True,
        description="Write a current-Team artifact.",
    )
    ctx.register_tool(
        name="team_artifact_read",
        toolset="redis_team",
        schema={
            "name": "team_artifact_read",
            "description": "Read a UTF-8 artifact from the current Team workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {**artifact_path_properties, "maxBytes": {"type": "integer", "minimum": 1, "maximum": MAX_ARTIFACT_BYTES}},
            },
        },
        handler=_tool_team_artifact_read,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=True,
        description="Read a current-Team artifact.",
    )
    ctx.register_tool(
        name="team_artifact_list",
        toolset="redis_team",
        schema={
            "name": "team_artifact_list",
            "description": "List current-Team artifacts without following symlinks.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {**artifact_path_properties, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
            },
        },
        handler=_tool_team_artifact_list,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=True,
        description="List current-Team artifacts.",
    )
    ctx.register_tool(
        name="team_artifact_mkdir",
        toolset="redis_team",
        schema={
            "name": "team_artifact_mkdir",
            "description": "Create a directory inside the current Team workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": artifact_path_properties,
            },
        },
        handler=_tool_team_artifact_mkdir,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=True,
        description="Create a current-Team artifact directory.",
    )
    ctx.register_tool(
        name="team_artifact_preview",
        toolset="redis_team",
        schema={
            "name": "team_artifact_preview",
            "description": "Create a managed read-only HTTP Browser URL for a current-Team file.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": artifact_path_properties,
            },
        },
        handler=_tool_team_artifact_preview,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR", "CLAWMANAGER_TEAM_TOKEN", "CLAWMANAGER_TEAM_PREVIEW_ORIGIN"],
        is_async=True,
        description="Open a Team artifact through the managed Browser preview.",
    )
    ctx.register_tool(
        name="team_send",
        toolset="redis_team",
        schema={
            "name": "team_send",
            "description": "Send a task or message to another ClawManager team member via Redis Streams.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["to", "text"],
                "properties": {
                    "to": {"type": "string", "description": "Recipient member ID, or broadcast if supported"},
                    "text": {"type": "string", "description": "Task or message text"},
                    "intent": {"type": "string"},
                    "taskId": {"type": "string"},
                    "rootTaskId": {"type": "string"},
                    "workId": {"type": "string"},
                    "assignmentId": {"type": "string"},
                    "phaseId": {"type": "string"},
                    "revision": {"type": "integer"},
                    "title": {"type": "string"},
                    "contextRefs": {"type": "array", "items": {"type": "string"}},
                    "ttlSeconds": {"type": "integer"},
                    "priority": {"type": "string"},
                    "metadata": {"type": "object"},
                },
            },
        },
        handler=_tool_team_send,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_REDIS_URL", "CLAWMANAGER_TEAM_ID", "CLAWMANAGER_TEAM_MEMBER_ID"],
        is_async=True,
        description="Send work to another Redis Team member.",
    )
    ctx.register_tool(
        name="team_status",
        toolset="redis_team",
        schema={
            "name": "team_status",
            "description": "Read Redis Team member status snapshots from the shared Team directory.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"memberId": {"type": "string"}},
            },
        },
        handler=_tool_team_status,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=True,
        description="Read Redis Team status snapshots.",
    )
    ctx.register_tool(
        name="team_update_progress",
        toolset="redis_team",
        schema={
            "name": "team_update_progress",
            "description": "Update this Hermes member's Redis Team task progress and publish a progress event.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {
                    "taskId": {"type": "string"},
                    "status": {"type": "string"},
                    "summary": {"type": "string"},
                    "progress": {"type": "number"},
                    "artifactRefs": {"type": "array", "items": {"type": "string"}},
                    "eventKind": {"type": "string"},
                },
            },
        },
        handler=_tool_team_update_progress,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_REDIS_URL", "CLAWMANAGER_TEAM_ID", "CLAWMANAGER_TEAM_MEMBER_ID"],
        is_async=True,
        description="Publish Redis Team task progress.",
    )
    ctx.register_tool(
        name="team_complete_task",
        toolset="redis_team",
        schema={
            "name": "team_complete_task",
            "description": "Mark a Redis Team task succeeded, failed or cancelled, and write durable result files.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "summary"],
                "properties": {
                    "taskId": {"type": "string"},
                    "status": {"type": "string"},
                    "summary": {"type": "string"},
                    "resultMarkdown": {"type": "string"},
                    "artifactRefs": {"type": "array", "items": {"type": "string"}},
                    "reviewVerdict": {"type": "string", "enum": ["pass", "fail"]},
                    "reviewedAssignmentId": {"type": "string"},
                    "reviewedRevision": {"type": "integer"},
                    "reviewedArtifactRefs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        handler=_tool_team_complete_task,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_REDIS_URL", "CLAWMANAGER_TEAM_ID", "CLAWMANAGER_TEAM_MEMBER_ID"],
        is_async=True,
        description="Complete a Redis Team task.",
    )
    ctx.register_platform(
        name="redis_team",
        label="Redis Team",
        adapter_factory=lambda cfg: RedisTeamAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "CLAWMANAGER_TEAM_ENABLED",
            "CLAWMANAGER_TEAM_REDIS_URL",
            "CLAWMANAGER_TEAM_ID",
            "CLAWMANAGER_TEAM_MEMBER_ID",
        ],
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="CLAWMANAGER_TEAM_MEMBER_ID",
        standalone_sender_fn=_standalone_send,
        emoji="[team]",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are handling a ClawManager Redis Team task. Treat the incoming "
            "message as delegated Worker work. Read the task context and "
            "/team/team.json when available. Prefer team_artifact_write/read/list/mkdir "
            "for shared files and use team_artifact_preview before opening a Team file "
            "in Browser; never use file:// or start a temporary server. A substantive "
            "final response is automatically submitted for control-plane validation, "
            "so do not repeat it only to satisfy a tool sequence. Use team_complete_task "
            "when reporting failure, cancellation, review metadata, or an explicit final "
            "result. Missing optional Team metadata is not a reason to stop. Never write "
            "Team tokens, API keys, or Redis credentials into Team files or logs."
        ),
    )
