"""Load narrow, host-supplied ACK startup environment without logging values."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

import yaml

from .errors import AckError
from .paths import resolve_inside
from .redact import redact


RUNTIME_ENV_ALLOWLIST = {
    "ACK_REDIS_URL",
    "ACK_WORKER_CODEX_HOME",
    "ACK_LITELLM_URL",
    "ACK_API_KEY",
}
REQUIRED_WORKER_RUNTIME = (
    "ACK_REDIS_URL",
    "ACK_WORKER_CODEX_HOME",
    "ACK_API_KEY",
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AckError(f"cannot load ACK runtime configuration {redact(path)}") from exc
    if not isinstance(raw, dict):
        raise AckError("ACK runtime configuration must be a YAML mapping")
    return raw


def _dotenv_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AckError(f"cannot read ACK runtime source {redact(path)}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            break
        return value
    raise AckError(f"required runtime key {key} is unavailable in configured source")


def _resolve_value(name: str, specification: Any) -> str:
    if isinstance(specification, str):
        if not specification:
            raise AckError(f"required runtime variable {name} is empty")
        return specification
    if not isinstance(specification, dict) or set(specification) != {"source", "key"}:
        raise AckError(f"runtime variable {name} must be a string or source/key mapping")
    source = specification["source"]
    key = specification["key"]
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise AckError(f"runtime variable {name} source must be an absolute path")
    if not isinstance(key, str) or not key.replace("_", "").isalnum():
        raise AckError(f"runtime variable {name} source key is invalid")
    return _dotenv_value(Path(source), key)


def load_startup_runtime(root: Path, config_path: str | Path | None = None) -> Path:
    """Fill missing approved runtime variables from ignored/external host config."""
    explicit = config_path or os.environ.get("ACK_RUNTIME_CONFIG")
    path = Path(explicit) if explicit else root / ".ack/runtime.yaml"
    if not path.is_absolute():
        path = resolve_inside(root, path)
    if not path.is_file():
        raise AckError(f"ACK runtime configuration is required: {redact(path)}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AckError("ACK runtime configuration must not grant group or other permissions")
    raw = _read_yaml(path)
    values = raw.get("environment", {})
    if not isinstance(values, dict):
        raise AckError("ACK runtime environment must be a mapping")
    unknown = set(values) - RUNTIME_ENV_ALLOWLIST
    if unknown:
        raise AckError(f"ACK runtime configuration contains unsupported variables: {', '.join(sorted(unknown))}")
    for name, specification in values.items():
        if name not in os.environ:
            os.environ[name] = _resolve_value(name, specification)
    missing = [name for name in REQUIRED_WORKER_RUNTIME if not os.environ.get(name)]
    if missing:
        raise AckError(f"required ACK worker runtime variables are unavailable: {', '.join(missing)}")
    worker_home = Path(os.environ["ACK_WORKER_CODEX_HOME"])
    if not worker_home.is_absolute() or not worker_home.is_dir():
        raise AckError("ACK_WORKER_CODEX_HOME must identify an existing absolute directory")
    os.environ["ACK_RUNTIME_CONFIG"] = str(path)
    return path
