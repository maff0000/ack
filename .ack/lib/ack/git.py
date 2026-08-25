import json
import os
from pathlib import Path
import subprocess

from .contracts import load_yaml, validate_task
from .errors import AckError
from .paths import resolve_inside, root_from_pid


def allocate_worker_repo(task_path: str | Path) -> Path:
    task_file = Path(task_path).resolve(strict=True)
    raw = load_yaml(task_file)
    root = root_from_pid(Path(raw.get("project_root", "")) / "PID.md")
    task = validate_task(raw, root)
    if task["type"] != "write": raise AckError("only write tasks require an isolated repository")
    target = resolve_inside(root, task["worktree"])
    allowed = resolve_inside(root, ".ack/worktrees")
    try: target.relative_to(allowed)
    except ValueError as exc: raise AckError("worker repository must be under .ack/worktrees") from exc
    if target.exists(): raise AckError(f"worker repository already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(root), str(target)], check=True)
    branch = f"ack/{task['id']}/worker"
    subprocess.run(["git", "-C", str(target), "switch", "-c", branch, task["base_commit"]], check=True)
    marker = {"project_root": str(root), "task": task["id"], "base_commit": task["base_commit"], "branch": branch, "no_hardlinks": True}
    (target / ".git/ack-provenance.json").write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    verify_worker_repo(root, target, task)
    return target


def verify_worker_repo(root: Path, target: Path, task: dict) -> None:
    marker_path = target / ".git/ack-provenance.json"
    try: marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise AckError("worker repository lacks valid ACK allocation provenance") from exc
    expected = {"project_root": str(root), "task": task["id"], "base_commit": task["base_commit"], "branch": f"ack/{task['id']}/worker", "no_hardlinks": True}
    if marker != expected: raise AckError("worker repository provenance does not match task")
    canonical_objects = root / ".git/objects"
    worker_objects = target / ".git/objects"
    for worker_file in worker_objects.rglob("*"):
        if not worker_file.is_file(): continue
        relative = worker_file.relative_to(worker_objects)
        canonical_file = canonical_objects / relative
        if canonical_file.is_file():
            left, right = os.stat(worker_file), os.stat(canonical_file)
            if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
                raise AckError(f"worker Git object shares canonical inode: {relative}")
