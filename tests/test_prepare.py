import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / ".ack/lib"))

from ack.errors import AckError
from ack.broker import _assert_worker_unowned
from ack.git import allocate_worker_repo


def make_project(directory: str) -> tuple[Path, Path, str]:
    root = Path(directory) / "project"
    (root / ".ack/tasks/active").mkdir(parents=True)
    (root / "PID.md").write_text(f"PROJECT_ROOT: {root}\n", encoding="utf-8")
    (root / "requirements.txt").write_text("Flask>=3\n", encoding="utf-8")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Axiom", "-c", "user.email=axiom@local", "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    task_path = root / ".ack/tasks/active/AX-001.yaml"
    task_path.write_text(yaml.safe_dump({
        "id": "AX-001", "project": "ack", "type": "write", "role": "builder", "model": "trinity-fast",
        "project_root": str(root), "base_commit": base, "worktree": str(root / ".ack/worktrees/AX-001"),
        "skills": [], "objective": "test", "scope": [], "must_not": [], "acceptance": [], "dependencies": [],
        "risk": "low", "authority": {"mutation_allowed": True, "runtime_mutation_allowed": False}, "status": "queued",
    }, sort_keys=False), encoding="utf-8")
    return root, task_path, base


class PrepareReuseTests(unittest.TestCase):
    def test_absent_creates_and_clean_existing_reuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task, _ = make_project(directory)
            with patch("ack.git.prepare_worker_environment", return_value=None):
                first = allocate_worker_repo(task)
                second = allocate_worker_repo(task)
            self.assertEqual(first, second)

    def test_wrong_head_and_dirty_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task, _ = make_project(directory)
            with patch("ack.git.prepare_worker_environment", return_value=None):
                worker = allocate_worker_repo(task)
            (worker / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(AckError, "dirty"):
                allocate_worker_repo(task)
            subprocess.run(["git", "-C", str(worker), "restore", "."], check=True)
            subprocess.run(["git", "-C", str(worker), "-c", "user.name=Axiom", "-c", "user.email=axiom@local", "commit", "--allow-empty", "-qm", "extra"], check=True)
            with self.assertRaisesRegex(AckError, "base_commit"):
                allocate_worker_repo(task)

    def test_active_lease_or_worker_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task_path, _ = make_project(directory)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))

            class LeasePlane:
                def lease_key(self, task_id): return "lease:" + task_id
                def agents(self): return []

            class ActiveLeaseRedis:
                def exists(self, key): return True

            with patch("ack.broker.ControlPlane", return_value=LeasePlane()):
                with self.assertRaisesRegex(AckError, "active lease"):
                    _assert_worker_unowned(ActiveLeaseRedis(), task, root)

            class WorkerPlane:
                def lease_key(self, task_id): return "lease:" + task_id
                def agents(self): return [{"task": "AX-001", "status": "working", "worktree": str(root / ".ack/worktrees/AX-001")}]

            class NoLeaseRedis:
                def exists(self, key): return False

            with patch("ack.broker.ControlPlane", return_value=WorkerPlane()):
                with self.assertRaisesRegex(AckError, "owned by an active worker"):
                    _assert_worker_unowned(NoLeaseRedis(), task, root)

    def test_dependency_preparation_runs_after_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task, _ = make_project(directory)
            with patch("ack.git.prepare_worker_environment", return_value=root / ".ack/runtime/worker-env/AX-001") as prepare:
                allocate_worker_repo(task)
                allocate_worker_repo(task)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(prepare.call_args.args, (root, "AX-001"))


if __name__ == "__main__":
    unittest.main()
