import json
import os
from pathlib import Path
import subprocess

from .contracts import load_yaml, validate_result, validate_task
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

def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


def _status_paths(root: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        check=True, capture_output=True,
    ).stdout.decode("utf-8")
    paths: list[str] = []
    entries = iter(raw.split("\0"))
    for entry in entries:
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            next(entries, None)
            raise AckError("canonical rename/copy residue must be resolved before integration")
        paths.append(path)
    return paths


def integrate_worker_commit(task_path: str | Path, expected_canonical_head: str) -> tuple[str, str]:
    """
    Integrate one validated ACK write-worker commit into canonical Git.

    This is a mechanical integration primitive only. It does not accept/reject
    work and does not update project state or Redis.
    """
    task_file = Path(task_path).resolve(strict=True)
    raw = load_yaml(task_file)
    root = root_from_pid(Path(raw.get("project_root", "")) / "PID.md")
    task = validate_task(raw, root)

    if task["type"] != "write":
        raise AckError("only write tasks can be integrated")

    if not expected_canonical_head:
        raise AckError("expected canonical HEAD is required")

    canonical_head = _head(root)

    if canonical_head != expected_canonical_head:
        raise AckError(
            f"canonical HEAD moved: expected {expected_canonical_head}, found {canonical_head}"
        )

    worker = resolve_inside(root, task["worktree"], must_exist=True)
    if worker == root or not (worker / ".git").is_dir():
        raise AckError("write task does not reference an isolated worker repository")

    verify_worker_repo(root, worker, task)

    result_path = resolve_inside(worker, f".ack/results/{task['id']}.yaml", must_exist=True)
    result = validate_result(load_yaml(result_path), task["id"], root)

    if result["status"] != "completed":
        raise AckError("only a completed worker result can be integrated")

    worker_commit = result.get("commit", "")
    if not worker_commit:
        raise AckError("completed write result has no worker commit")

    worker_head = subprocess.run(
        ["git", "-C", str(worker), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    worker_branch = subprocess.run(
        ["git", "-C", str(worker), "branch", "--show-current"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    expected_branch = f"ack/{task['id']}/worker"

    if worker_branch != expected_branch:
        raise AckError("worker repository is not on the assigned task branch")

    if worker_head != worker_commit:
        raise AckError("result commit is not the worker branch HEAD")

    worker_dirty = subprocess.run(
        ["git", "-C", str(worker), "status", "--porcelain", "--untracked-files=all"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    # The structured result is controller output and intentionally remains
    # outside the worker commit. No other worker residue is permitted.
    expected_result = f".ack/results/{task['id']}.yaml"
    dirty_lines = [line for line in worker_dirty.splitlines() if line]
    if any(line[3:] != expected_result for line in dirty_lines):
        raise AckError("worker repository contains uncommitted residue outside its result")

    parent = subprocess.run(
        ["git", "-C", str(worker), "rev-parse", f"{worker_commit}^"],
        check=False,
        text=True,
        capture_output=True,
    )

    if parent.returncode != 0 or parent.stdout.strip() != task["base_commit"]:
        raise AckError("worker integration requires exactly one commit above base_commit")

    changed = subprocess.run(
        [
            "git",
            "-C",
            str(worker),
            "diff",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            task["base_commit"],
            worker_commit,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()

    actual_changed = sorted(path for path in changed if path)
    reported_changed = sorted(result.get("changed") or [])

    if actual_changed != reported_changed:
        raise AckError("worker result changed paths do not match committed diff")

    allowed = [Path(value).as_posix().rstrip("/") for value in task["scope"]]

    for changed_path in actual_changed:
        resolved = resolve_inside(root, changed_path)
        relative = resolved.relative_to(root).as_posix()

        if not any(
            relative == item or relative.startswith(item + "/")
            for item in allowed
        ):
            raise AckError(f"worker commit contains path outside task scope: {relative}")

    permitted_control = {
        task_file.relative_to(root).as_posix(),
        f".ack/results/{task['id']}.yaml",
    }
    worker_relative = worker.relative_to(root).as_posix().rstrip("/")
    canonical_residue = _status_paths(root)
    unexpected = sorted(
        path for path in canonical_residue
        if path.rstrip("/") not in permitted_control
        and path.rstrip("/") != worker_relative
        and not path.startswith(worker_relative + "/")
    )
    if unexpected:
        raise AckError(
            "canonical repository has unrelated residue before integration: "
            + ", ".join(unexpected)
        )

    subprocess.run(
        ["git", "-C", str(root), "fetch", str(worker), worker_commit],
        check=True,
        capture_output=True,
    )

    if _head(root) != expected_canonical_head:
        raise AckError("canonical HEAD moved during worker validation")

    try:
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Axiom", "-c", "user.email=axiom@localhost", "cherry-pick", worker_commit],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        subprocess.run(
            ["git", "-C", str(root), "cherry-pick", "--abort"],
            check=False,
            capture_output=True,
        )
        detail = exc.stderr.strip() or "cherry-pick failed"
        raise AckError(f"worker integration failed: {detail}") from exc

    integrated_commit = _head(root)

    return worker_commit, integrated_commit


def commit_project_paths(
    root: str | Path,
    paths: list[str],
    message: str,
    expected_canonical_head: str,
) -> str:
    """Create one guarded canonical commit without granting arbitrary commands."""
    canonical = root_from_pid(Path(root) / "PID.md")
    if _head(canonical) != expected_canonical_head:
        raise AckError("canonical HEAD moved before commit")
    if not paths or not message.strip():
        raise AckError("commit paths and message are required")
    if subprocess.run(
        ["git", "-C", str(canonical), "diff", "--cached", "--quiet"],
        check=False,
    ).returncode != 0:
        raise AckError("canonical index must be clean before guarded commit")
    relative_paths: list[str] = []
    for value in paths:
        resolved = resolve_inside(canonical, value)
        relative = resolved.relative_to(canonical).as_posix()
        if relative == ".git" or relative.startswith(".git/") or relative == ".ack/config.yaml":
            raise AckError(f"path is not eligible for canonical commit: {relative}")
        relative_paths.append(relative)
    subprocess.run(
        ["git", "-C", str(canonical), "add", "--", *relative_paths],
        check=True, capture_output=True,
    )
    staged_paths = subprocess.run(
        ["git", "-C", str(canonical), "diff", "--cached", "--name-only", "-z"],
        check=True, capture_output=True,
    ).stdout.decode("utf-8").rstrip("\0").split("\0")
    allowed = [value.rstrip("/") for value in relative_paths]
    if any(
        not any(path == item or path.startswith(item + "/") for item in allowed)
        for path in staged_paths if path
    ):
        subprocess.run(["git", "-C", str(canonical), "reset", "--quiet"], check=False)
        raise AckError("guarded commit staged a path outside the requested set")
    staged = subprocess.run(
        ["git", "-C", str(canonical), "diff", "--cached", "--quiet", "--", *relative_paths],
        check=False,
    )
    if staged.returncode == 0:
        raise AckError("selected paths contain no staged changes")
    if staged.returncode != 1:
        raise AckError("cannot inspect staged canonical changes")
    if _head(canonical) != expected_canonical_head:
        subprocess.run(
            ["git", "-C", str(canonical), "restore", "--staged", "--", *relative_paths],
            check=False, capture_output=True,
        )
        raise AckError("canonical HEAD moved during commit validation")
    subprocess.run(
        ["git", "-C", str(canonical), "-c", "user.name=Axiom", "-c", "user.email=axiom@localhost", "commit", "-m", message],
        check=True, text=True, capture_output=True,
    )
    return _head(canonical)


def push_project_head(
    root: str | Path,
    expected_canonical_head: str,
    remote: str = "origin",
) -> str:
    """Push the current branch without force after exact HEAD validation."""
    canonical = root_from_pid(Path(root) / "PID.md")
    if _head(canonical) != expected_canonical_head:
        raise AckError("canonical HEAD moved before push")
    if not remote or not all(character.isalnum() or character in "._-" for character in remote):
        raise AckError("invalid Git remote name")
    branch = subprocess.run(
        ["git", "-C", str(canonical), "branch", "--show-current"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    if not branch:
        raise AckError("cannot push a detached canonical HEAD")
    subprocess.run(
        ["git", "-C", str(canonical), "push", remote, f"HEAD:refs/heads/{branch}"],
        check=True, text=True, capture_output=True,
    )
    return expected_canonical_head
