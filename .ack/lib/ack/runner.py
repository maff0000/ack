import os
from pathlib import Path
import subprocess
import threading
import secrets
import shutil
from typing import Any

import redis

from .config import Config
from .contracts import load_yaml, validate_result, validate_task
from .control import ControlPlane
from .errors import AckError
from .git import verify_worker_repo
from .paths import resolve_inside, root_from_pid
from .skills import compose_skills


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
        result_schema = resolve_inside(root, ".ack/templates/result.schema.json", must_exist=True)
        context = compose_skills(root, task["role"], task.get("skills") or [])
        result_template = resolve_inside(root, ".ack/templates/result.yaml", must_exist=True).read_text(encoding="utf-8")
        prompt = context + "\n\n## TASK\n\n" + task_file.read_text(encoding="utf-8") + "\n\n## REQUIRED RESULT SHAPE\n\n" + result_template + f"\n\nYour result id must be {task['id']} and agent_instance must be {agent}. Use `ack-agent progress <phase> <concise-action>` at meaningful milestones. Return only the structured result; no Markdown fences or surrounding prose."
        template_home_value = os.environ.get("CODEX_HOME")
        if not template_home_value:
            raise AckError("CODEX_HOME provider template is required")
        template_home = resolve_inside(root, template_home_value, must_exist=True)
        runtime_home = resolve_inside(root, f".ack/worktrees/.runtime-sessions/{agent}-{token[:10]}")
        if runtime_home.exists(): raise AckError("unique worker runtime home already exists")
        runtime_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_home, runtime_home)
        client = redis.Redis.from_url(self.config.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        try: client.ping()
        except Exception as exc: raise AckError(f"Redis unavailable: {type(exc).__name__}") from exc
        control = ControlPlane(client, task["project"])
        slot = control.acquire_slot(agent, token, self.config.max_parallel_agents, self.config.lease_seconds)
        if slot is None: raise AckError("maximum parallel agent slots are occupied")
        try: control.start(task, agent, token, self.config.lease_seconds)
        except Exception:
            control.release_slot(token, slot); raise
        stop = threading.Event()
        lost: list[Exception] = []
        def pulse() -> None:
            while not stop.wait(self.config.heartbeat_seconds):
                try:
                    control.heartbeat(task["id"], agent, token, self.config.lease_seconds)
                    control.renew_slot(token, slot, self.config.lease_seconds)
                except Exception as exc:
                    lost.append(exc); stop.set()
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            control.progress(task["id"], agent, token, "agent", "local agent invoked")
            if not self.config.agent_command:
                raise AckError("agent_command is required to run a worker")
            # Codex workspace-write deliberately protects .git. Write workers are
            # already confined by ACK's outer bubblewrap boundary to a private,
            # project-local clone, so the inner client must allow Git metadata.
            native_sandbox = "read-only" if task["type"] == "read" else "danger-full-access"
            replacements = {"task_file": str(task_file), "result_file": str(result_file), "result_schema": str(result_schema), "project_root": str(root), "working_dir": str(working_dir), "model": str(task["model"]), "agent": agent, "sandbox_mode": native_sandbox}
            command = [self.config.sandbox_executable, "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--chdir", str(working_dir)]
            command += ["--bind", str(runtime_home), str(runtime_home)]
            if task["type"] == "write":
                command += ["--bind", str(working_dir), str(working_dir)]
            command += [part.format_map(replacements) for part in self.config.agent_command]
            env = {name: os.environ[name] for name in self.config.agent_env_allowlist if name in os.environ}
            env.update({"ACK_TASK_FILE": str(task_file), "ACK_RESULT_FILE": str(result_file), "ACK_PROJECT_ROOT": str(root), "ACK_MODEL_ALIAS": str(task["model"]), "ACK_AGENT_INSTANCE": agent, "ACK_LEASE_TOKEN": token, "ACK_PROJECT": str(task["project"]), "ACK_TASK_ID": str(task["id"]), "ACK_CONFIG": str(Path(os.environ.get("ACK_CONFIG", ".ack/config.yaml")).resolve())})
            env["CODEX_HOME"] = str(runtime_home)
            env["PATH"] = f"{working_dir / '.ack/tools'}:{env.get('PATH','')}"
            process = subprocess.Popen(command, cwd=working_dir, env=env, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert process.stdin is not None
            process.stdin.write(prompt); process.stdin.close()
            output_parts: list[str] = []
            output_size = [0]
            def drain() -> None:
                assert process.stdout is not None
                for chunk in iter(lambda: process.stdout.read(65536), ""):
                    output_parts.append(chunk)
                    output_size[0] += len(chunk)
                    if output_size[0] > 1_000_000:
                        process.terminate(); break
            reader = threading.Thread(target=drain, daemon=True); reader.start()
            while process.poll() is None:
                if stop.wait(1) and lost:
                    process.terminate()
                    raise AckError(f"worker lease lost: {lost[0]}")
            if process.returncode != 0:
                raise AckError(f"local agent exited {process.returncode}")
            reader.join(timeout=5)
            if reader.is_alive(): raise AckError("local agent result stream did not close")
            output = "".join(output_parts)
            if len(output) > 1_000_000: raise AckError("local agent result exceeded 1 MB")
            if task["type"] == "write":
                dirty = subprocess.run(["git", "-C", str(working_dir), "status", "--porcelain"], check=True, text=True, capture_output=True).stdout.strip()
                if dirty: raise AckError("worker left uncommitted or untracked repository changes")
            result_file.parent.mkdir(parents=True, exist_ok=True)
            safe_result = resolve_inside(working_dir, f".ack/results/{task['id']}.yaml")
            fd = os.open(safe_result, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle: handle.write(output)
            result_file = safe_result
            result = validate_result(load_yaml(result_file), task["id"], root, expected_agent=agent)
            if task["type"] == "read" and result.get("changed"):
                raise AckError("read worker reported repository changes")
            if task["type"] == "write" and result["status"] == "completed":
                commit = result.get("commit", "")
                if not commit:
                    raise AckError("completed write task requires a worker commit")
                final_head = subprocess.run(["git", "-C", str(working_dir), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
                final_branch = subprocess.run(["git", "-C", str(working_dir), "branch", "--show-current"], check=True, text=True, capture_output=True).stdout.strip()
                ancestor = subprocess.run(["git", "-C", str(working_dir), "merge-base", "--is-ancestor", task["base_commit"], commit], check=False)
                if commit != final_head or final_branch != branch or ancestor.returncode != 0 or commit == task["base_commit"]:
                    raise AckError("worker commit is not the assigned branch HEAD descended from base_commit")
            error = "worker reported blockers; inspect result" if result["status"] == "blocked" else ""
            control.finish(task["id"], agent, token, result["status"], result=str(result_file.relative_to(root)), commit=str(result.get("commit") or ""), error=error)
            return 0 if result["status"] == "completed" else 2
        except Exception as exc:
            try: control.finish(task["id"], agent, token, "failed", error=f"{type(exc).__name__}: worker failed; inspect local logs")
            except Exception: pass
            raise
        finally:
            stop.set(); thread.join(timeout=2)
            try: control.release_slot(token, slot)
            except AckError: pass
