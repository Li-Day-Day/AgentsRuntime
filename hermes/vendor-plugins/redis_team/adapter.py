"""Redis Team platform adapter for ClawManager-managed Hermes runtimes."""

from __future__ import annotations

import asyncio
import hashlib
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
from urllib.parse import unquote, urlparse

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
PROTOCOL_VERSION = 2
COMPLETION_SOURCE = "team_complete_task"
DEFAULT_SHARED_DIR = "/team"
DEFAULT_CONSUMER_GROUP = "team-members"
READ_BLOCK_MS = 5000
STATUS_INTERVAL_SECONDS = 30
TEAM_SHARED_DIR_MODE = 0o2775
TEAM_SHARED_FILE_MODE = 0o664


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


def _assert_response_locale(locale: Any, text: Any, label: str) -> None:
    normalized = _trim(locale).lower()
    body = str(text or "")
    if normalized.startswith("zh") and body.strip() and re.search(r"[\u3400-\u9fff]", body) is None:
        raise ValueError(f"{label} must use {_trim(locale) or 'zh-CN'}; rewrite user-visible prose in Chinese while preserving code and technical names")


def processed_message_key(settings: "RedisTeamSettings", key: str) -> str:
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
    return f"claw:team:{settings.team_id}:processed:{settings.member_id}:{digest}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "unknown"


def _stable_assignment_id(settings: "RedisTeamSettings", *, task_id: str, root_task_id: str, to: str, title: str, text: str) -> str:
    seed = "\n".join([settings.team_id, settings.member_id, root_task_id, task_id, to, title, text])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"assignment-{_safe_name(to)}-{digest}"


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
    tmp.write_text(value.rstrip() + "\n", encoding="utf-8")
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
    team_config_path: str = ""
    auto_run: bool = True
    consumer_group: str = DEFAULT_CONSUMER_GROUP
    embedded_timeout_seconds: int = 1800
    manager_url: str = ""

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
        team_config_path=_trim(pick("team_config_path", "CLAWMANAGER_TEAM_CONFIG_PATH", "", "teamConfigPath")),
        auto_run=_truthy(pick("auto_run", "CLAWMANAGER_TEAM_AUTORUN", True, "autoRun"), True),
        consumer_group=_trim(pick("consumer_group", "CLAWMANAGER_TEAM_CONSUMER_GROUP", DEFAULT_CONSUMER_GROUP, "consumerGroup"))
        or DEFAULT_CONSUMER_GROUP,
        embedded_timeout_seconds=max(1, timeout),
        manager_url=_trim(pick("manager_url", "CLAWMANAGER_TEAM_MANAGER_URL", "", "managerUrl")),
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


