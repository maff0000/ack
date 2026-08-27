import os
import json
from pathlib import Path
import subprocess
import threading
import secrets
import shutil
import time
from datetime import datetime, timezone
import re
from typing import Any

import redis
import yaml

from .config import Config
from .contracts import load_yaml, validate_result, validate_task
from .dependencies import worker_environment_path
from .control import ControlPlane
from .errors import AckError
from .git import verify_worker_repo
from .paths import resolve_inside, root_from_pid
from .skills import compose_skills
from .redact import redact
from .time import parse_utc


EXECUTION_TASK_FIELDS = (
    "id", "project", "type", "role", "model", "project_root", "base_commit",
    "worktree", "skills", "objective", "scope", "must_not", "acceptance",
    "dependencies", "risk", "authority",
)

DIAGNOSTIC_OUTPUT_LIMIT = 16_384
SESSION_TRACE_LIMIT = 1_048_576


def worker_guard_classification(
    *,
    elapsed_seconds: float,
    progress_age_seconds: float,
    no_progress_seconds: int,
    max_worker_seconds: int,
    usage_tokens: int = 0,
    max_worker_tokens: int = 0,
    usage_cost_usd: float | None = None,
    max_worker_cost_usd: float = 0.0,
    usage_increasing: bool = False,
    material_evidence: bool = False,
) -> dict[str, Any]:
    """Classify guard state without inferring anything about model internals."""
    if max_worker_tokens and usage_tokens >= max_worker_tokens:
        return {"classification": "resource_ceiling", "stop": True, "reason": f"token budget reached ({usage_tokens})"}
    if usage_cost_usd is not None and max_worker_cost_usd and usage_cost_usd >= max_worker_cost_usd:
        return {"classification": "resource_ceiling", "stop": True, "reason": f"cost budget reached (${usage_cost_usd:.2f})"}
    if elapsed_seconds >= max_worker_seconds:
        return {"classification": "wall_time_ceiling", "stop": True, "reason": f"worker wall time reached ({int(elapsed_seconds)}s)"}
    if progress_age_seconds >= no_progress_seconds:
        classification = "probable_nonproductive_execution" if usage_increasing and not material_evidence else "alive_but_stalled"
        return {"classification": classification, "stop": False, "reason": f"governed progress stale for {int(progress_age_seconds)}s"}
    return {"classification": "progressing", "stop": False, "reason": "governed progress is fresh"}


