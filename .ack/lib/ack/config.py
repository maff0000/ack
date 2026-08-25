from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
from typing import Any

import yaml

from .errors import AckError


@dataclass(frozen=True)
class Config:
    redis_url: str
    heartbeat_seconds: int = 20
    lease_seconds: int = 60
    degraded_seconds: int = 45
    stale_seconds: int = 90
    max_parallel_agents: int = 4
    agent_command: tuple[str, ...] = ()
    sandbox_executable: str = "bwrap"
    agent_env_allowlist: tuple[str, ...] = ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or os.environ.get("ACK_CONFIG", ".ack/config.yaml"))
    try:
        raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AckError(f"cannot load ACK config {config_path}: {exc}") from exc
    redis_url = os.environ.get("ACK_REDIS_URL") or raw.get("redis_url")
    if not redis_url or (isinstance(redis_url, str) and redis_url.startswith("${")):
        raise AckError("ACK_REDIS_URL or config redis_url is required")
    command = raw.get("agent_command", [])
    if command and (not isinstance(command, list) or not all(isinstance(v, str) for v in command)):
        raise AckError("agent_command must be a YAML list of arguments")
    if command and Path(command[0]).name in {"sh", "bash", "dash", "zsh"}:
        raise AckError("agent_command cannot invoke a shell")
    placeholders = {"{task_file}", "{result_file}", "{project_root}", "{working_dir}", "{model}", "{agent}"}
    for argument in command:
        if ("{" in argument or "}" in argument) and argument not in placeholders:
            raise AckError("task placeholders must occupy a complete argv element")
    sandbox_executable = raw.get("sandbox_executable", "bwrap")
    if not isinstance(sandbox_executable, str) or Path(sandbox_executable).name != "bwrap":
        raise AckError("ACK v0.1 supports only the internally constructed bubblewrap sandbox")
    sandbox_path = shutil.which(sandbox_executable)
    if not sandbox_path:
        raise AckError("bubblewrap executable not found")
    mode = os.stat(sandbox_path)
    if mode.st_uid != 0 or mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AckError("bubblewrap executable must be root-owned and not group/world writable")
    env_allowlist = raw.get("agent_env_allowlist", list(Config.agent_env_allowlist))
    if not isinstance(env_allowlist, list) or not all(isinstance(v, str) and v.replace("_", "").isalnum() for v in env_allowlist):
        raise AckError("agent_env_allowlist must be a list of environment variable names")
    def number(name: str, default: int) -> int:
        value = int(os.environ.get(f"ACK_{name.upper()}", raw.get(name, default)))
        if value <= 0:
            raise AckError(f"{name} must be positive")
        return value
    return Config(
        redis_url=str(redis_url), heartbeat_seconds=number("heartbeat_seconds", 20),
        lease_seconds=number("lease_seconds", 60), degraded_seconds=number("degraded_seconds", 45),
        stale_seconds=number("stale_seconds", 90), max_parallel_agents=number("max_parallel_agents", 4),
        agent_command=tuple(command), sandbox_executable=sandbox_path, agent_env_allowlist=tuple(env_allowlist),
    )