def event_for(settings: RedisTeamSettings, event: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = {
        "v": WIRE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
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
    for directory in [settings.shared_path, *(settings.shared_path / child for child in ("inbox", "status", "tasks", "results", "artifacts", "tmp", ".hermes-redis-team"))]:
        directory.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
        directory.chmod(TEAM_SHARED_DIR_MODE)


def _artifact_relative_path(value: Any) -> Path:
    raw = _trim(value).replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError("Team artifact path must be a non-empty relative path")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Team artifact path traversal is not allowed")
    return Path(*parts)


def _artifact_path(settings: RedisTeamSettings, args: dict[str, Any], *, default_scope: str) -> Path:
    scope = _trim(args.get("scope") or default_scope).lower()
    if scope == "team":
        root = settings.shared_path
    elif scope == "member":
        root_task_id = _safe_name(_trim(args.get("rootTaskId") or args.get("root_task_id") or "unscoped"))
        root = settings.shared_path / "artifacts" / root_task_id / "members" / _safe_name(settings.member_id)
    else:
        raise ValueError("Team artifact scope must be member or team")
    candidate = root / _artifact_relative_path(args.get("path"))
    shared_root = settings.shared_path.resolve()
    resolved_parent = candidate.parent.resolve(strict=False)
    if shared_root != resolved_parent and shared_root not in resolved_parent.parents:
        raise ValueError("Team artifact path escaped the current Team workspace")
    current = shared_root
    for component in candidate.relative_to(shared_root).parts:
        current = current / component
        if current.exists() and current.is_symlink():
            raise ValueError(f"Team artifact paths may not traverse symlinks: {current}")
    return candidate


def _assert_artifact_write_scope(settings: RedisTeamSettings, args: dict[str, Any]) -> None:
    scope = _trim(args.get("scope") or "member").lower()
    role = settings.role.lower()
    member_id = settings.member_id.lower()
    if scope == "team" and "leader" not in role and "leader" not in member_id:
        raise ValueError("Only the Team Leader may write team-scoped artifacts; members must use scope=member")


def _shared_workspace_for_target(
    settings: RedisTeamSettings,
    inherited: Any,
    target_member_id: str,
    root_task_id: str,
) -> dict[str, Any]:
    workspace = dict(inherited) if isinstance(inherited, dict) else {}
    physical_path = _trim(workspace.get("physicalPath")) or settings.shared_dir
    task_ref = _safe_name(root_task_id or "unscoped")
    member_ref = _safe_name(target_member_id or settings.member_id)
    workspace.update(
        {
            "physicalPath": physical_path,
            "canonicalPrefix": "/team",
            "memberArtifactPhysicalRoot": str(Path(physical_path) / "artifacts" / task_ref / "members" / member_ref),
            "memberArtifactCanonicalRoot": f"/team/artifacts/{task_ref}/members/{member_ref}",
        }
    )
    return workspace


def _tool_team_artifact_write(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        _assert_artifact_write_scope(settings, args)
        target = _artifact_path(settings, args, default_scope="member")
        _atomic_write_text(target, str(args.get("content") or ""))
        return json.dumps({"ok": True, "artifact": {"path": canonical_artifact_ref(settings, target), "bytes": target.stat().st_size}}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _tool_team_artifact_read(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="team")
        max_bytes = min(1024 * 1024, max(1, int(args.get("maxBytes") or 256 * 1024)))
        if not target.is_file():
            raise ValueError("Team artifact is not a file")
        if target.stat().st_size > max_bytes:
            raise ValueError("Team artifact exceeds maxBytes")
        return json.dumps({"ok": True, "artifact": {"path": canonical_artifact_ref(settings, target), "content": target.read_text(encoding="utf-8"), "bytes": target.stat().st_size}}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _tool_team_artifact_list(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        target = _artifact_path(settings, args, default_scope="team")
        limit = min(200, max(1, int(args.get("limit") or 100)))
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: item.name)[:limit]:
            if child.is_symlink():
                continue
            entries.append({"name": child.name, "type": "directory" if child.is_dir() else "file", "path": canonical_artifact_ref(settings, child), "bytes": child.stat().st_size if child.is_file() else None})
        return json.dumps({"ok": True, "path": canonical_artifact_ref(settings, target), "entries": entries}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _tool_team_artifact_mkdir(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    try:
        _assert_artifact_write_scope(settings, args)
        target = _artifact_path(settings, args, default_scope="member")
        target.mkdir(parents=True, exist_ok=True, mode=TEAM_SHARED_DIR_MODE)
        target.chmod(TEAM_SHARED_DIR_MODE)
        return json.dumps({"ok": True, "artifact": {"path": canonical_artifact_ref(settings, target)}}, ensure_ascii=False)
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


def _team_config_candidates(settings: RedisTeamSettings) -> list[Path]:
    candidates = [
        _trim(settings.team_config_path),
        _trim(os.getenv("CLAWMANAGER_TEAM_CONFIG_PATH")),
        "/etc/clawmanager/team/team.json",
        str(settings.shared_path / "team.json"),
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(Path(candidate))
    return out


def _normalize_roster_member(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    member_id = _trim(raw.get("memberId") or raw.get("memberID") or raw.get("memberKey") or raw.get("id") or raw.get("key"))
    if not member_id:
        return None
    role = _trim(raw.get("role") or raw.get("effectiveRole") or raw.get("profileName") or "member") or "member"
    aliases = [member_id, _safe_name(member_id)]
    for value in (raw.get("displayName"), raw.get("name")):
        text = _trim(value)
        if text:
            aliases.extend([text, _safe_name(text)])
    return {
        "teamId": _trim(raw.get("teamId")),
        "memberId": member_id,
        "role": role,
        "effectiveRole": _trim(raw.get("effectiveRole")),
        "profileKey": _trim(raw.get("profileKey")),
        "profileName": _trim(raw.get("profileName")),
        "displayName": _trim(raw.get("displayName") or raw.get("name")),
        "runtime": _trim(raw.get("runtime") or raw.get("runtimeType")),
        "runtimeType": _trim(raw.get("runtimeType") or raw.get("runtime")),
        "instanceMode": _trim(raw.get("instanceMode")),
        "isLeader": bool(raw.get("isLeader")),
        "description": _trim(raw.get("description")),
        "aliases": sorted({alias for alias in aliases if alias}),
    }


def read_team_roster(settings: RedisTeamSettings) -> dict[str, Any]:
    for path in _team_config_candidates(settings):
        raw = _read_json(path)
        members_raw = []
        if isinstance(raw, dict):
            if isinstance(raw.get("members"), list):
                members_raw = raw["members"]
            elif isinstance(raw.get("team"), dict) and isinstance(raw["team"].get("members"), list):
                members_raw = raw["team"]["members"]
            elif isinstance(raw.get("roster"), dict) and isinstance(raw["roster"].get("members"), list):
                members_raw = raw["roster"]["members"]
        members = [member for member in (_normalize_roster_member(item) for item in members_raw) if member]
        if members:
            return {"source": str(path), "raw": raw, "members": members}
    env_json = _trim(os.getenv("CLAWMANAGER_TEAM_CONFIG_JSON"))
    if env_json:
        try:
            raw = json.loads(env_json)
        except Exception:
            raw = None
        members_raw = raw.get("members", []) if isinstance(raw, dict) and isinstance(raw.get("members"), list) else []
        members = [member for member in (_normalize_roster_member(item) for item in members_raw) if member]
        if members:
            return {"source": "CLAWMANAGER_TEAM_CONFIG_JSON", "raw": raw, "members": members}
    return {"source": "", "raw": None, "members": []}


def _status_matches_target(status: dict[str, Any], target: str) -> bool:
    raw = _trim(target)
    safe = _safe_name(raw)
    for value in (
        status.get("memberId"),
        status.get("memberID"),
        status.get("memberKey"),
        status.get("displayName"),
        status.get("name"),
    ):
        text = _trim(value)
        if text and (text == raw or _safe_name(text) == raw or _safe_name(text) == safe):
            return True
    return False


def _roster_member_matches(member: dict[str, Any], target: str) -> bool:
    raw = _trim(target)
    safe = _safe_name(raw)
    aliases = member.get("aliases") if isinstance(member.get("aliases"), list) else []
    return raw in aliases or safe in aliases


def _roster_status_stub(settings: RedisTeamSettings, member: dict[str, Any]) -> dict[str, Any]:
    return {
        "teamId": settings.team_id or member.get("teamId"),
        "memberId": member.get("memberId"),
        "role": member.get("effectiveRole") or member.get("role"),
        "rosterRole": member.get("role"),
        "effectiveRole": member.get("effectiveRole") or None,
        "profileKey": member.get("profileKey") or None,
        "profileName": member.get("profileName") or None,
        "displayName": member.get("displayName") or None,
        "runtime": member.get("runtime") or member.get("runtimeType") or None,
        "runtimeType": member.get("runtimeType") or member.get("runtime") or None,
        "instanceMode": member.get("instanceMode") or None,
        "isLeader": bool(member.get("isLeader")),
        "description": member.get("description") or None,
        "liveness": "unknown",
        "runtimeStatus": "unknown",
        "availability": "unknown",
        "lastSeenAt": "",
    }


def _read_raw_team_statuses(settings: RedisTeamSettings, member_id: str = "") -> Any:
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


def read_team_statuses(settings: RedisTeamSettings, member_id: str = "") -> Any:
    raw_statuses = _read_raw_team_statuses(settings)
    roster = read_team_roster(settings)
    members = roster.get("members") if isinstance(roster.get("members"), list) else []
    if member_id:
        for status in raw_statuses if isinstance(raw_statuses, list) else []:
            if isinstance(status, dict) and _status_matches_target(status, member_id):
                return status
        for member in members:
            if _roster_member_matches(member, member_id):
                return _roster_status_stub(settings, member)
        return None
    if not members:
        return raw_statuses
    merged: list[dict[str, Any]] = []
    for member in members:
        status = next(
            (
                item
                for item in raw_statuses
                if isinstance(item, dict) and _status_matches_target(item, str(member.get("memberId") or ""))
            ),
            None,
        )
        stub = _roster_status_stub(settings, member)
        if status:
            stub.update(status)
        merged.append(stub)
    known = {_safe_name(str(item.get("memberId") or "")) for item in merged}
    for status in raw_statuses:
        if isinstance(status, dict) and _safe_name(str(status.get("memberId") or "")) not in known:
            merged.append(status)
    merged.sort(key=lambda item: str(item.get("memberId", "")))
    return merged


def _resolve_team_target_error(settings: RedisTeamSettings, to: str) -> str:
    target = _trim(to)
    if not target:
        return "to is required"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,160}", target):
        return f"unknown Redis Team target: {to}"
    if target.lower() in {"broadcast", "team", "all"}:
        return ""
    if target == settings.member_id or _safe_name(target) == _safe_name(settings.member_id):
        return ""
    roster = read_team_roster(settings)
    members = roster.get("members") if isinstance(roster.get("members"), list) else []
    if members:
        return "" if any(_roster_member_matches(member, target) for member in members) else f"unknown Redis Team target: {to}"
    statuses = _read_raw_team_statuses(settings)
    if not statuses:
        return ""
    return "" if any(isinstance(status, dict) and _status_matches_target(status, target) for status in statuses) else f"unknown Redis Team target: {to}"


def write_task_result(
    settings: RedisTeamSettings,
    task_id: str,
    *,
    status: str,
    summary: str,
    result_markdown: str = "",
    artifact_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    ensure_team_dirs(settings)
    task_id = task_id or f"task_{uuid.uuid4().hex}"
    result_dir = settings.shared_path / "results" / _safe_name(task_id)
    result_dir.mkdir(parents=True, exist_ok=True)

    refs = validate_artifact_refs(settings, artifact_refs)
    result_markdown = result_markdown.strip() or summary
    result_md = result_dir / "result.md"
    _atomic_write_text(result_md, result_markdown)
    refs.append(canonical_artifact_ref(settings, result_md))

    payload = {
        "taskId": task_id,
        "status": status,
        "summary": summary,
        "resultMarkdown": result_markdown,
        "artifactRefs": refs,
        "completedAt": _now_iso(),
    }
    _atomic_write_json(result_dir / "result.json", payload)
    write_local_status(
        settings,
        {
            "availability": "idle" if status == "succeeded" else "blocked",
            "runtimeStatus": "succeeded" if status == "succeeded" else "failed",
            "currentTaskId": task_id,
            "progress": 100 if status == "succeeded" else None,
            "lastSummary": summary,
            "artifactRefs": refs,
        },
    )
    return payload


def task_result_is_terminal(settings: RedisTeamSettings, task_id: str) -> bool:
    result = _read_json(settings.shared_path / "results" / _safe_name(task_id) / "result.json")
    if not isinstance(result, dict):
        return False
    return _trim(result.get("status")).lower() in {"succeeded", "failed", "cancelled"}


def canonical_artifact_ref(settings: RedisTeamSettings, path: Path) -> str:
    shared = settings.shared_path.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(shared)
    except ValueError as exc:
        raise ValueError(f"artifact path escaped Redis Team shared directory: {path}") from exc
    return "/team/" + relative.as_posix()


def validate_artifact_refs(settings: RedisTeamSettings, refs: Optional[list[str]]) -> list[str]:
    shared = settings.shared_path.resolve()
    validated: list[str] = []
    for raw in refs or []:
        ref = _trim(raw)
        if not ref:
            continue
        candidate = (shared / ref[len("/team/"):]).resolve() if ref.startswith("/team/") else Path(ref).resolve()
        try:
            candidate.relative_to(shared)
        except ValueError as exc:
            raise ValueError(f"artifact reference escaped Redis Team shared directory: {ref}") from exc
        if not candidate.is_file():
            raise ValueError(f"artifact reference is not a readable file: {ref}")
        canonical = canonical_artifact_ref(settings, candidate)
        if canonical not in validated:
            validated.append(canonical)
    return validated


def normalize_envelope(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "schemaVersion": raw.get("v") or raw.get("schemaVersion") or WIRE_SCHEMA_VERSION,
        "protocolVersion": raw.get("protocolVersion") or raw.get("protocol_version") or raw.get("v") or WIRE_SCHEMA_VERSION,
        "messageId": raw.get("messageId") or raw.get("id") or f"msg_{uuid.uuid4().hex}",
        "taskId": raw.get("taskId") or raw.get("task_id") or f"task_{uuid.uuid4().hex}",
        "rootTaskId": raw.get("rootTaskId") or raw.get("root_task_id") or raw.get("taskId") or raw.get("task_id"),
        "rootMessageId": raw.get("rootMessageId") or raw.get("root_message_id") or raw.get("messageId") or raw.get("message_id"),
        "workId": raw.get("workId") or raw.get("work_id") or raw.get("assignmentId") or raw.get("assignment_id"),
        "assignmentId": raw.get("assignmentId") or raw.get("assignment_id") or raw.get("workId") or raw.get("work_id"),
        "dependsOn": raw.get("dependsOn") if isinstance(raw.get("dependsOn"), list) else [],
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
        "responseLocale": raw.get("responseLocale") or raw.get("response_locale") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("responseLocale") or "zh-CN",
        "sharedWorkspace": raw.get("sharedWorkspace") or raw.get("shared_workspace") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("sharedWorkspace") or {},
        "workspaceContract": raw.get("workspaceContract") or raw.get("workspace_contract") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("workspaceContract") or {},
        "checkId": raw.get("checkId") or raw.get("check_id") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("checkId"),
        "checkSequence": raw.get("checkSequence") or raw.get("check_sequence") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("checkSequence"),
        "requestedAt": raw.get("requestedAt") or raw.get("requested_at") or (raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}).get("requestedAt"),
        "requiresCompletion": raw.get("requiresCompletion", True),
        "completionTool": raw.get("completionTool") or "team_complete_task",
        "resultSink": raw.get("resultSink") if isinstance(raw.get("resultSink"), dict) else {},
        "sessionKey": raw.get("sessionKey") or raw.get("approvalSessionKey") or "",
        "approval": raw.get("approval") if isinstance(raw.get("approval"), dict) else {},
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


def _completion_id(settings: RedisTeamSettings, task_id: str) -> str:
    return f"completion:{_safe_name(settings.team_id)}:{_safe_name(task_id)}:{_safe_name(settings.member_id)}"


def completion_key(settings: RedisTeamSettings, completion_id: str) -> str:
    return f"{_key_prefix(settings)}:completions:{_safe_name(completion_id)}"


def task_envelope_path(settings: RedisTeamSettings, task_id: str) -> Path:
    return settings.shared_path / "tasks" / f"{_safe_name(task_id)}.json"


def write_task_envelope(settings: RedisTeamSettings, envelope: dict[str, Any]) -> None:
    task_id = _trim(envelope.get("taskId"))
    if not task_id:
        raise ValueError("Redis Team envelope is missing taskId")
    _atomic_write_json(task_envelope_path(settings, task_id), envelope)


def read_task_envelope(settings: RedisTeamSettings, task_id: str) -> Optional[dict[str, Any]]:
    value = _read_json(task_envelope_path(settings, task_id))
    return value if isinstance(value, dict) else None


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


async def xadd_terminal_once(
    redis: AsyncRedisClient,
    settings: RedisTeamSettings,
    completion_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    script = "\n".join(
        (
            "local existing = redis.call('GET', KEYS[1])",
            "if existing then return {0, existing} end",
            "local streamId = redis.call('XADD', KEYS[2], '*', 'payload', ARGV[1])",
            "redis.call('SET', KEYS[1], streamId)",
            "return {1, streamId}",
        )
    )
    result = await redis.command(
        "EVAL",
        script,
        2,
        completion_key(settings, completion_id),
        events_key(settings),
        json.dumps(event, ensure_ascii=False),
    )
    return {
        "published": isinstance(result, list) and int(result[0]) == 1,
        "streamId": str(result[1]) if isinstance(result, list) and len(result) > 1 else "",
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


async def _publish_terminal_event(
    settings: RedisTeamSettings,
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not settings.valid:
        return {"published": False, "streamId": ""}
    completion_id = _trim(payload.get("completionId"))
    if not completion_id:
        raise ValueError("terminal Redis Team event requires completionId")
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        return await xadd_terminal_once(redis, settings, completion_id, event_for(settings, event, payload))
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
    task_id = _trim(args.get("taskId")) or f"task_{uuid.uuid4().hex}"
    parent_envelope = read_task_envelope(settings, task_id) or {}
    root_task_id = parent_envelope.get("rootTaskId") or parent_envelope.get("taskId") or task_id
    response_locale = _trim(args.get("responseLocale")) or parent_envelope.get("responseLocale") or "zh-CN"
    try:
        _assert_response_locale(response_locale, text, "Team assignment")
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    title = _trim(args.get("title")) or "Team Message"
    explicit_work_id = _trim(args.get("workId")) or _trim(args.get("assignmentId"))
    work_id = explicit_work_id or _stable_assignment_id(
        settings,
        task_id=task_id,
        root_task_id=root_task_id,
        to=to,
        title=title,
        text=text,
    )
    message = {
        "v": WIRE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "messageId": f"msg_{uuid.uuid4().hex}",
        "teamId": settings.team_id,
        "from": settings.member_id,
        "to": to,
        "intent": _trim(args.get("intent")) or "send",
        "taskId": task_id,
        "rootTaskId": root_task_id,
        "rootMessageId": parent_envelope.get("rootMessageId") or parent_envelope.get("messageId"),
        "workId": work_id,
        "assignmentId": _trim(args.get("assignmentId")) or _trim(args.get("workId")) or work_id,
        "dependsOn": args.get("dependsOn") if isinstance(args.get("dependsOn"), list) else [],
        "title": title,
        "text": text,
        "contextRefs": args.get("contextRefs") if isinstance(args.get("contextRefs"), list) else [],
        "ttlSeconds": args.get("ttlSeconds") if isinstance(args.get("ttlSeconds"), int) else 3600,
        "priority": _trim(args.get("priority")) or "normal",
        "metadata": args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
        "responseLocale": response_locale,
        "sharedWorkspace": _shared_workspace_for_target(settings, parent_envelope.get("sharedWorkspace"), to, root_task_id),
        "workspaceContract": parent_envelope.get("workspaceContract") or {},
        "createdAt": _now_iso(),
    }
    redis = AsyncRedisClient(settings.redis_url)
    try:
        await redis.connect()
        target_error = _resolve_team_target_error(settings, to)
        if target_error:
            await xadd_json(
                redis,
                events_key(settings),
                event_for(
                    settings,
                    "message_failed",
                    {
                        "messageId": message["messageId"],
                        "taskId": message["taskId"],
                        "rootTaskId": message["rootTaskId"],
                        "rootMessageId": message.get("rootMessageId"),
                        "workId": message.get("workId"),
                        "assignmentId": message.get("assignmentId"),
                        "to": to,
                        "status": "message_failed",
                        "summary": target_error,
                        "error": target_error,
                    },
                ),
            )
            return json.dumps({"ok": False, "sent": message, "failed": True, "error": target_error}, ensure_ascii=False)
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
    task_id = _trim(args.get("taskId"))
    status_text = _trim(args.get("status"))
    summary = _trim(args.get("summary"))
    if not task_id or not status_text:
        return json.dumps({"error": "taskId and status are required"}, ensure_ascii=False)
    if status_text not in {"idle", "busy", "running", "blocked", "waiting_review", "waiting_completion"}:
        return json.dumps({"error": "terminal status must use team_complete_task"}, ensure_ascii=False)
    progress = args.get("progress")
    if isinstance(progress, (int, float)):
        progress = min(99, max(0, progress))
    else:
        progress = None
    envelope = read_task_envelope(settings, task_id) or {}
    event_kind = _trim(args.get("eventKind") or args.get("event_kind")).lower()
    passive_monitor_progress = event_kind in {"assignment_check_result", "assignment_check_requested", "assignment_heartbeat"}
    try:
        if not passive_monitor_progress or event_kind == "assignment_check_result":
            _assert_response_locale(envelope.get("responseLocale") or "zh-CN", summary, "Team progress")
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    progress_payload = {
        **dict(args),
        "taskId": task_id,
        "status": status_text,
        "progress": progress,
        "rootTaskId": args.get("rootTaskId") or args.get("root_task_id") or envelope.get("rootTaskId") or envelope.get("taskId"),
        "rootMessageId": args.get("rootMessageId") or args.get("root_message_id") or envelope.get("rootMessageId") or envelope.get("messageId"),
        "workId": args.get("workId") or args.get("work_id") or envelope.get("workId") or envelope.get("assignmentId"),
        "assignmentId": args.get("assignmentId") or args.get("assignment_id") or envelope.get("assignmentId") or envelope.get("workId"),
        "rootTaskTerminal": False,
        "nonAuthoritative": True,
        "visibleToChat": args.get("visibleToChat") if "visibleToChat" in args else (args.get("visible_to_chat") if "visible_to_chat" in args else not passive_monitor_progress),
        "responseLocale": envelope.get("responseLocale") or "zh-CN",
        "checkId": (args.get("checkId") or args.get("check_id") or envelope.get("checkId") or envelope.get("messageId")) if passive_monitor_progress else None,
        "checkSequence": (args.get("checkSequence") or args.get("check_sequence") or envelope.get("checkSequence")) if passive_monitor_progress else None,
        "requestedAt": (args.get("requestedAt") or args.get("requested_at") or envelope.get("requestedAt")) if passive_monitor_progress else None,
        "respondedAt": _now_iso() if event_kind == "assignment_check_result" else None,
    }
    status = write_local_status(
        settings,
        {
            "availability": "idle" if status_text == "idle" else status_text,
            "currentTaskId": task_id,
            "progress": progress,
            "lastSummary": summary or status_text,
            "artifactRefs": args.get("artifactRefs") if isinstance(args.get("artifactRefs"), list) else [],
        },
    )
    await _publish_event(settings, "task_progress", progress_payload)
    return json.dumps({"ok": True, "status": status}, ensure_ascii=False)


async def _tool_team_complete_task(args: dict[str, Any], **_kwargs) -> str:
    settings = load_settings(None)
    if not settings.enabled:
        return json.dumps({"error": "Redis Team is disabled"}, ensure_ascii=False)
    task_id = _trim(args.get("taskId"))
    status_text = _trim(args.get("status")).lower()
    summary = _trim(args.get("summary"))
    if not task_id or not status_text or not summary:
        return json.dumps({"error": "taskId, status and summary are required"}, ensure_ascii=False)
    if status_text not in {"succeeded", "failed", "cancelled"}:
        return json.dumps({"error": "status must be succeeded, failed or cancelled"}, ensure_ascii=False)
    envelope = read_task_envelope(settings, task_id)
    if not envelope:
        return json.dumps({"error": f"team_complete_task could not resolve the task envelope: {task_id}"}, ensure_ascii=False)
    try:
        _assert_response_locale(envelope.get("responseLocale") or "zh-CN", summary + "\n" + _trim(args.get("resultMarkdown")), "Team completion")
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    result = write_task_result(
        settings,
        task_id,
        status=status_text,
        summary=summary,
        result_markdown=_trim(args.get("resultMarkdown")),
        artifact_refs=args.get("artifactRefs") if isinstance(args.get("artifactRefs"), list) else [],
    )
    completion_id = _trim(args.get("completionId")) or _completion_id(settings, task_id)
    completion_payload = {
        **dict(args),
        "messageId": envelope.get("messageId"),
        "taskId": task_id,
        "rootTaskId": envelope.get("rootTaskId") or task_id,
        "rootMessageId": envelope.get("rootMessageId") or envelope.get("messageId"),
        "workId": envelope.get("workId") or envelope.get("assignmentId"),
        "assignmentId": envelope.get("assignmentId") or envelope.get("workId"),
        "status": status_text,
        "summary": summary,
        "resultMarkdown": result["resultMarkdown"],
        "artifactRefs": result["artifactRefs"],
        "completionId": completion_id,
        "completionSource": COMPLETION_SOURCE,
        "explicitCompletion": True,
        "responseLocale": envelope.get("responseLocale") or "zh-CN",
    }
    terminal = await _publish_terminal_event(settings, _completion_event_for_status(status_text), completion_payload)
    return json.dumps({"ok": True, **result, **terminal, "completionId": completion_id}, ensure_ascii=False)


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
            except Exception as exc:
                logger.error("Redis Team: failed to connect: %s", exc)
                self._set_fatal_error("connect_failed", str(exc), retryable=True)
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
            logger.info(
                "Redis Team: connected team=%s member=%s group=%s reconnect=%s",
                self.settings.team_id,
                self.settings.member_id,
                self.settings.consumer_group,
                is_reconnect,
            )
            return True

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            await self._disconnect_unlocked(mark_offline=True)

    async def _disconnect_unlocked(self, *, mark_offline: bool) -> None:
        self._mark_disconnected()
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
        message_id = f"reply_{uuid.uuid4().hex}"
        if task_result_is_terminal(self.settings, str(task_id)):
            logger.info("Redis Team: suppressed duplicate reply after explicit completion for %s", task_id)
            return SendResult(success=True, message_id=message_id)
        envelope = read_task_envelope(self.settings, str(task_id)) or {}
        try:
            _assert_response_locale(envelope.get("responseLocale") or "zh-CN", content, "Team reply")
        except ValueError as exc:
            return SendResult(success=False, error=str(exc))
        target = _reply_target(self.settings, metadata)
        event = event_for(
            self.settings,
            "reply",
            {
                "messageId": message_id,
                "taskId": task_id,
                "conversationId": metadata.get("conversation_id") or chat_id,
                "to": target,
                "text": content,
                "replyTo": reply_to,
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
                await xadd_json(self._redis, events_key(self.settings), event)
                if target:
                    await xadd_json(
                        self._redis,
                        inbox_key(self.settings, target),
                        {
                            "v": WIRE_SCHEMA_VERSION,
                            "protocolVersion": PROTOCOL_VERSION,
                            "messageId": message_id,
                            "teamId": self.settings.team_id,
                            "from": self.settings.member_id,
                            "to": target,
                            "taskId": task_id,
                            "conversationId": metadata.get("conversation_id") or chat_id,
                            "type": "reply",
                            "role": self.settings.role,
                            "text": content,
                            "replyTo": reply_to,
                            "createdAt": _now_iso(),
                        },
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
        envelope = event.raw_message if isinstance(event.raw_message, dict) else {}
        task_id = str(envelope.get("taskId") or event.source.chat_id)
        message_id = str(envelope.get("messageId") or event.message_id or "")
        if outcome == ProcessingOutcome.SUCCESS:
            if task_result_is_terminal(self.settings, task_id):
                self._redis_reply_metadata.pop(task_id, None)
                return
            summary = "Agent turn finished; waiting for explicit team_complete_task"
            write_local_status(
                self.settings,
                {
                    "availability": "waiting_completion",
                    "runtimeStatus": "waiting_completion",
                    "currentTaskId": task_id,
                    "lastSummary": summary,
                },
            )
            if self._redis:
                await xadd_json(
                    self._redis,
                    events_key(self.settings),
                    event_for(
                        self.settings,
                        "task_progress",
                        {
                            "messageId": message_id,
                            "taskId": task_id,
                            "status": "waiting_completion",
                            "availability": "waiting_completion",
                            "runtimeStatus": "waiting_completion",
                            "summary": summary,
                            "completionRequired": True,
                        },
                    ),
                )
        else:
            status = "cancelled" if outcome == ProcessingOutcome.CANCELLED else "failed"
            summary = f"Redis Team task {status}"
            result = write_task_result(self.settings, task_id, status=status, summary=summary)
            if self._redis:
                completion_id = _completion_id(self.settings, task_id)
                await xadd_terminal_once(
                    self._redis,
                    self.settings,
                    completion_id,
                    event_for(
                        self.settings,
                        _completion_event_for_status(status),
                        {
                            "taskId": task_id,
                            "rootTaskId": envelope.get("rootTaskId") or task_id,
                            "rootMessageId": envelope.get("rootMessageId") or message_id,
                            "workId": envelope.get("workId") or envelope.get("assignmentId"),
                            "status": status,
                            "summary": summary,
                            "artifactRefs": result["artifactRefs"],
                            "completionId": completion_id,
                            "completionSource": "runtime_processing",
                            "explicitCompletion": False,
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Redis Team: presence update failed: %s", exc)
            await asyncio.sleep(STATUS_INTERVAL_SECONDS)

    async def _consumer_loop(self) -> None:
        assert self._consumer_redis is not None
        redis = self._consumer_redis
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
                    ">",
                )
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
        processed_key = processed_message_key(self.settings, dedup_key)
        if await self._redis.command("GET", processed_key):
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
                await self._redis.command("SET", processed_key, envelope["messageId"], "EX", 604800)
                if redis_id:
                    await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)
                return
            if self.settings.auto_run:
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
            await self._redis.command("SET", processed_key, envelope["messageId"], "EX", 604800)
            if redis_id:
                await self._redis.command("XACK", inbox_key(self.settings), self.settings.consumer_group, redis_id)
        except Exception as exc:
            error = str(exc)
            logger.warning("Redis Team: message processing failed: %s", error)
            await xadd_json(
                self._redis,
                dlq_key(self.settings),
                event_for(self.settings, "dlq", {"redisId": redis_id, "error": error, "message": raw}),
            )
            result = write_task_result(
                self.settings,
                envelope["taskId"],
                status="failed",
                summary=error,
            )
            completion_id = _completion_id(self.settings, envelope["taskId"])
            await xadd_terminal_once(
                self._redis,
                self.settings,
                completion_id,
                event_for(
                    self.settings,
                    "task_failed",
                    {
                        "messageId": envelope["messageId"],
                        "taskId": envelope["taskId"],
                        "rootTaskId": envelope.get("rootTaskId") or envelope["taskId"],
                        "rootMessageId": envelope.get("rootMessageId") or envelope["messageId"],
                        "workId": envelope.get("workId") or envelope.get("assignmentId"),
                        "status": "failed",
                        "summary": error,
                        "artifactRefs": result["artifactRefs"],
                        "completionId": completion_id,
                        "completionSource": "runtime_error",
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
        write_task_envelope(self.settings, envelope)
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
        response_locale = _trim(envelope.get("responseLocale")) or "zh-CN"
        physical_shared_dir = _trim((envelope.get("sharedWorkspace") or {}).get("physicalPath")) or self.settings.shared_dir
        root_task_id = _trim(envelope.get("rootTaskId") or envelope.get("taskId"))
        member_artifact_root = _trim((envelope.get("sharedWorkspace") or {}).get("memberArtifactCanonicalRoot")) or f"/team/artifacts/{_safe_name(root_task_id)}/members/{_safe_name(self.settings.member_id)}"
        text += (
            "\n\nRedis Team delivery contract:\n"
            "- Complete this assignment with team_complete_task; a normal reply may not reach ClawManager.\n"
            "- Prefer team_artifact_write/read/list/mkdir for shared artifacts. /team is a canonical report prefix, not a pooled-runtime global directory.\n"
            "- Never list, search, resolve, or scan the parent of the current Team shared directory, and never inspect sibling Team directories.\n"
            f"- Current member artifact root: {member_artifact_root}\n"
            f"- Use {response_locale} for all user-visible plans, progress, results, and synthesis. Preserve code and technical names.\n"
            f"- Current Team physical shared directory: {physical_shared_dir}"
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
        "team_config_path": settings.team_config_path,
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
                    "workId": {"type": "string", "description": "Stable business work item ID within the root task"},
                    "assignmentId": {"type": "string"},
                    "dependsOn": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"},
                    "contextRefs": {"type": "array", "items": {"type": "string"}},
                    "ttlSeconds": {"type": "integer"},
                    "priority": {"type": "string"},
                    "metadata": {"type": "object"},
                    "responseLocale": {"type": "string"},
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
    artifact_path_properties = {
        "path": {"type": "string", "description": "Relative Team artifact path; absolute paths and traversal are rejected"},
        "scope": {"type": "string", "enum": ["member", "team"]},
        "rootTaskId": {"type": "string"},
    }
    ctx.register_tool(
        name="team_artifact_write",
        toolset="redis_team",
        schema={
            "name": "team_artifact_write",
            "description": "Atomically write a UTF-8 artifact inside the current Team workspace with cooperative permissions.",
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
        is_async=False,
        description="Write a current-Team artifact.",
    )
    ctx.register_tool(
        name="team_artifact_read",
        toolset="redis_team",
        schema={
            "name": "team_artifact_read",
            "description": "Read a UTF-8 artifact from the current Team workspace without allowing traversal.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {**artifact_path_properties, "maxBytes": {"type": "integer", "minimum": 1, "maximum": 1048576}},
            },
        },
        handler=_tool_team_artifact_read,
        check_fn=check_requirements,
        requires_env=["CLAWMANAGER_TEAM_SHARED_DIR"],
        is_async=False,
        description="Read a current-Team artifact.",
    )
    ctx.register_tool(
        name="team_artifact_list",
        toolset="redis_team",
        schema={
            "name": "team_artifact_list",
            "description": "List artifacts in the current Team workspace without following symlinks.",
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
        is_async=False,
        description="List current-Team artifacts.",
    )
    ctx.register_tool(
        name="team_artifact_mkdir",
        toolset="redis_team",
        schema={
            "name": "team_artifact_mkdir",
            "description": "Create a cooperative artifact directory inside the current Team workspace.",
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
        is_async=False,
        description="Create a current-Team artifact directory.",
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
                "required": ["taskId", "status"],
                "properties": {
                    "taskId": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["idle", "busy", "running", "blocked", "waiting_review", "waiting_completion"],
                    },
                    "summary": {"type": "string"},
                    "progress": {"type": "number", "minimum": 0, "maximum": 99},
                    "artifactRefs": {"type": "array", "items": {"type": "string"}},
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
                "required": ["taskId", "status", "summary"],
                "properties": {
                    "taskId": {"type": "string"},
                    "status": {"type": "string", "enum": ["succeeded", "failed", "cancelled"]},
                    "summary": {"type": "string"},
                    "resultMarkdown": {"type": "string"},
                    "artifactRefs": {"type": "array", "items": {"type": "string"}},
                    "completionId": {"type": "string"},
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
            "message as delegated work from another team member. Before writing "
            "results, read the task brief when available and read "
            "/team/team.json or /team/team.yaml only if either exists. Missing "
            "team metadata files or /team/members does not mean the team is "
            "unconfigured; continue from the env and message context. Write "
            "final artifacts under /team/results/<taskId>/, call "
            "team_complete_task before sending the final notification, then use "
            "team_send with a concise result summary for the leader. Never write "
            "team tokens, API keys, or Redis credentials into /team files or logs."
        ),
    )
