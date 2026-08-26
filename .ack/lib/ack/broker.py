"""Project-scoped host broker for guarded ACK worker and Git operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import subprocess
from typing import Any

import redis

from .config import load_config
from .control import ControlPlane
from .contracts import load_yaml, validate_result, validate_task
from .errors import AckError
from .git import allocate_worker_repo, commit_project_paths, integrate_worker_commit, push_project_head
from .paths import resolve_inside, root_from_pid
from .pl import validate_project_root
from .redact import redact
from .runner import Runner


class BrokerUnavailable(AckError):
    """The broker could not be reached before a request was dispatched."""


class BrokerOutcomeUnknown(AckError):
    """The request was sent, but its response timed out and needs reconciliation."""

    def __init__(self, operation: str, task: str) -> None:
        self.operation = operation
        self.task = task
        super().__init__(f"ACK broker request timed out after dispatch; reconcile task: {task}")


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "ack_worker_validate", "description": "Validate one existing ACK task against the PID-defined project root.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"], "additionalProperties": False}},
    {"name": "ack_worker_prepare", "description": "Allocate the isolated repository for one validated ACK write task.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"], "additionalProperties": False}},
    {"name": "ack_worker_run", "description": "Run one validated ACK task through the existing confined worker runner.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}, "agent": {"type": "string"}}, "required": ["task", "agent"], "additionalProperties": False}},
    {"name": "ack_worker_reconcile", "description": "Reconcile a dispatched ACK task from Redis terminal state and Git/result evidence without redispatching.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"], "additionalProperties": False}},
    {"name": "ack_worker_integrate", "description": "Mechanically validate and integrate one completed write-worker commit. This does not accept or reject work.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}, "expected_canonical_head": {"type": "string"}}, "required": ["task", "expected_canonical_head"], "additionalProperties": False}},
    {"name": "ack_git_commit", "description": "Commit explicitly selected canonical project paths after an exact HEAD check.", "inputSchema": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "message": {"type": "string", "minLength": 1}, "expected_canonical_head": {"type": "string"}}, "required": ["paths", "message", "expected_canonical_head"], "additionalProperties": False}},
    {"name": "ack_git_push", "description": "Push the exact current canonical HEAD without force.", "inputSchema": {"type": "object", "properties": {"expected_canonical_head": {"type": "string"}, "remote": {"type": "string", "default": "origin"}}, "required": ["expected_canonical_head"], "additionalProperties": False}},
]

_SCHEMAS = {item["name"]: item["inputSchema"] for item in TOOL_SCHEMAS}


def broker_socket_path(root: Path) -> Path:
    return root / ".ack/runtime/broker.sock"


def broker_identity(socket_path: Path, root: Path, timeout: float = 1.0) -> dict[str, Any]:
    """Read the private broker identity without invoking a worker operation."""
    request = {"project_root": str(root), "operation": "__broker_identity", "arguments": {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            response = json.loads(client.makefile("r", encoding="utf-8").readline())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AckError("ACK broker socket is stale or unresponsive") from exc
    result = response.get("result") if isinstance(response, dict) and response.get("ok") is True else None
    if not isinstance(result, dict) or not isinstance(result.get("pid"), int) or not isinstance(result.get("nonce"), str):
        raise AckError("ACK broker identity response is invalid")
    return result


def _validate_arguments(operation: str, arguments: Any) -> dict[str, Any]:
    schema = _SCHEMAS.get(operation)
    if schema is None:
        raise AckError("unknown ACK broker operation")
    if not isinstance(arguments, dict):
        raise AckError("broker arguments must be an object")
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    missing = set(schema.get("required", [])) - set(arguments)
    if unknown or missing:
        raise AckError("broker arguments do not match operation schema")
    for name, value in arguments.items():
        expected = properties[name].get("type")
        if expected == "string" and not isinstance(value, str):
            raise AckError("broker argument type mismatch")
        if expected == "array" and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            raise AckError("broker argument type mismatch")
    return arguments


def _task_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise AckError("task must be a non-empty project-relative path")
    path = resolve_inside(root, value, must_exist=True)
    try:
        path.relative_to(resolve_inside(root, ".ack/tasks/active", must_exist=True))
    except ValueError as exc:
        raise AckError("broker worker task must be under .ack/tasks/active") from exc
    return path


def dispatch(root: Path, operation: str, raw_arguments: Any) -> dict[str, Any]:
    """Validate and execute one guarded operation inside the host broker."""
    root = validate_project_root(root)
    arguments = _validate_arguments(operation, raw_arguments)
    if operation == "ack_worker_validate":
        task = load_yaml(_task_path(root, arguments["task"]))
        validate_task(task, root_from_pid(root / "PID.md"))
        return {"status": "PASS", "task": task["id"]}
    if operation == "ack_worker_prepare":
        worker = allocate_worker_repo(_task_path(root, arguments["task"]))
        return {"status": "PASS", "worker": worker.relative_to(root).as_posix()}
    if operation == "ack_worker_run":
        agent = arguments["agent"]
        if not agent or not all(character.isalnum() or character in "_-" for character in agent):
            raise AckError("agent must be a safe non-empty identifier")
        config_path = Path(os.environ.get("ACK_CONFIG", root / ".ack/config.yaml"))
        if not config_path.is_absolute():
            config_path = root / config_path
        code = Runner(load_config(resolve_inside(root, config_path, must_exist=True))).run(_task_path(root, arguments["task"]), agent)
        return {"status": "PASS" if code == 0 else "INCOMPLETE", "exit_code": code}
    if operation == "ack_worker_reconcile":
        return reconcile_task(root, _task_path(root, arguments["task"]))
    if operation == "ack_worker_integrate":
        worker, integrated = integrate_worker_commit(_task_path(root, arguments["task"]), arguments["expected_canonical_head"])
        return {"status": "PASS", "worker_commit": worker, "integrated_commit": integrated, "acceptance_recorded": False}
    if operation == "ack_git_commit":
        commit = commit_project_paths(root, arguments["paths"], arguments["message"], arguments["expected_canonical_head"])
        return {"status": "PASS", "commit": commit}
    commit = push_project_head(root, arguments["expected_canonical_head"], arguments.get("remote", "origin"))
    return {"status": "PASS", "commit": commit}


def bwrap_probe() -> tuple[int, str]:
    probe = subprocess.run(["bwrap", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "/bin/true"], shell=False, capture_output=True, text=True)
    return probe.returncode, redact(probe.stderr)


def _worker_git_evidence(worker: Path, base_commit: str) -> dict[str, Any]:
    head = subprocess.run(["git", "-C", str(worker), "rev-parse", "HEAD"], check=False, text=True, capture_output=True)
    if head.returncode != 0:
        return {"available": False, "error": "worker HEAD unavailable"}
    worker_head = head.stdout.strip()
    parent = subprocess.run(["git", "-C", str(worker), "rev-parse", f"{worker_head}^"], check=False, text=True, capture_output=True)
    changed = subprocess.run(
        ["git", "-C", str(worker), "diff", "--name-only", "--diff-filter=ACMRTUXB", base_commit, worker_head],
        check=False, text=True, capture_output=True,
    )
    return {
        "available": parent.returncode == 0 and changed.returncode == 0,
        "head": worker_head,
        "parent": parent.stdout.strip(),
        "changed": [path for path in changed.stdout.splitlines() if path],
    }


def _reconcile_outcome(redis_status: str, result: dict[str, Any] | None, git: dict[str, Any]) -> str:
    if redis_status == "completed" and result and git.get("available") and result.get("commit") == git.get("head") and sorted(result.get("changed") or []) == sorted(git.get("changed") or []):
        return "COMPLETED"
    if redis_status in {"failed", "blocked"}:
        return redis_status.upper()
    return "OUTCOME_UNKNOWN"


def reconcile_task(root: Path, task_path: Path) -> dict[str, Any]:
    """Return task truth after a dispatched request without starting another worker."""
    raw = load_yaml(task_path)
    task = validate_task(raw, root_from_pid(root / "PID.md"))
    config_path = Path(os.environ.get("ACK_CONFIG", root / ".ack/config.yaml"))
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_config(resolve_inside(root, config_path, must_exist=True))
    client = redis.Redis.from_url(config.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    client.ping()
    plane = ControlPlane(client, task["project"])
    record = client.hgetall(plane.task_key(task["id"]))
    if not record:
        return {"status": "OUTCOME_UNKNOWN", "reconcile_required": True, "reason": "Redis has no task record", "task": task["id"]}

    worker = resolve_inside(root, task["worktree"], must_exist=True)
    git = _worker_git_evidence(worker, task["base_commit"])
    result_path = worker / ".ack/results" / f"{task['id']}.yaml"
    result: dict[str, Any] | None = None
    result_error = ""
    if result_path.is_file():
        try:
            result = validate_result(load_yaml(result_path), task["id"], root, record.get("agent_instance") or None)
        except AckError as exc:
            result_error = str(exc)

    evidence = {
        "redis_status": record.get("status", "unknown"),
        "agent_instance": record.get("agent_instance", ""),
        "lease_present": bool(client.exists(plane.lease_key(task["id"]))),
        "worker_slots": [number for number in range(1, config.max_parallel_agents + 1) if client.exists(plane.slot_key(number))],
        "git": git,
        "result": {
            "present": result is not None,
            "path": result_path.relative_to(root).as_posix(),
            "commit": result.get("commit", "") if result else "",
            "changed": result.get("changed", []) if result else [],
            "error": result_error,
        },
    }
    status = record.get("status")
    outcome = _reconcile_outcome(status or "unknown", result, git)
    return {"status": outcome, "reconcile_required": outcome == "OUTCOME_UNKNOWN", "task": task["id"], "evidence": evidence}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline(1_000_001))
            if not isinstance(request, dict) or set(request) != {"project_root", "operation", "arguments"}:
                raise AckError("invalid broker request envelope")
            if request["project_root"] != str(self.server.project_root):  # type: ignore[attr-defined]
                raise AckError("broker request project root mismatch")
            if request["operation"] == "__broker_identity" and request["arguments"] == {}:
                value = {"pid": os.getpid(), "nonce": self.server.owner_nonce}  # type: ignore[attr-defined]
            else:
                value = dispatch(self.server.project_root, request["operation"], request["arguments"])  # type: ignore[attr-defined]
            response = {"ok": True, "result": value}
        except Exception as exc:
            response = {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


class BrokerServer(socketserver.UnixStreamServer):
    def __init__(self, root: Path, socket_path: Path, owner_nonce: str):
        self.project_root = validate_project_root(root)
        if not owner_nonce:
            raise AckError("ACK broker owner nonce is required")
        self.owner_nonce = owner_nonce
        expected_socket = broker_socket_path(self.project_root)
        if socket_path != expected_socket:
            raise AckError("ACK broker socket does not match project binding")
        runtime = resolve_inside(root, ".ack/runtime")
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime.chmod(0o700)
        if socket_path.exists():
            raise AckError("ACK broker already active or stale socket requires operator review")
        super().__init__(str(socket_path), _Handler)
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        self.socket_path = socket_path
        endpoint = socket_path.lstat()
        self.socket_identity = (endpoint.st_dev, endpoint.st_ino)

    def server_close(self) -> None:
        super().server_close()
        try:
            endpoint = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(endpoint.st_mode) and (endpoint.st_dev, endpoint.st_ino) == self.socket_identity:
            self.socket_path.unlink()


def serve_broker(root: Path, socket_path: Path, owner_nonce: str) -> None:
    code, error = bwrap_probe()
    if code != 0:
        raise AckError(f"host broker bwrap probe failed: {error.strip() or code}")
    with BrokerServer(root, socket_path, owner_nonce) as server:
        server.serve_forever(poll_interval=0.2)


def broker_call(socket_path: Path, root: Path, operation: str, arguments: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    request = {"project_root": str(root), "operation": operation, "arguments": arguments}
    dispatched = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            dispatched = True
            response = json.loads(client.makefile("r", encoding="utf-8").readline())
    except socket.timeout as exc:
        if dispatched:
            raise BrokerOutcomeUnknown(operation, str(arguments.get("task", ""))) from exc
        raise BrokerUnavailable("ACK host broker unavailable") from exc
    except (ConnectionError, FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
        if dispatched:
            raise BrokerOutcomeUnknown(operation, str(arguments.get("task", ""))) from exc
        raise BrokerUnavailable("ACK host broker unavailable") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise AckError(str(response.get("error", "ACK host broker rejected request")) if isinstance(response, dict) else "invalid ACK broker response")
    return response["result"]
