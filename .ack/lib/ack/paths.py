from pathlib import Path
import re

from .errors import AckError


_PROJECT_ROOT = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?PROJECT_ROOT(?:\*\*)?\s*[:=](?:\*\*)?\s*`?([^`\n]+?)`?\s*(?:\*\*)?\s*$", re.I | re.M)


def root_from_pid(pid_path: str | Path) -> Path:
    pid = Path(pid_path).resolve(strict=True)
    match = _PROJECT_ROOT.search(pid.read_text(encoding="utf-8"))
    if not match:
        raise AckError(f"missing PROJECT_ROOT in {pid}")
    configured = Path(match.group(1).strip())
    if not configured.is_absolute():
        raise AckError("PROJECT_ROOT must be absolute")
    return configured.resolve(strict=True)


def validate_root(configured: str | Path, expected: str | Path | None = None) -> Path:
    root = Path(configured)
    if not root.is_absolute():
        raise AckError("project_root must be absolute")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise AckError(f"project_root is not a directory: {resolved}")
    if expected is not None and resolved != Path(expected).resolve(strict=True):
        raise AckError(f"task project_root {resolved} does not match PID root")
    return resolved


def resolve_inside(root: str | Path, requested: str | Path, *, must_exist: bool = False) -> Path:
    boundary = validate_root(root)
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = boundary / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise AckError(f"path does not exist: {candidate}") from exc
    try:
        resolved.relative_to(boundary)
    except ValueError as exc:
        raise AckError(f"path escapes PROJECT_ROOT: {requested}") from exc
    return resolved
