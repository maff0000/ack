from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import time
import unittest
import shutil
import subprocess
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / ".ack/lib"))

from ack.contracts import planning_advisories, validate_result, validate_task
from ack.config import load_config
from ack.control import ControlPlane, STATUS_FIELDS
from ack.dependencies import prepare_worker_environment, worker_environment_path
from ack.errors import AckError
from ack.git import allocate_worker_repo, commit_project_paths, integrate_worker_commit, verify_worker_repo
from ack.paths import resolve_inside, root_from_pid, validate_root
from ack.skills import compose_skills
from ack.time import utc_text, utc_now
from tests.fakes import FakeRedis


def bubblewrap_usable():
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def task(root, kind="read", mutation=False):
    return {"id":"AX-001", "project":"ack", "type":kind, "role":"scout", "model":"trinity-fast", "project_root":str(root), "base_commit":"abc", "worktree":str(Path(root)/"wt") if kind == "write" else "", "skills":[], "objective":"inspect", "scope":[], "must_not":[], "acceptance":[], "dependencies":[], "risk":"low", "authority":{"mutation_allowed":mutation,"runtime_mutation_allowed":False}, "status":"queued"}


def result(root, status="completed"):
    return {"id":"AX-001","agent_instance":"A01","status":status,"summary":"done","changed":[],"commit":"","tests":{"commands":[],"passed":0,"failed":0},"findings":[],"risks":[],"blockers":[],"evidence":[],"started_at_utc":utc_text(),"completed_at_utc":utc_text()}


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "root"; self.root.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def test_root_validation(self): self.assertEqual(validate_root(self.root), self.root.resolve())
    def test_in_root_accepted(self): self.assertEqual(resolve_inside(self.root, "a/b"), self.root / "a/b")
    def test_dotdot_escape_rejected(self):
        with self.assertRaises(AckError): resolve_inside(self.root, "../escape")
    def test_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "outside"; outside.mkdir(); (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AckError): resolve_inside(self.root, "link/file")

    def test_planning_advisories_are_non_blocking_heuristics(self):
        data = task(self.root, "write", True)
        data["scope"] = ["one.py", "two.py", "three.py"]
        data["acceptance"] = [str(index) for index in range(7)]
        self.assertEqual(
            planning_advisories(data),
            [
                "multiple primary paths: consider sequential independently verifiable deliveries",
                "large acceptance set: consider decomposing into bounded deliveries",
            ],
        )
        validate_task(data, self.root)

    def test_small_task_has_no_planning_advisory(self):
        self.assertEqual(planning_advisories(task(self.root)), [])

    def test_project_dependencies_are_prepared_outside_worker_tree(self):
        (self.root / "requirements.txt").write_text("Flask>=3\n", encoding="utf-8")
        environment = worker_environment_path(self.root, "EB-TEST")
        with patch("ack.dependencies.subprocess.run") as run:
            returned = prepare_worker_environment(self.root, "EB-TEST")
        self.assertEqual(returned, environment)
        self.assertNotIn("EB-TEST", str(self.root / ".ack/worktrees"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3], [sys.executable, "-m", "venv"])
        self.assertEqual(run.call_args_list[1].args[0][1:4], ["-m", "pip", "install"])

    def test_governed_host_commit_selects_only_requested_paths(self):
        root = Path(self.tmp.name) / "git-project"
        root.mkdir()
        (root / "PID.md").write_text(f"PROJECT_ROOT: `{root}`\n", encoding="utf-8")
        (root / "AXIOM.md").write_text("before\n", encoding="utf-8")
        (root / "unrelated.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "PID.md", "AXIOM.md", "unrelated.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-m", "base"], check=True, capture_output=True)
        expected = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
        (root / "AXIOM.md").write_text("after\n", encoding="utf-8")
        (root / "unrelated.txt").write_text("still dirty\n", encoding="utf-8")

        committed = commit_project_paths(root, ["AXIOM.md"], "governed doctrine change", expected)

        self.assertNotEqual(committed, expected)
        changed = subprocess.run(["git", "-C", str(root), "show", "--format=", "--name-only", committed], check=True, text=True, capture_output=True).stdout.splitlines()
        self.assertEqual(changed, ["AXIOM.md"])
        self.assertEqual((root / "unrelated.txt").read_text(encoding="utf-8"), "still dirty\n")
        self.assertEqual(subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True, text=True, capture_output=True).stdout.strip(), "M unrelated.txt")

    def test_pid_root(self):
        (self.root/"PID.md").write_text(f"PROJECT_ROOT: `{self.root}`\n")
        self.assertEqual(root_from_pid(self.root/"PID.md"), self.root.resolve())
    def test_canonical_pid_root(self): self.assertEqual(root_from_pid(ROOT/"PID.md"), ROOT.resolve())
    @unittest.skipUnless(bubblewrap_usable(), "bubblewrap namespaces unavailable")
    def test_process_sandbox_enforces_read_and_project_write(self):
        sibling = ROOT.parent / "battleships"
        read = subprocess.run(["bwrap","--ro-bind","/","/","--dev","/dev","--proc","/proc","--tmpfs","/tmp","--chdir",str(ROOT),"sh","-c",f"test ! -w {ROOT} && test ! -w {sibling}"],check=False)
        self.assertEqual(read.returncode,0)
        write = subprocess.run(["bwrap","--ro-bind","/","/","--bind",str(ROOT),str(ROOT),"--dev","/dev","--proc","/proc","--tmpfs","/tmp","--chdir",str(ROOT),"sh","-c",f"test -w {ROOT} && test ! -w {sibling}"],check=False)
        self.assertEqual(write.returncode,0)


class ContractTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_write_authority_required(self):
        with self.assertRaises(AckError): validate_task(task(self.root, "write", False), self.root)
    def test_write_authority_valid(self): self.assertTrue(validate_task(task(self.root,"write",True),self.root))
    def test_worker_cannot_accept_task(self):
        data=task(self.root); data["status"]="accepted"
        with self.assertRaises(AckError): validate_task(data,self.root)
    def test_worker_cannot_accept_result(self):
        with self.assertRaises(AckError): validate_result(result(self.root,"accepted"),"AX-001",self.root)
    def test_result_schema(self): self.assertTrue(validate_result(result(self.root),"AX-001",self.root))
    def test_yaml_datetime_result_is_normalized(self):
        data=result(self.root); data["started_at_utc"]=datetime.now(timezone.utc); data["completed_at_utc"]=datetime.now(timezone.utc)
        checked=validate_result(data,"AX-001",self.root)
        self.assertTrue(checked["started_at_utc"].endswith("Z"))
    def test_bad_result_schema(self):
        data=result(self.root); del data["tests"]
        with self.assertRaises(AckError): validate_result(data,"AX-001",self.root)
    def test_result_agent_is_bound(self):
        with self.assertRaises(AckError): validate_result(result(self.root),"AX-001",self.root,expected_agent="B02")
    def test_unsafe_model_alias_rejected(self):
        data=task(self.root); data["model"]="$(unsafe)"
        with self.assertRaises(AckError): validate_task(data,self.root)
    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap not installed")
    def test_shell_agent_command_rejected(self):
        path=self.root/"config.yaml"; path.write_text("redis_url: redis://example.invalid\nagent_command: [sh, -c, echo]\n")
        with self.assertRaises(AckError): load_config(path)