def _usage_snapshot(runtime_home: Path) -> tuple[int, float | None]:
    """Read already-emitted session usage metadata; absence is acceptable."""
    tokens = 0
    cost: float | None = None
    for path in runtime_home.glob("sessions/**/*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            continue
        for line in lines:
            try: record = json.loads(line)
            except json.JSONDecodeError: continue
            info = record.get("payload", {}).get("info", {}) if isinstance(record, dict) else {}
            usage = info.get("total_token_usage", {}) if isinstance(info, dict) else {}
            if isinstance(usage, dict): tokens = max(tokens, int(usage.get("total_tokens") or 0))
            for key in ("cost_usd", "total_cost_usd", "cost"):
                value = usage.get(key) if isinstance(usage, dict) else None
                if isinstance(value, (int, float)): cost = max(cost or 0.0, float(value))
    return tokens, cost


class _BoundedStreamCapture:
    """Drain a worker stream while retaining only its diagnostic tail."""

    def __init__(self, limit: int = DIAGNOSTIC_OUTPUT_LIMIT) -> None:
        self.limit = limit
        self._buffer = bytearray()
        self.total_bytes = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        self._buffer.extend(chunk)
        if len(self._buffer) > self.limit:
            del self._buffer[:-self.limit]
            self.truncated = True

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")

    def stats(self) -> dict[str, int | bool]:
        return {
            "total_bytes": self.total_bytes,
            "retained_bytes": len(self._buffer),
            "truncated": self.truncated,
        }


def _drain_worker_streams(
    process: subprocess.Popen[bytes],
    limit: int = DIAGNOSTIC_OUTPUT_LIMIT,
) -> tuple[_BoundedStreamCapture, _BoundedStreamCapture, threading.Thread, threading.Thread]:
    stdout = _BoundedStreamCapture(limit)
    stderr = _BoundedStreamCapture(limit)

    def drain(stream: Any, capture: _BoundedStreamCapture) -> None:
        for chunk in iter(lambda: stream.read(65536), b""):
            capture.append(chunk)

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    return stdout, stderr, stdout_thread, stderr_thread


def _bounded_diagnostic(value: Any, limit: int = DIAGNOSTIC_OUTPUT_LIMIT) -> str:
    text = redact("" if value is None else str(value))
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _write_worker_diagnostic(path: Path, diagnostic: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(diagnostic, sort_keys=False))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _write_private_text(path: Path, value: str, limit: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    text = redact(value)
    if len(text.encode("utf-8")) > limit:
        suffix = "\n...[truncated]"
        available = max(0, limit - len(suffix.encode("utf-8")))
        encoded = text.encode("utf-8")[:available]
        text = encoded.decode("utf-8", errors="ignore") + suffix
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _session_trace(runtime_home: Path) -> tuple[Path | None, str]:
    traces = sorted(
        (path for path in runtime_home.glob("sessions/**/*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not traces:
        return None, ""
    source = traces[-1]
    session_id = ""
    try:
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            record = json.loads(line)
            payload = record.get("payload", {}) if isinstance(record, dict) else {}
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = str(payload.get("session_id") or payload.get("id") or "")
                break
    except (OSError, json.JSONDecodeError, UnicodeError):
        pass
    return source, session_id


def _preserve_failure_session(
    runtime_home: Path,
    diagnostic_path: Path,
    stdout: str,
    stderr: str,
) -> dict[str, str]:
    source, session_id = _session_trace(runtime_home)
    prefix = diagnostic_path.with_suffix("")
    paths = {
        "stdout_path": prefix.with_suffix(".stdout.txt"),
        "stderr_path": prefix.with_suffix(".stderr.txt"),
    }
    _write_private_text(paths["stdout_path"], stdout, DIAGNOSTIC_OUTPUT_LIMIT)
    _write_private_text(paths["stderr_path"], stderr, DIAGNOSTIC_OUTPUT_LIMIT)
    metadata: dict[str, str] = {
        "session_id": session_id,
        "source_path": str(source) if source else "",
        "stdout_path": str(paths["stdout_path"]),
        "stderr_path": str(paths["stderr_path"]),
    }
    if source:
        trace_path = prefix.with_suffix(".session.jsonl")
        _write_private_text(trace_path, source.read_text(encoding="utf-8", errors="replace"), SESSION_TRACE_LIMIT)
        metadata["trace_path"] = str(trace_path)
    metadata_path = prefix.with_suffix(".session.yaml")
    _write_worker_diagnostic(metadata_path, metadata)
    return metadata


def _scoped_git_snapshot(working_dir: Path, task: dict[str, Any]) -> bool | None:
    try:
        raw = subprocess.run(
            ["git", "-C", str(working_dir), "status", "--porcelain=v1", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    allowed = [Path(value).as_posix().rstrip("/") for value in task.get("scope", [])]
    for entry in raw.decode("utf-8", errors="replace").split("\0"):
        if not entry:
            continue
        changed = entry[3:]
        if any(changed == item or changed.startswith(item + "/") for item in allowed):
            return True
    return False


def execution_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return only the assignment contract, excluding PL lifecycle/adjudication metadata."""
    return {field: task[field] for field in EXECUTION_TASK_FIELDS if field in task}


def build_worker_prompt(
    context: str,
    task: dict[str, Any],
    result_template: str,
    agent: str,
) -> str:
    """Compose explicit worker execution instructions from a sanitized assignment."""
    assignment = yaml.safe_dump(execution_task(task), sort_keys=False).strip()
    execution_contract = (
        "Acceptance criteria are required actions and outcomes; do not merely repeat them as facts in the result."
    )
    if task["type"] == "write":
        execution_contract += (
            " The primary job is to execute the requested repository mutation using tools. Before returning any"
            " result, you MUST inspect relevant existing files, perform the required scoped filesystem mutation"
            " using available tools, verify the resulting filesystem state, run required or relevant tests or"
            " checks, and leave valid scoped changes uncommitted for ACK Runner. `status: completed` is forbidden"
            " unless the required scoped filesystem changes actually exist. Do not merely describe, simulate,"
            " summarize, or claim the requested change. Do not return the structured result until execution and"
            " verification are finished. Workers must not run git commit, merge, integrate, accept, or reject."
            " The structured result is a post-execution report only."
        )
    return (
        context
        + "\n\n## EXECUTION CONTRACT\n\n"
        + execution_contract
        + "\n\n## TASK\n\n"
        + assignment
        + "\n\n## REQUIRED RESULT SHAPE\n\n"
        + result_template
        + f"\n\nYour result id must be {task['id']} and agent_instance must be {agent}. "
        + "Use `ack-agent progress <phase> <concise-action>` at meaningful milestones. "
        + "Do not create virtual environments or install project dependencies inside the worker worktree; ACK prepares the declared project environment outside it. Report blocked if that environment is unavailable. "
        + "Return only the structured result; no Markdown fences or surrounding prose."
    )


def worker_runtime_profile(task: dict[str, Any], runtime_home: Path) -> tuple[str, dict[str, str]]:
    """Disable nested Codex confinement and return any disposable tester environment."""
    native = "danger-full-access"
    if task["role"] != "tester":
        return native, {}
    home = runtime_home / "home"
    return native, {"HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": "/tmp"}


def build_worker_command(
    config: Config,
    task: dict[str, Any],
    working_dir: Path,
    runtime_home: Path,
    replacements: dict[str, str],
    prompt: str | None = None,
) -> list[str]:
    """Build ACK's authoritative outer sandbox and the nested Codex argv."""
    command = [config.sandbox_executable, "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/etc/codex", "--chdir", str(working_dir)]
    command += ["--bind", str(runtime_home), str(runtime_home)]
    if task["type"] == "write":
        command += ["--bind", str(working_dir), str(working_dir)]
    command += [part.format_map(replacements) for part in config.agent_command]
    if prompt is not None:
        command[-1] = prompt
    return command


def remove_runtime_home(runtime_home: Path) -> None:
    """Remove disposable worker state, failing closed if residue remains."""
    try:
        shutil.rmtree(runtime_home)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AckError(f"worker runtime cleanup failed: {type(exc).__name__}") from exc


def worker_template_home(root: Path) -> Path:
    """Resolve the dedicated worker provider template inside the project."""
    template_home_value = os.environ.get("ACK_WORKER_CODEX_HOME")
    if not template_home_value:
        raise AckError("ACK_WORKER_CODEX_HOME provider template is required")
    return resolve_inside(root, template_home_value, must_exist=True)


def worker_subprocess_environment(config: Config, runtime_home: Path) -> dict[str, str]:
    """Build narrow worker inheritance and force its disposable Codex home."""
    env = {name: os.environ[name] for name in config.agent_env_allowlist if name in os.environ}
    env["CODEX_HOME"] = str(runtime_home)
    return env


def finalize_completed_write(
    task: dict[str, Any],
    working_dir: Path,
    branch: str,
    pending_result: dict[str, Any],
    result_file: Path,
    agent: str,
    root: Path,
) -> dict[str, Any]:
    """Commit and validate observed worker changes before publishing completion."""
    result_file.unlink(missing_ok=True)
    observed_head = subprocess.run(
        ["git", "-C", str(working_dir), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if observed_head != task["base_commit"]:
        raise AckError("worker-created Git commits are prohibited; ACK Runner owns the worker-output commit")
    status_raw = subprocess.run(
        ["git", "-C", str(working_dir), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    dirty_paths: list[str] = []
    for entry in status_raw.decode("utf-8").split("\0"):
        if not entry:
            continue
        if entry[:1] in {"R", "C"} or entry[1:2] in {"R", "C"}:
            raise AckError("worker rename/copy changes require Axiom review")
        dirty_paths.append(entry[3:])
    allowed = [Path(value).as_posix().rstrip("/") for value in task["scope"]]
    for changed_path in dirty_paths:
        if not any(changed_path == item or changed_path.startswith(item + "/") for item in allowed):
            raise AckError(f"worker changed path outside task scope: {changed_path}")
    if not dirty_paths:
        raise AckError("completed write task must produce at least one scoped change")

    subprocess.run(["git", "-C", str(working_dir), "add", "--", *dirty_paths], check=True)
    subprocess.run(
        ["git", "-C", str(working_dir), "-c", "user.name=ACK Worker", "-c", "user.email=ack-worker@localhost", "commit", "-m", f"{task['id']}: worker output"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(working_dir), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    pending_result["commit"] = commit
    pending_result["changed"] = dirty_paths
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not pending_result.get("started_at_utc"):
        pending_result["started_at_utc"] = now
    if not pending_result.get("completed_at_utc"):
        pending_result["completed_at_utc"] = now

    dirty = subprocess.run(
        ["git", "-C", str(working_dir), "status", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if dirty:
        raise AckError("worker controller could not produce a clean commit")
    final_branch = subprocess.run(
        ["git", "-C", str(working_dir), "branch", "--show-current"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    ancestor = subprocess.run(
        ["git", "-C", str(working_dir), "merge-base", "--is-ancestor", task["base_commit"], commit],
        check=False,
    )
    commit_count = subprocess.run(
        ["git", "-C", str(working_dir), "rev-list", "--count", f"{task['base_commit']}..{commit}"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if final_branch != branch or ancestor.returncode != 0 or commit == task["base_commit"] or commit_count != "1":
        raise AckError("worker output must be exactly one commit on the assigned branch descended from base_commit")

    result = validate_result(pending_result, task["id"], root, expected_agent=agent)
    result_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(result_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(result, sort_keys=False))
    return result


class Runner:
    def __init__(self, config: Config):
        self.config = config

    def run(self, task_path: str | Path, agent: str) -> int:
        task_file = Path(task_path).resolve(strict=True)
        candidate_root = Path(load_yaml(task_file).get("project_root", ""))
        pid_root = root_from_pid(candidate_root / "PID.md")
        task = validate_task(load_yaml(task_file), pid_root)
        root = Path(task["project_root"]).resolve(strict=True)
        token = secrets.token_urlsafe(32)
        task_file = resolve_inside(root, task_file, must_exist=True)
        working_dir = root
        branch = ""
        if task["type"] == "write":
            working_dir = resolve_inside(root, task["worktree"], must_exist=True)
            if working_dir == root or not (working_dir / ".git").is_dir():
                raise AckError("write task requires an isolated project-local Git repository")
            verify_worker_repo(root, working_dir, task)
            head = subprocess.run(["git", "-C", str(working_dir), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
            branch = subprocess.run(["git", "-C", str(working_dir), "branch", "--show-current"], check=True, text=True, capture_output=True).stdout.strip()
            if not task.get("base_commit") or head != task["base_commit"]:
                raise AckError("write worktree HEAD does not match task base_commit")
            if not branch or branch in {"main", "master"}:
                raise AckError("worker write task cannot run on the canonical branch")
            required_prefix = f"ack/{task['id']}/"
            if not branch.startswith(required_prefix):
                raise AckError(f"worker branch must start with {required_prefix}")
        result_file = resolve_inside(working_dir, f".ack/results/{task['id']}.yaml")
        if task["type"] == "write":
            result_file.unlink(missing_ok=True)
        result_schema = resolve_inside(root, ".ack/templates/result.schema.json", must_exist=True)
        context = compose_skills(root, task["role"], task.get("skills") or [])
        result_template = resolve_inside(root, ".ack/templates/result.yaml", must_exist=True).read_text(encoding="utf-8")
        prompt = build_worker_prompt(context, task, result_template, agent)
        template_home = worker_template_home(root)
        runtime_home = resolve_inside(root, f".ack/worktrees/.runtime-sessions/{agent}-{token[:10]}")
        if runtime_home.exists(): raise AckError("unique worker runtime home already exists")
        runtime_home.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(template_home, runtime_home)
            client = redis.Redis.from_url(self.config.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
            client.ping()
        except Exception as exc:
            remove_runtime_home(runtime_home)
            raise AckError(f"worker runtime setup failed: {type(exc).__name__}") from exc
        control = ControlPlane(client, task["project"])
        try:
            slot = control.acquire_slot(agent, token, self.config.max_parallel_agents, self.config.lease_seconds)
        except Exception:
            remove_runtime_home(runtime_home)
            raise
        if slot is None:
            remove_runtime_home(runtime_home)
            raise AckError("maximum parallel agent slots are occupied")
        try: control.start(task, agent, token, self.config.lease_seconds)
        except Exception:
            try:
                control.release_slot(token, slot)
            finally:
                remove_runtime_home(runtime_home)
            raise
        stop = threading.Event()
        lost: list[Exception] = []
        guard_stop: list[str] = []
        guard_last: list[str] = []
        guard_started = time.monotonic()
        guard_usage_tokens = 0
        no_progress_limit = int(task.get("no_progress_seconds", self.config.no_progress_seconds))
        wall_limit = int(task.get("max_worker_seconds", self.config.max_worker_seconds))
        token_limit = int(task.get("max_worker_tokens", self.config.max_worker_tokens))
        cost_limit = float(task.get("max_worker_cost_usd", self.config.max_worker_cost_usd))
        def pulse() -> None:
            nonlocal guard_usage_tokens
            while not stop.wait(self.config.heartbeat_seconds):
                try:
                    record = client.hgetall(control.agent_key(agent))
                    progress_value = record.get("progress_at_utc")
                    progress_age = (datetime.now(timezone.utc) - parse_utc(progress_value)).total_seconds() if progress_value else float("inf")
                    usage_tokens, usage_cost = _usage_snapshot(runtime_home)
                    usage_increasing = usage_tokens > guard_usage_tokens
                    guard_usage_tokens = max(guard_usage_tokens, usage_tokens)
                    guard = worker_guard_classification(
                        elapsed_seconds=time.monotonic() - guard_started,
                        progress_age_seconds=progress_age,
                        no_progress_seconds=no_progress_limit,
                        max_worker_seconds=wall_limit,
                        usage_tokens=usage_tokens,
                        max_worker_tokens=token_limit,
                        usage_cost_usd=usage_cost,
                        max_worker_cost_usd=cost_limit,
                        usage_increasing=usage_increasing,
                        material_evidence=_scoped_git_snapshot(working_dir, task) is True or result_file.is_file(),
                    )
                    classification = str(guard["classification"])
                    if classification != "progressing" and classification != (guard_last[0] if guard_last else ""):
                        reason = str(guard["reason"])
                        control.guard(task["id"], agent, token, classification, reason, usage_tokens=usage_tokens, usage_cost_usd=usage_cost)
                        guard_last[:] = [classification]
                    if guard["stop"]:
                        guard_stop[:] = [f"{classification}: {guard['reason']}"]
                        stop.set()
                        break
                    control.heartbeat(task["id"], agent, token, self.config.lease_seconds)
                    control.renew_slot(token, slot, self.config.lease_seconds)
                except Exception as exc:
                    lost.append(exc); stop.set()
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        diagnostic_path = root / ".ack" / "runtime" / "diagnostics" / f"{task['id']}-{agent}-{secrets.token_hex(8)}.yaml"
        diagnostic: dict[str, Any] = {
            "task_id": task["id"],
            "agent_instance": agent,
            "worker_exit_code": None,
            "stdout": "",
            "stderr": "",
            "yaml_parse": {"outcome": "not_attempted"},
            "parsed_top_level_type": None,
            "parsed_status": None,
            "failure_gate": None,
            "failure_class": None,
            "failure_message": None,
            "git_boundaries": [],
        }
        worker_failed = False
        def record_git_boundary(name: str) -> None:
            diagnostic["git_boundaries"].append({"boundary": name, "scoped_mutation": _scoped_git_snapshot(working_dir, task)})
        try:
            control.progress(task["id"], agent, token, "agent", "local agent invoked")
            if not self.config.agent_command:
                raise AckError("agent_command is required to run a worker")
            native_sandbox, profile_env = worker_runtime_profile(task, runtime_home)
            replacements = {"task_file": str(task_file), "result_file": str(result_file), "result_schema": str(result_schema), "project_root": str(root), "working_dir": str(working_dir), "model": str(task["model"]), "agent": agent, "sandbox_mode": native_sandbox}
            command = build_worker_command(self.config, task, working_dir, runtime_home, replacements, prompt)
            env = worker_subprocess_environment(self.config, runtime_home)
            env.update({"ACK_TASK_FILE": str(task_file), "ACK_RESULT_FILE": str(result_file), "ACK_PROJECT_ROOT": str(root), "ACK_MODEL_ALIAS": str(task["model"]), "ACK_AGENT_INSTANCE": agent, "ACK_LEASE_TOKEN": token, "ACK_PROJECT": str(task["project"]), "ACK_TASK_ID": str(task["id"]), "ACK_CONFIG": str(Path(os.environ.get("ACK_CONFIG", ".ack/config.yaml")).resolve())})
            if task["role"] == "tester":
                disposable_home = Path(profile_env["HOME"])
                disposable_home.mkdir(mode=0o700)
                env.update(profile_env)
            env["PATH"] = f"{working_dir / '.ack/tools'}:{env.get('PATH','')}"
            dependency_environment = worker_environment_path(root, task["id"])
            if (dependency_environment / "bin/python").is_file():
                env["VIRTUAL_ENV"] = str(dependency_environment)
                env["PATH"] = f"{dependency_environment / 'bin'}:{env['PATH']}"
            record_git_boundary("before_worker")
            process = subprocess.Popen(command, cwd=working_dir, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout_capture, stderr_capture, reader, error_reader = _drain_worker_streams(process)
            while process.poll() is None:
                if stop.wait(1) and lost:
                    process.terminate()
                    raise AckError(f"worker lease lost: {redact(lost[0])}")
                if stop.is_set() and guard_stop:
                    process.terminate()
                    raise AckError(f"worker guard stopped delivery: {guard_stop[0]}")
            if guard_stop:
                raise AckError(f"worker guard stopped delivery: {guard_stop[0]}")
            diagnostic["worker_exit_code"] = process.returncode
            record_git_boundary("after_worker_exit")
            if process.returncode != 0:
                diagnostic["failure_gate"] = "worker_exit"
                error_reader.join(timeout=5)
                detail = redact(stderr_capture.text()).strip()[-1000:]
                suffix = f": {detail}" if detail else ""
                raise AckError(f"local agent exited {process.returncode}{suffix}")
            reader.join(timeout=5)
            error_reader.join(timeout=5)
            if reader.is_alive(): raise AckError("local agent result stream did not close")
            if error_reader.is_alive(): raise AckError("local agent diagnostic stream did not close")
            output = stdout_capture.text()
            diagnostic["stdout"] = _bounded_diagnostic(output)
            diagnostic["stderr"] = _bounded_diagnostic(stderr_capture.text())
            diagnostic["stdout_stream"] = stdout_capture.stats()
            diagnostic["stderr_stream"] = stderr_capture.stats()
            fenced = re.findall(r"```(?:json|yaml)?\s*\n(.*?)```", output, flags=re.DOTALL | re.IGNORECASE)
            if fenced: output = fenced[-1].strip() + "\n"
            try:
                normalized_result = yaml.safe_load(output)
                diagnostic["yaml_parse"] = {"outcome": "success"}
            except yaml.YAMLError as exc:
                normalized_result = None
                diagnostic["yaml_parse"] = {"outcome": "failure", "message": _bounded_diagnostic(exc)}
            diagnostic["parsed_top_level_type"] = type(normalized_result).__name__
            diagnostic["parsed_status"] = normalized_result.get("status") if isinstance(normalized_result, dict) else None
            record_git_boundary("after_initial_parse")
            if isinstance(normalized_result, dict):
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                normalized_result["id"] = task["id"]
                normalized_result["agent_instance"] = agent
                if task["type"] == "read":
                    normalized_result["changed"] = []
                    normalized_result["commit"] = ""
                if not normalized_result.get("started_at_utc"): normalized_result["started_at_utc"] = now
                if not normalized_result.get("completed_at_utc"): normalized_result["completed_at_utc"] = now
                result_fields = {"id", "agent_instance", "status", "summary", "changed", "commit", "tests", "findings", "risks", "blockers", "evidence", "started_at_utc", "completed_at_utc"}
                normalized_result = {key: value for key, value in normalized_result.items() if key in result_fields}
                output = yaml.safe_dump(normalized_result, sort_keys=False)
            if task["type"] == "write":
                raw = output.strip()
                if raw.startswith("```") and raw.endswith("```"):
                    raw = "\n".join(raw.splitlines()[1:-1])
                try: pending_result = yaml.safe_load(raw)
                except yaml.YAMLError as exc:
                    diagnostic["failure_gate"] = "write_result_yaml_parse"
                    raise AckError("worker did not return structured YAML/JSON") from exc
                if not isinstance(pending_result, dict) or pending_result.get("status") != "completed":
                    diagnostic["failure_gate"] = "write_result_status"
                    raise AckError("write worker left changes without a completed result")
                record_git_boundary("before_write_finalization")
                result = finalize_completed_write(task, working_dir, branch, pending_result, result_file, agent, root)
            else:
                result = validate_result(normalized_result, task["id"], root, expected_agent=agent)
                result_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(result_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(yaml.safe_dump(result, sort_keys=False))
            if task["type"] == "read" and result.get("changed"):
                raise AckError("read worker reported repository changes")
            error = "worker reported blockers; inspect result" if result["status"] == "blocked" else ""
            control.finish(task["id"], agent, token, result["status"], result=str(result_file.relative_to(root)), commit=str(result.get("commit") or ""), error=error)
            return 0 if result["status"] == "completed" else 2
        except Exception as exc:
            worker_failed = True
            diagnostic["failure_class"] = type(exc).__name__
            diagnostic["failure_message"] = _bounded_diagnostic(exc)
            if diagnostic["failure_gate"] is None:
                diagnostic["failure_gate"] = "runner"
            record_git_boundary("failure")
            detail = str(exc)
            try: control.finish(task["id"], agent, token, "failed", error=f"{type(exc).__name__}: {detail}")
            except Exception: pass
            raise
        finally:
            if diagnostic["worker_exit_code"] is None and 'process' in locals():
                diagnostic["worker_exit_code"] = process.returncode
            if "reader" in locals(): reader.join(timeout=5)
            if "error_reader" in locals(): error_reader.join(timeout=5)
            if "stdout_capture" in locals():
                diagnostic["stdout"] = _bounded_diagnostic(stdout_capture.text())
                diagnostic["stdout_stream"] = stdout_capture.stats()
            if "stderr_capture" in locals():
                diagnostic["stderr"] = _bounded_diagnostic(stderr_capture.text())
                diagnostic["stderr_stream"] = stderr_capture.stats()
            if worker_failed:
                try:
                    diagnostic["session_preservation"] = _preserve_failure_session(
                        runtime_home,
                        diagnostic_path,
                        stdout_capture.text() if "stdout_capture" in locals() else "",
                        stderr_capture.text() if "stderr_capture" in locals() else "",
                    )
                except Exception:
                    pass
            try:
                _write_worker_diagnostic(diagnostic_path, diagnostic)
            except Exception:
                pass
            stop.set(); thread.join(timeout=2)
            try:
                control.release_slot(token, slot)
            except AckError:
                pass
            finally:
                remove_runtime_home(runtime_home)
