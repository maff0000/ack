from pathlib import Path
from typing import Any
import re
from datetime import datetime, timezone

import yaml

from .errors import AckError
from .paths import resolve_inside, validate_root
from .time import parse_utc


WORKER_STATUSES = {"completed", "blocked", "failed"}
LIVE_STATUSES = {"queued", "starting", "working", "blocked", "failed", "completed"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def planning_advisories(task: dict[str, Any]) -> list[str]:
    """Return non-blocking decomposition prompts for Axiom's planning review."""
    advisories: list[str] = []
    if task.get("type") == "write" and len(task.get("scope") or []) > 2:
        advisories.append("multiple primary paths: consider sequential independently verifiable deliveries")
    if len(task.get("acceptance") or []) > 6:
        advisories.append("large acceptance set: consider decomposing into bounded deliveries")
    return advisories


def load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AckError(f"cannot load YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AckError(f"expected YAML mapping: {path}")
    return data


def validate_task(task: dict[str, Any], pid_root: str | Path) -> dict[str, Any]:
    required = {"id", "project", "type", "role", "model", "project_root", "base_commit", "worktree", "skills", "objective", "scope", "must_not", "acceptance", "dependencies", "risk", "authority", "status"}
    missing = sorted(required - task.keys())
    if missing:
        raise AckError(f"task missing fields: {', '.join(missing)}")
    if task["type"] not in {"read", "write"}:
        raise AckError("task type must be read or write")
    if task["role"] not in {"scout", "builder", "tester", "reviewer", "debugger"}:
        raise AckError("unknown task role")
    if task["status"] not in LIVE_STATUSES:
        raise AckError("invalid worker task status")
    for field in ("id", "project", "model", "objective"):
        if not isinstance(task[field], str) or not task[field].strip():
            raise AckError(f"task {field} must be a non-empty string")
    for field in ("id", "project"):
        if not _SAFE_ID.fullmatch(task[field]):
            raise AckError(f"task {field} has unsafe namespace characters")
    if not _SAFE_ID.fullmatch(task["model"]):
        raise AckError("task model must be a safe logical alias")
    for field in ("skills", "scope", "must_not", "acceptance", "dependencies"):
        if not isinstance(task.get(field, []), list) or not all(isinstance(v, str) for v in task.get(field, [])):
            raise AckError(f"task {field} must be a list of strings")
    for field in ("base_commit", "worktree", "risk"):
        if not isinstance(task[field], str):
            raise AckError(f"task {field} must be a string")
    if task["risk"] not in {"low", "normal", "material", "security", "architecture"}:
        raise AckError("task risk is invalid")
    root = validate_root(task["project_root"], pid_root)
    for field in ("no_progress_seconds", "max_worker_seconds"):
        if field in task and (not isinstance(task[field], int) or task[field] <= 0):
            raise AckError(f"task {field} must be a positive integer")
    if "max_worker_tokens" in task and (not isinstance(task["max_worker_tokens"], int) or task["max_worker_tokens"] < 0):
        raise AckError("task max_worker_tokens must not be negative")
    if "max_worker_cost_usd" in task and (not isinstance(task["max_worker_cost_usd"], (int, float)) or task["max_worker_cost_usd"] < 0):
        raise AckError("task max_worker_cost_usd must not be negative")
    authority = task["authority"]
    if not isinstance(authority, dict):
        raise AckError("task authority must be a mapping")
    if set(authority) != {"mutation_allowed", "runtime_mutation_allowed"} or not all(isinstance(authority[k], bool) for k in authority):
        raise AckError("task authority requires boolean mutation_allowed and runtime_mutation_allowed")
    mutation = authority.get("mutation_allowed") is True
    if task["type"] == "write" and not mutation:
        raise AckError("write task requires mutation_allowed: true")
    if task["type"] == "read" and mutation:
        raise AckError("read task cannot grant mutation authority")
    if task["type"] == "write" and not task.get("worktree"):
        raise AckError("write task requires an isolated project-local worktree")
    for field in ("worktree",):
        if task.get(field):
            resolve_inside(root, task[field])
    for field in ("scope",):
        for item in task.get(field) or []:
            if isinstance(item, str) and (item.startswith("/") or ".." in Path(item).parts):
                resolve_inside(root, item)
    return task


def validate_result(result: dict[str, Any], task_id: str, root: str | Path, expected_agent: str | None = None) -> dict[str, Any]:
    required = {"id", "agent_instance", "status", "summary", "changed", "commit", "tests", "findings", "risks", "blockers", "evidence", "started_at_utc", "completed_at_utc"}
    missing = sorted(required - result.keys())
    if missing:
        raise AckError(f"result missing fields: {', '.join(missing)}")
    if result["id"] != task_id:
        raise AckError("result id does not match task")
    if expected_agent is not None and result["agent_instance"] != expected_agent:
        raise AckError("result agent_instance does not match launched worker")
    if result["status"] not in WORKER_STATUSES:
        raise AckError("worker result status must be completed, blocked, or failed")
    for field in ("started_at_utc", "completed_at_utc"):
        value = result[field]
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise AckError(f"result {field} must be timezone-aware UTC")
            result[field] = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    for field in ("agent_instance", "summary", "started_at_utc", "completed_at_utc"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise AckError(f"result {field} must be a non-empty string")
    if result["commit"] is None:
        result["commit"] = ""
    if not isinstance(result["commit"], str):
        raise AckError("result commit must be a string")
    try:
        parse_utc(result["started_at_utc"]); parse_utc(result["completed_at_utc"])
    except ValueError as exc:
        raise AckError(f"invalid result UTC timestamp: {exc}") from exc
    for field in ("changed", "findings", "risks", "blockers", "evidence"):
        if not isinstance(result.get(field, []), list) or not all(isinstance(v, str) for v in result.get(field, [])):
            raise AckError(f"result {field} must be a list of strings")
    for changed in result.get("changed") or []:
        resolve_inside(root, changed)
    tests = result["tests"]
    if not isinstance(tests, dict) or not {"commands", "passed", "failed"} <= tests.keys():
        raise AckError("result tests must contain commands, passed, and failed")
    if not isinstance(tests["commands"], list) or not all(isinstance(v, str) for v in tests["commands"]):
        raise AckError("test commands must be a list of strings")
    if not isinstance(tests["passed"], int) or not isinstance(tests["failed"], int):
        raise AckError("test counts must be integers")
    return result