class ControlTests(unittest.TestCase):
    def setUp(self): self.redis=FakeRedis(); self.cp=ControlPlane(self.redis,"ack"); self.task=task(ROOT); self.token="token-A"
    def test_namespaces_do_not_collide(self): self.assertNotEqual(self.cp.agent_key("A"),ControlPlane(self.redis,"other").agent_key("A"))
    def test_lease_acquisition(self): self.assertTrue(self.cp.acquire_lease("T","token-A",2)); self.assertFalse(self.cp.acquire_lease("T","token-B",2))
    def test_start_is_single_atomic_owner(self):
        self.cp.start(self.task,"A","token-A",10)
        with self.assertRaises(AckError): self.cp.start(self.task,"B","token-B",10)
    def test_parallel_slot_limit(self):
        self.assertEqual(self.cp.acquire_slot("A","token-A",1,10),1); self.assertIsNone(self.cp.acquire_slot("B","token-B",1,10)); self.cp.release_slot("token-A",1); self.assertEqual(self.cp.acquire_slot("B","token-B",1,10),1)
    def test_lease_renewal(self): self.cp.acquire_lease("T","token-A",1); self.cp.renew("T","A","token-A",5); self.assertGreater(self.redis.expiry[self.cp.lease_key("T")],time.time()+4)
    def test_expired_lease(self): self.cp.acquire_lease("T","token-A",1); self.redis.expiry[self.cp.lease_key("T")]=time.time()-1; self.assertTrue(self.cp.acquire_lease("T","token-B",2)); self.assertEqual(self.redis.get(self.cp.lease_key("T")),"token-B")
    def test_heartbeat_updates(self): self.cp.start(self.task,"A",self.token,10); before=self.redis.hgetall(self.cp.agent_key("A"))["heartbeat_at_utc"]; self.cp.heartbeat("AX-001","A",self.token,10); self.assertGreaterEqual(self.redis.hgetall(self.cp.agent_key("A"))["heartbeat_at_utc"],before)
    def test_progress_updates(self): self.cp.start(self.task,"A",self.token,10); self.cp.progress("AX-001","A",self.token,"tests","running"); row=self.redis.hgetall(self.cp.agent_key("A")); self.assertEqual((row["phase"],row["current_action"]),("tests","running"))
    def _event(self,status,expected): self.cp.start(self.task,"A",self.token,10); self.cp.finish("AX-001","A",self.token,status); self.assertEqual(self.redis.streams[self.cp.events_key][-1][1]["event"],expected)
    def test_completion_event(self): self._event("completed","task_completed")
    def test_failure_event(self): self._event("failed","task_failed")
    def test_blocked_event(self): self._event("blocked","task_blocked")
    def test_zombie_cannot_finish(self): self.cp.start(self.task,"A",self.token,10); self.redis.delete(self.cp.lease_key("AX-001")); self.cp.acquire_lease("AX-001","token-B",10); self.assertRaises(AckError,self.cp.finish,"AX-001","A",self.token,"completed")
    def test_stale_classification(self): self.assertEqual(self.cp.health({"status":"working","heartbeat_at_utc":utc_text(utc_now()-timedelta(seconds=100))},45,90),"STALE")
    def test_alive_but_progress_stalled_is_visible(self):
        row={"status":"working","heartbeat_at_utc":utc_text(),"progress_at_utc":utc_text(utc_now()-timedelta(seconds=100))}
        self.assertEqual(self.cp.health(row,45,90),"HEALTHY"); self.assertEqual(self.cp.progress_health(row,90),"ALIVE_BUT_STALLED")
    def test_zombie_cannot_progress(self):
        self.cp.start(self.task,"A",self.token,10); self.redis.delete(self.cp.lease_key("AX-001")); self.cp.acquire_lease("AX-001","token-B",10)
        with self.assertRaises(AckError): self.cp.progress("AX-001","A",self.token,"tests","late")
    def test_reused_display_id_cannot_fence_new_run(self):
        self.cp.start(self.task,"A","old-token",10); self.redis.delete(self.cp.lease_key("AX-001")); self.cp.acquire_lease("AX-001","new-token",10)
        with self.assertRaises(AckError): self.cp.finish("AX-001","A","old-token","completed")
    def test_unsafe_ids_rejected(self):
        with self.assertRaises(AckError): self.cp.task_key("bad:lease")
        with self.assertRaises(AckError): self.cp.agent_key("../agent")
    def test_redis_text_is_bounded(self):
        with self.assertRaises(AckError): self.cp._clean({"current_action":"x"*241})
    def test_status_allowlist_excludes_sensitive_data(self):
        self.assertTrue({"prompt","reasoning","secret","code"}.isdisjoint(STATUS_FIELDS))
        with self.assertRaises(AckError): self.cp._clean({"prompt":"no"})


class SkillTests(unittest.TestCase):
    def test_selective_composition(self):
        text=compose_skills(ROOT,"builder",["python"]); self.assertIn("SKILL: core",text); self.assertIn("SKILL: builder",text); self.assertIn("SKILL: project",text); self.assertIn("SKILL: python",text); self.assertNotIn("SKILL: docker",text)
    def test_missing_skill_fails(self):
        with self.assertRaises(AckError): compose_skills(ROOT,"builder",["missing"])


