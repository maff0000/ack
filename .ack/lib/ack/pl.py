"""PID-bound Project Lead launch and capability preflight."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import secrets
import stat
import subprocess
import tempfile
import json
import time
from datetime import datetime, timezone

import redis
import yaml

from .config import load_config
from .errors import AckError
from .paths import resolve_inside, root_from_pid
from .redact import redact


MCP_ENV_ALLOWLIST: tuple[str, ...] = ()
BROKER_ONLY_ENV = {
    "ACK_REDIS_URL", "ACK_CONFIG", "ACK_WORKER_CODEX_HOME",
    "ACK_LITELLM_URL", "ACK_API_KEY", "ACK_RUNTIME_CONFIG",
}


def validate_pid_root(requested: str | Path) -> Path:
    raw = Path(requested)
    if not raw.is_absolute():
        raise AckError("project root must be an absolute path")
    if ".." in raw.parts:
        raise AckError("project root must not contain '..'")
    try:
        canonical = raw.resolve(strict=True)
    except OSError as exc:
        raise AckError(f"project root does not exist: {redact(raw)}") from exc
    if raw.absolute() != canonical:
        raise AckError("project root must be canonical (symlink aliases are rejected)")
    if not canonical.is_dir():
        raise AckError("project root is not a directory")
    if not (canonical / "PID.md").is_file():
        raise AckError("project root missing required marker: PID.md")
    pid_root = root_from_pid(canonical / "PID.md")
    if pid_root != canonical:
        raise AckError(f"PID PROJECT_ROOT mismatch: expected {canonical}, found {pid_root}")
    return canonical


def validate_project_root(requested: str | Path) -> Path:
    canonical = validate_pid_root(requested)
    if not (canonical / "AXIOM.md").is_file():
        raise AckError("project root missing required marker: AXIOM.md")
    if not (canonical / ".ack").is_dir():
        raise AckError("project root missing required marker: .ack")
    state_path = canonical / ".ack/state/project.yaml"
    try:
        state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AckError(f"cannot load project state: {redact(exc)}") from exc
    state_root = state.get("project_root")
    if not isinstance(state_root, str) or not Path(state_root).is_absolute() or Path(state_root).resolve(strict=True) != canonical:
        raise AckError("project state root does not match PID root")
    try:
        git_root = Path(_git(canonical, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
    except Exception as exc:
        raise AckError(f"cannot establish project Git root: {redact(exc)}") from exc
    if git_root != canonical:
        raise AckError("Git repository root does not match PID root")
    return canonical


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], check=check, text=True, capture_output=True)


def preflight(root_value: str | Path, config_path: str | Path | None = None, *, allow_redis_degraded: bool = False) -> tuple[bool, list[str]]:
    lines: list[str] = []
    failed = False
    degraded = False

    def report(name: str, value: str = "OK") -> None:
        lines.append(f"{name:<16} {redact(value)}")

    try:
        root = validate_project_root(root_value)
        report("PROJECT_ROOT")
        report("PID")
    except Exception as exc:
        return False, [f"PROJECT_ROOT     FAIL {redact(exc)}", "STATUS           NOT READY"]

    git_dir: Path | None = None
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
        if top != root:
            raise AckError(f"Git repository root mismatch: {top}")
        raw_git = _git(root, "rev-parse", "--git-dir").stdout.strip()
        git_dir = (root / raw_git).resolve(strict=True) if not Path(raw_git).is_absolute() else Path(raw_git).resolve(strict=True)
        branch = _git(root, "branch", "--show-current").stdout.strip() or "DETACHED"
        report("GIT_BRANCH", branch)
    except Exception as exc:
        failed = True
        report("GIT_REPOSITORY", f"FAIL {type(exc).__name__}: {exc}")

    probe: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".ack-preflight-", dir=root / ".ack", delete=False) as handle:
            probe = Path(handle.name)
            handle.write(b"probe")
        if probe.read_bytes() != b"probe":
            raise AckError("project probe verification failed")
        probe.unlink()
        report("PROJECT_WRITE")
    except Exception as exc:
        failed = True
        report("PROJECT_WRITE", f"FAIL {type(exc).__name__}: {exc}")
        try:
            if probe is not None:
                probe.unlink(missing_ok=True)
        except OSError:
            pass

    if git_dir is not None:
        token = secrets.token_hex(8)
        ref = f"refs/ack/preflight-{token}"
        index_probe = git_dir / f"ack-preflight-index-{token}"
        object_path: Path | None = None
        object_existed = False
        try:
            payload = f"ACK preflight {token}\n".encode()
            oid = subprocess.run(
                ["git", "-C", str(root), "hash-object", "--stdin"], input=payload,
                check=True, capture_output=True,
            ).stdout.decode().strip()
            object_path = git_dir / "objects" / oid[:2] / oid[2:]
            object_existed = object_path.exists()
            written = subprocess.run(
                ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=payload,
                check=True, capture_output=True,
            ).stdout.decode().strip()
            if written != oid:
                raise AckError("Git object probe verification failed")
            _git(root, "update-ref", ref, "HEAD")
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(index_probe)
            subprocess.run(["git", "-C", str(root), "read-tree", "HEAD"], env=env, check=True, capture_output=True)
            report("GIT_METADATA")
        except Exception as exc:
            detail = exc.stderr.decode(errors="replace").strip() if isinstance(getattr(exc, "stderr", None), bytes) else str(exc)
            failed = True; report("GIT_METADATA", f"FAIL {type(exc).__name__}: {detail}")
        finally:
            _git(root, "update-ref", "-d", ref, check=False)
            try:
                index_probe.unlink(missing_ok=True)
                index_probe.with_name(index_probe.name + ".lock").unlink(missing_ok=True)
            except OSError:
                pass
            if object_path is not None and not object_existed:
                try:
                    object_path.unlink(missing_ok=True)
                    object_path.parent.rmdir()
                except OSError:
                    pass

    try:
        origin = _git(root, "remote", "get-url", "origin").stdout.strip()
        report("ORIGIN", "configured")
        query = _git(root, "ls-remote", "origin", check=False)
        if query.returncode != 0:
            raise AckError(query.stderr.strip() or "remote query failed")
        report("REMOTE_QUERY")
    except subprocess.CalledProcessError:
        report("ORIGIN", "not configured")
    except Exception as exc:
        failed = True
        report("REMOTE_QUERY", f"FAIL {type(exc).__name__}: {exc}")

    sandbox = shutil.which("bwrap")
    if not sandbox:
        failed = True
        report("WORKER_RUNTIME", "FAIL bubblewrap executable not found")
    else:
        probe = subprocess.run(
            [sandbox, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
            check=False, text=True, capture_output=True,
        )
        if probe.returncode == 0:
            report("WORKER_RUNTIME")
        else:
            failed = True
            detail = probe.stderr.strip() or f"bubblewrap exited {probe.returncode}"
            report("WORKER_RUNTIME", f"FAIL {detail}")

    try:
        config = load_config(config_path or root / ".ack/config.yaml", require_redis=not allow_redis_degraded)
        report("ACK_CONFIG")
        if not config.redis_url:
            degraded = True
            report("REDIS", "DEGRADED unavailable; durable recovery only")
        else:
            client = redis.Redis.from_url(config.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
            try:
                client.ping()
                report("REDIS")
            except Exception as exc:
                if not allow_redis_degraded: failed = True
                else: degraded = True
                report("REDIS", f"{'DEGRADED' if allow_redis_degraded else 'FAIL'} {type(exc).__name__}")
        if not config.agent_command or not shutil.which(config.agent_command[0]):
            raise AckError("configured worker command is unavailable")
        report("WORKER_COMMAND")
        report("SANDBOX", Path(config.sandbox_executable).name)
    except Exception as exc:
        failed = True
        report("ACK_RUNTIME", f"FAIL {type(exc).__name__}: {exc}")
    try:
        pl_command = build_pl_command(root)
        version = subprocess.run([pl_command[0], "--version"], check=True, text=True, capture_output=True)
        if not version.stdout.strip():
            raise AckError("Codex version probe returned no output")
        report("PL_CODEX")
    except Exception as exc:
        failed = True
        report("PL_CODEX", f"FAIL {type(exc).__name__}: {exc}")
    try:
        bridge = pl_mcp_bridge(root)
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        env = os.environ.copy()
        env["ACK_PROJECT_ROOT"] = str(root)
        env["ACK_BROKER_SOCKET"] = str(root / ".ack/runtime/broker.sock")
        probe = subprocess.run(
            [str(bridge)], input=request, check=True, text=True,
            capture_output=True, env=env, timeout=5,
        )
        response = json.loads(probe.stdout.splitlines()[0])
        if response.get("result", {}).get("serverInfo", {}).get("name") != "ack-pl":
            raise AckError("ACK PL MCP handshake returned an unexpected identity")
        configured = subprocess.run(
            [*build_pl_command(root), "mcp", "list", "--json"],
            check=True, text=True, capture_output=True, timeout=10,
        )
        servers = json.loads(configured.stdout)
        expected_bridge = str(bridge)
        if not any(
            item.get("name") == "ack_pl"
            and item.get("enabled") is True
            and item.get("transport", {}).get("type") == "stdio"
            and item.get("transport", {}).get("command") == expected_bridge
            and item.get("transport", {}).get("cwd") == str(root)
            and item.get("transport", {}).get("env", {}).get("ACK_PROJECT_ROOT") == str(root)
            for item in servers
        ):
            raise AckError("Codex did not accept the required project-scoped MCP configuration")
        report("PL_MCP")
    except Exception as exc:
        failed = True
        report("PL_MCP", f"FAIL {type(exc).__name__}: {exc}")
    report("STATUS", "NOT READY" if failed else ("READY DEGRADED" if degraded else "READY"))
    return not failed, lines


def pl_mcp_bridge(root: Path) -> Path:
    bridge = resolve_inside(root, ".ack/tools/ack-pl-mcp", must_exist=True)
    if not bridge.is_file() or not os.access(bridge, os.X_OK):
        raise AckError("ACK PL MCP bridge is missing or not executable")
    return bridge


def _mcp_overrides(root: Path) -> list[str]:
    bridge = pl_mcp_bridge(root)
    tools = [
        "ack_worker_validate", "ack_worker_prepare", "ack_worker_run", "ack_worker_reconcile",
        "ack_worker_integrate", "ack_git_commit", "ack_git_push",
    ]
    values = {
        "mcp_servers.ack_pl.command": str(bridge),
        "mcp_servers.ack_pl.cwd": str(root),
        "mcp_servers.ack_pl.required": True,
        "mcp_servers.ack_pl.enabled_tools": tools,
        "mcp_servers.ack_pl.env.ACK_PROJECT_ROOT": str(root),
        "mcp_servers.ack_pl.env.ACK_BROKER_SOCKET": str(root / ".ack/runtime/broker.sock"),
        "mcp_servers.ack_pl.default_tools_approval_mode": "approve",
    }
    arguments: list[str] = []
    for key, value in values.items():
        encoded = json.dumps(value) if not isinstance(value, bool) else str(value).lower()
        arguments.extend(["-c", f"{key}={encoded}"])
    return arguments


def build_pl_command(root: Path, executable_value: str | None = None) -> list[str]:
    requested = executable_value or os.environ.get("ACK_PL_CODEX", "codex")
    executable = shutil.which(requested) if "/" not in requested else requested
    if not executable:
        raise AckError(f"Codex command not found: {requested}")
    if Path(executable).name == "axel":
        raise AckError("trusted PL must invoke Codex directly, not nested axel")
    return [executable, "-C", str(root), *_mcp_overrides(root)]


RESUME_INSTRUCTION = """Resume the existing ACK-governed project at ACK_PROJECT_ROOT. Do not require or rely on prior chat history. Recover durable truth in this order: PID.md; AXIOM.md; .ack/state/project.yaml; Git status and history; active and archived tasks; results and evidence; relevant ADRs; then Redis live state and events if available. If Redis is unavailable, continue from Git and project state and report live-control degradation accurately; do not treat the project as unknown. Reconcile expired or stale leases before redispatch. If no tasks are active but the current PID objective is incomplete, inspect accepted work and determine the next bounded tasks. Only Axiom may accept or reject recovered pending worker work. Do not regenerate or overwrite PID.md, PROJECT.md, or project state as a new bootstrap."""


def build_resume_command(root: Path, executable_value: str | None = None) -> list[str]:
    return [*build_pl_command(root, executable_value), RESUME_INSTRUCTION]


def managed_codex_environment(root: Path) -> dict[str, str]:
    """Return the PL environment without host-broker worker credentials."""
    environment = {name: value for name, value in os.environ.items() if name not in BROKER_ONLY_ENV}
    environment["ACK_PROJECT_ROOT"] = str(root)
    environment["ACK_BROKER_SOCKET"] = str(root / ".ack/runtime/broker.sock")
    return environment


class BrokerProcess:
    """Own one fail-loud project broker for the lifetime of managed Codex."""

    def __init__(self, root: Path):
        self.root = validate_project_root(root)
        self.socket_path = self.root / ".ack/runtime/broker.sock"
        self.process: subprocess.Popen[str] | None = None
        self.owner_nonce = secrets.token_hex(32)
        self.socket_identity: tuple[int, int] | None = None

    def __enter__(self) -> "BrokerProcess":
        from .broker import broker_identity

        if self.socket_path.exists():
            try:
                identity = broker_identity(self.socket_path, self.root)
            except AckError as exc:
                raise AckError("ACK broker startup refused stale socket; operator review required") from exc
            raise AckError(f"ACK broker already active under pid {identity['pid']}")
        broker = resolve_inside(self.root, ".ack/tools/ack-broker", must_exist=True)
        self.process = subprocess.Popen(
            [str(broker), "--project-root", str(self.root), "--socket", str(self.socket_path), "--owner-nonce", self.owner_nonce],
            cwd=self.root,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.socket_path.is_socket():
                try:
                    identity = broker_identity(self.socket_path, self.root, timeout=0.2)
                except AckError:
                    identity = None
                if identity and identity["nonce"] == self.owner_nonce and identity["pid"] == self.process.pid:
                    endpoint = self.socket_path.lstat()
                    self.socket_identity = (endpoint.st_dev, endpoint.st_ino)
                    return self
                if identity and identity["nonce"] != self.owner_nonce:
                    self.stop()
                    raise AckError(f"ACK broker duplicate launch lost ownership race to pid {identity['pid']}")
            if self.process.poll() is not None:
                detail = redact(self.process.stderr.read()).strip() if self.process.stderr else ""
                raise AckError(f"ACK host broker failed to start: {detail or self.process.returncode}")
            time.sleep(0.02)
        self.stop()
        raise AckError("ACK host broker startup timed out")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.socket_identity is not None:
            try:
                endpoint = self.socket_path.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISSOCK(endpoint.st_mode) and (endpoint.st_dev, endpoint.st_ino) == self.socket_identity:
                self.socket_path.unlink()

    def __exit__(self, *_: object) -> None:
        self.stop()


def launch(root: Path, command: list[str] | None = None, *, resume: bool = False) -> int:
    os.chdir(root)
    with BrokerProcess(root):
        os.environ["ACK_BROKER_SOCKET"] = str(root / ".ack/runtime/broker.sock")
        argv = command or (build_resume_command(root) if resume else build_pl_command(root))
        env = managed_codex_environment(root)
        return subprocess.run(argv, cwd=root, env=env, check=False).returncode


def _copy_without_overwrite(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            raise AckError(f"bootstrap refuses to overwrite existing project truth: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise AckError(f"bootstrap refuses to overwrite existing project truth: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def bootstrap_project(root_value: str | Path) -> Path:
    """Mechanically adopt an Architect-approved PID into the portable ACK kit."""
    root = validate_pid_root(root_value)
    kit = Path(__file__).resolve().parents[2]
    framework = kit.parent
    copies: list[tuple[Path, Path]] = [
        (kit / "config.example.yaml", root / ".ack/config.example.yaml"),
        (kit / "runtime.example.yaml", root / ".ack/runtime.example.yaml"),
        (kit / "requirements.txt", root / ".ack/requirements.txt"),
    ]
    for directory in ("lib", "rules", "templates", "tools"):
        for source in (kit / directory).rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts:
                copies.append((source, root / ".ack" / source.relative_to(kit)))
    for source in (kit / "skills").rglob("*"):
        if source.is_file() and source.relative_to(kit).as_posix() != "skills/project/PROJECT.md":
            copies.append((source, root / ".ack" / source.relative_to(kit)))
    copies.append((framework / "AXIOM.md", root / "AXIOM.md"))
    for source, destination in copies:
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != source.read_bytes()):
            raise AckError(f"bootstrap refuses to overwrite existing project truth: {destination}")
    if (root / ".git").exists():
        git_root = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
        if git_root != root:
            raise AckError("existing Git repository root does not match PID root")
    else:
        _git(root, "init", "-b", "main")
    for source, destination in copies:
        _copy_without_overwrite(source, destination)

    project_skill = root / ".ack/skills/project/PROJECT.md"
    if not project_skill.exists():
        _write_new(project_skill, "# Project Essentials\n\n- `PID.md` is the approved project authority.\n- Canonical `PROJECT_ROOT` is `" + str(root) + "`.\n- Workers may not mutate canonical Git or broaden the PID scope.\n- Record only verified project-specific constraints here.\n")
    state = root / ".ack/state/project.yaml"
    if not state.exists():
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        data = {"project": root.name, "project_root": str(root), "pid_version": "0.1.1", "branch": _git(root, "branch", "--show-current").stdout.strip() or "main", "current_objective": "Recover the approved PID and begin Axiom-led execution.", "active_tasks": [], "blocked_tasks": [], "last_completed": [], "last_updated_utc": now}
        _write_new(state, yaml.safe_dump(data, sort_keys=False))
    for directory in ("decisions", "evidence", "results", "tasks/active", "tasks/archive", "worktrees"):
        (root / ".ack" / directory).mkdir(parents=True, exist_ok=True)
    runtime_config = root / ".ack/config.yaml"
    if not runtime_config.exists():
        _copy_without_overwrite(root / ".ack/config.example.yaml", runtime_config)
    ignore = root / ".gitignore"
    additions = [".ack/config.yaml", ".ack/runtime.yaml", ".ack/worktrees/", "__pycache__/", "*.py[cod]"]
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    missing = [item for item in additions if item not in existing.splitlines()]
    if missing:
        with ignore.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n".join(missing) + "\n")
    return validate_project_root(root)