class GitIsolationTests(unittest.TestCase):
    def test_allocator_uses_independent_objects_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:

            root = Path(temp) / "project"
            root.mkdir()
            (root / ".ack/tasks/active").mkdir(parents=True)

            (root / "PID.md").write_text(f"PROJECT_ROOT: {root}\n")
            (root / "file.txt").write_text("base\n")

            subprocess.run(
                ["git", "init", "-b", "main", str(root)],
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "-C", str(root), "add", "."],
                check=True,
            )

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Axiom",
                    "-c",
                    "user.email=axiom@local",
                    "commit",
                    "-m",
                    "base",
                ],
                check=True,
                capture_output=True,
            )

            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            data = task(root, "write", True)
            data["base_commit"] = base
            data["worktree"] = str(root / ".ack/worktrees/AX-001")

            task_path = root / ".ack/tasks/active/AX-001.yaml"
            task_path.write_text(yaml.safe_dump(data, sort_keys=False))

            worker = allocate_worker_repo(task_path)
            verify_worker_repo(root, worker, data)

            canonical = {
                p.relative_to(root / ".git/objects"): (
                    p.stat().st_dev,
                    p.stat().st_ino,
                )
                for p in (root / ".git/objects").rglob("*")
                if p.is_file()
            }

            for path in (worker / ".git/objects").rglob("*"):

                if (
                    path.is_file()
                    and path.relative_to(worker / ".git/objects") in canonical
                ):
                    self.assertNotEqual(
                        (path.stat().st_dev, path.stat().st_ino),
                        canonical[path.relative_to(worker / ".git/objects")],
                    )

    def test_integrates_valid_completed_worker_commit(self):

        with tempfile.TemporaryDirectory() as temp:

            root = Path(temp) / "project"
            root.mkdir()

            (root / ".ack/tasks/active").mkdir(parents=True)
            (root / ".ack/results").mkdir(parents=True)

            (root / "PID.md").write_text(f"PROJECT_ROOT: {root}\n")
            (root / "file.txt").write_text("base\n")

            subprocess.run(
                ["git", "init", "-b", "main", str(root)],
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "-C", str(root), "add", "."],
                check=True,
            )

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Axiom",
                    "-c",
                    "user.email=axiom@local",
                    "commit",
                    "-m",
                    "base",
                ],
                check=True,
                capture_output=True,
            )

            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            data = task(root, "write", True)
            data["role"] = "builder"
            data["base_commit"] = base
            data["worktree"] = str(root / ".ack/worktrees/AX-001")
            data["scope"] = ["file.txt"]
            data["status"] = "completed"

            task_path = root / ".ack/tasks/active/AX-001.yaml"
            task_path.write_text(
                yaml.safe_dump(data, sort_keys=False)
            )

            worker = allocate_worker_repo(task_path)

            (worker / "file.txt").write_text("base\nworker\n")

            subprocess.run(
                ["git", "-C", str(worker), "add", "file.txt"],
                check=True,
            )

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worker),
                    "-c",
                    "user.name=ACK Worker",
                    "-c",
                    "user.email=ack-worker@localhost",
                    "commit",
                    "-m",
                    "AX-001: worker output",
                ],
                check=True,
                capture_output=True,
            )

            worker_commit = subprocess.run(
                ["git", "-C", str(worker), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            result_data = result(root)
            result_data["changed"] = ["file.txt"]
            result_data["commit"] = worker_commit

            (worker / ".ack/results").mkdir(parents=True)
            (worker / ".ack/results/AX-001.yaml").write_text(
                yaml.safe_dump(result_data, sort_keys=False)
            )

            with self.assertRaisesRegex(AckError, "canonical HEAD moved"):
                integrate_worker_commit(task_path, "0" * 40)

            marker_path = worker / ".git/ack-provenance.json"
            marker = marker_path.read_text()
            marker_path.write_text("{}\n")
            with self.assertRaisesRegex(AckError, "provenance"):
                integrate_worker_commit(task_path, base)
            marker_path.write_text(marker)

            result_data["changed"] = ["other.txt"]
            (worker / ".ack/results/AX-001.yaml").write_text(yaml.safe_dump(result_data, sort_keys=False))
            with self.assertRaisesRegex(AckError, "changed paths"):
                integrate_worker_commit(task_path, base)
            result_data["changed"] = ["file.txt"]
            (worker / ".ack/results/AX-001.yaml").write_text(yaml.safe_dump(result_data, sort_keys=False))

            data["scope"] = []
            task_path.write_text(yaml.safe_dump(data, sort_keys=False))
            with self.assertRaisesRegex(AckError, "outside task scope"):
                integrate_worker_commit(task_path, base)
            data["scope"] = ["file.txt"]
            task_path.write_text(yaml.safe_dump(data, sort_keys=False))

            returned_worker, integrated = integrate_worker_commit(
                task_path,
                base,
            )

            self.assertEqual(returned_worker, worker_commit)

            canonical_head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            self.assertEqual(integrated, canonical_head)
            self.assertNotEqual(integrated, base)

            self.assertEqual(
                (root / "file.txt").read_text(),
                "base\nworker\n",
            )


if __name__ == "__main__":
    unittest.main()
