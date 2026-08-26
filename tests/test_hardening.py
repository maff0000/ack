from pathlib import Path
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / ".ack/lib"))

from ack.errors import AckError
from ack.pl import RESUME_INSTRUCTION, bootstrap_project, build_pl_command, build_resume_command, launch, preflight, validate_project_root
from ack.mcp_server import TOOL_SCHEMAS, dispatch, serve
from ack.redact import redact
from ack.runner import remove_runtime_home, worker_runtime_profile


def bubblewrap_usable() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def make_project(path: Path, pid_root: Path | None = None) -> Path:
    path.mkdir()
    (path / ".ack/state").mkdir(parents=True)
    (path / "AXIOM.md").write_text("doctrine\n")
    (path / "PID.md").write_text(f"PROJECT_ROOT: `{pid_root or path}`\n")
    (path / ".ack/state/project.yaml").write_text(f"project: test\nproject_root: {path}\n")
    shutil.copytree(ROOT / ".ack/lib", path / ".ack/lib")
    (path / ".ack/tools").mkdir()
    shutil.copy2(ROOT / ".ack/tools/ack-broker", path / ".ack/tools/ack-broker")
    shutil.copy2(ROOT / ".ack/tools/ack-pl-mcp", path / ".ack/tools/ack-pl-mcp")
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    (path / "tracked").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-m", "base"], check=True, capture_output=True)
    return path


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def test_matching_root_accepted_from_parent_cwd(self):
        root = make_project(self.base / "project")
        old = Path.cwd()
        try:
            os.chdir(self.base)
            self.assertEqual(validate_project_root(str(root)), root)
        finally:
            os.chdir(old)
    def test_pid_mismatch_rejected(self):
        other = self.base / "other"
        other.mkdir()
        root = make_project(self.base / "project", other)
        with self.assertRaisesRegex(AckError, "mismatch"):
            validate_project_root(root)
    def test_symlink_alias_rejected(self):
        root = make_project(self.base / "project")
        alias = self.base / "alias"
        alias.symlink_to(root)
        with self.assertRaisesRegex(AckError, "symlink"):
            validate_project_root(alias)
    def test_nonexistent_relative_and_dotdot_rejected(self):
        values = ("relative", str(self.base / "missing"), str(self.base / "x" / ".." / "project"))
        for value in values:
            with self.subTest(value=value), self.assertRaises(AckError):
                validate_project_root(value)
    def test_trusted_pl_command_uses_managed_codex_and_narrow_mcp(self):
        root = make_project(self.base / "project")
        command = build_pl_command(root, sys.executable)
        self.assertEqual(command[:3], [sys.executable, "-C", str(root)])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertTrue(any("mcp_servers.ack_pl.command" in item for item in command))
        self.assertTrue(any("mcp_servers.ack_pl.required=true" in item for item in command))
        with self.assertRaisesRegex(AckError, "not nested axel"):
            build_pl_command(root, "/usr/local/bin/axel")
    def test_launch_exports_pid_root(self):
        root = make_project(self.base / "project")
        old = Path.cwd()
        try:
            broker = unittest.mock.MagicMock()
            with patch("ack.pl.BrokerProcess", broker), patch("ack.pl.subprocess.run") as execute:
                launch(root, [sys.executable])
            self.assertEqual(execute.call_args.kwargs["env"]["ACK_PROJECT_ROOT"], str(root))
            self.assertEqual(Path.cwd(), root)
        finally:
            os.chdir(old)
    def test_project_state_root_mismatch_rejected(self):
        root = make_project(self.base / "project")
        (root / ".ack/state/project.yaml").write_text(f"project: test\nproject_root: {self.base}\n")
        with self.assertRaisesRegex(AckError, "state root"):
            validate_project_root(root)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.temp.name) / "project")
    def tearDown(self): self.temp.cleanup()
    def test_probes_cleanup_and_do_not_commit(self):
        before = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout
        ready, lines = preflight(self.root, self.root / ".ack/missing-config.yaml")
        text = "\n".join(lines)
        self.assertIn("PROJECT_WRITE    OK", text)
        self.assertIn("GIT_METADATA     OK", text)
        self.assertFalse(any(self.root.glob(".ack/.ack-preflight-*")))
        self.assertFalse(any((self.root / ".git").glob("ack-preflight-*")))
        after = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout
        self.assertEqual(before, after)
        self.assertFalse(ready)
    def test_failed_capability_reports_observed_operation(self):
        with patch("ack.pl.tempfile.NamedTemporaryFile", side_effect=PermissionError("denied by test")):
            ready, lines = preflight(self.root, self.root / ".ack/missing-config.yaml")
        text = "\n".join(lines)
        self.assertFalse(ready)
        self.assertIn("PROJECT_WRITE    FAIL PermissionError: denied by test", text)
        real_run = subprocess.run
        def fail_git_write(argv, *args, **kwargs):
            if "hash-object" in argv and "-w" in argv:
                raise PermissionError("git objects denied by test")
            return real_run(argv, *args, **kwargs)
        with patch("ack.pl.subprocess.run", side_effect=fail_git_write):
            ready, lines = preflight(self.root, self.root / ".ack/missing-config.yaml")
        self.assertFalse(ready)
        self.assertIn("GIT_METADATA     FAIL PermissionError: git objects denied by test", "\n".join(lines))
    def test_resume_allows_redis_only_degradation(self):
        config = self.root / ".ack/config.yaml"
        config.write_text(f"redis_url: redis://127.0.0.1:1/0\nsandbox_executable: bwrap\nagent_command: [{sys.executable}]\n")
        real_run = subprocess.run
        def usable_bwrap(argv, *args, **kwargs):
            if argv and Path(argv[0]).name == "bwrap":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, *args, **kwargs)
        client = unittest.mock.Mock()
        client.ping.side_effect = ConnectionError("unavailable")
        with patch("ack.pl.subprocess.run", side_effect=usable_bwrap), patch("ack.pl.redis.Redis.from_url", return_value=client):
            ready, lines = preflight(self.root, config, allow_redis_degraded=True)
        text = "\n".join(lines)
        self.assertTrue(ready, text)
        self.assertIn("REDIS            DEGRADED ConnectionError", text)
        self.assertIn("STATUS           READY DEGRADED", text)


class McpBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.temp.name) / "project")
        (self.root / ".ack/tasks/active").mkdir(parents=True)
    def tearDown(self): self.temp.cleanup()
    def test_surface_is_narrow_and_has_no_acceptance_or_shell(self):
        names = {tool["name"] for tool in TOOL_SCHEMAS}
        self.assertEqual(names, {
            "ack_worker_validate", "ack_worker_prepare", "ack_worker_run",
            "ack_worker_reconcile",
            "ack_worker_integrate", "ack_git_commit", "ack_git_push",
        })
        self.assertFalse(any("shell" in name or "accept" in name or "reject" in name for name in names))
    def test_stdio_handshake_lists_only_ack_tools(self):
        source = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        target = io.StringIO()
        with patch.dict(os.environ, {"ACK_PROJECT_ROOT": str(self.root), "ACK_BROKER_SOCKET": str(self.root / ".ack/runtime/broker.sock")}):
            self.assertEqual(serve(source, target), 0)
        replies = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "ack-pl")
        self.assertEqual(
            {tool["name"] for tool in replies[1]["result"]["tools"]},
            {tool["name"] for tool in TOOL_SCHEMAS},
        )
    def test_worker_validation_reuses_task_contract_and_root(self):
        data = {
            "id": "AX-MCP-001", "project": "test", "type": "read", "role": "scout",
            "model": "trinity-fast", "project_root": str(self.root), "base_commit": "abc",
            "worktree": "", "skills": [], "objective": "inspect", "scope": [],
            "must_not": [], "acceptance": [], "dependencies": [], "risk": "low",
            "authority": {"mutation_allowed": False, "runtime_mutation_allowed": False},
            "status": "queued",
        }
        task_path = self.root / ".ack/tasks/active/AX-MCP-001.yaml"
        import yaml
        task_path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertEqual(dispatch(self.root, "ack_worker_validate", {"task": ".ack/tasks/active/AX-MCP-001.yaml"})["status"], "PASS")
        with self.assertRaisesRegex(AckError, "under .ack/tasks/active"):
            dispatch(self.root, "ack_worker_validate", {"task": "PID.md"})


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "PID.md").write_text(f"# Project\n\n- **PROJECT_ROOT:** `{self.root}`\n")
    def tearDown(self): self.temp.cleanup()
    def test_approved_pid_is_mechanically_adopted(self):
        adopted = bootstrap_project(self.root)
        self.assertEqual(adopted, self.root)
        self.assertTrue((self.root / "AXIOM.md").is_file())
        self.assertTrue((self.root / ".ack/tools/ack-pl").is_file())
        self.assertTrue((self.root / ".ack/requirements.txt").is_file())
        self.assertIn(f"project_root: {self.root}", (self.root / ".ack/state/project.yaml").read_text())
        self.assertIn("PID.md", (self.root / ".ack/skills/project/PROJECT.md").read_text())
        self.assertIn(".ack/config.yaml", (self.root / ".gitignore").read_text())
    def test_existing_project_truth_is_not_overwritten(self):
        (self.root / "AXIOM.md").write_text("project-owned\n")
        with self.assertRaisesRegex(AckError, "refuses to overwrite"):
            bootstrap_project(self.root)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / ".ack").exists())


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = make_project(self.base / "project")
        (self.root / ".ack/skills/project").mkdir(parents=True)
        (self.root / ".ack/skills/project/PROJECT.md").write_text("existing truth\n")
    def tearDown(self): self.temp.cleanup()
    def test_valid_existing_project_builds_trusted_resume(self):
        root = validate_project_root(self.root)
        command = build_resume_command(root, sys.executable)
        self.assertEqual(command[:3], [sys.executable, "-C", str(root)])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[-1], RESUME_INSTRUCTION)
    def test_resume_root_mismatch_rejected(self):
        (self.root / ".ack/state/project.yaml").write_text(f"project: test\nproject_root: {self.base}\n")
        with self.assertRaisesRegex(AckError, "state root"):
            validate_project_root(self.root)
    def test_resume_requires_pid_and_state(self):
        (self.root / "PID.md").unlink()
        with self.assertRaisesRegex(AckError, "PID.md"):
            validate_project_root(self.root)
        (self.root / "PID.md").write_text(f"PROJECT_ROOT: `{self.root}`\n")
        (self.root / ".ack/state/project.yaml").unlink()
        with self.assertRaisesRegex(AckError, "project state"):
            validate_project_root(self.root)
    def test_resume_does_not_overwrite_project_truth(self):
        paths = [self.root / "PID.md", self.root / ".ack/state/project.yaml", self.root / ".ack/skills/project/PROJECT.md"]
        before = {path: path.read_bytes() for path in paths}
        build_resume_command(validate_project_root(self.root), sys.executable)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
    def test_recovery_instruction_uses_durable_sources_without_chat(self):
        for source in ("PID.md", "AXIOM.md", ".ack/state/project.yaml", "Git status and history", "active and archived tasks", "results and evidence", "relevant ADRs", "Redis live state and events"):
            self.assertIn(source, RESUME_INSTRUCTION)
        self.assertIn("Do not require or rely on prior chat history", RESUME_INSTRUCTION)
        self.assertIn("expired or stale leases", RESUME_INSTRUCTION)
        self.assertIn("Only Axiom may accept or reject", RESUME_INSTRUCTION)


class RedactionTests(unittest.TestCase):
    def test_url_credentials_redacted(self):
        safe = redact("redis://user:secret@host:6379/0 https://alice:hunter2@example.test/path", ())
        self.assertNotIn("secret", safe)
        self.assertNotIn("hunter2", safe)
        self.assertIn("host:6379/0", safe)
        self.assertIn("example.test/path", safe)
    def test_environment_token_redacted_endpoint_useful(self):
        safe = redact("token-value at redis://host:6379/0", ("token-value",))
        self.assertNotIn("token-value", safe)
        self.assertIn("redis://host:6379/0", safe)
    def test_config_credential_redacted(self):
        self.assertEqual(redact("api_key=very-secret", ()), "api_key=***")


class StaticProfileTests(unittest.TestCase):
    def test_tester_runtime_authority_enables_disposable_execution(self):
        runtime = Path("/runtime/session")
        task = {"type": "read", "role": "tester", "authority": {"runtime_mutation_allowed": True}}
        native, env = worker_runtime_profile(task, runtime)
        self.assertEqual(native, "danger-full-access")
        self.assertEqual(env["HOME"], "/runtime/session/home")
        self.assertEqual(env["XDG_CACHE_HOME"], "/runtime/session/home/.cache")
        self.assertEqual(env["TMPDIR"], "/tmp")
    def test_tester_without_runtime_authority_remains_read_only(self):
        task = {"type": "read", "role": "tester", "authority": {"runtime_mutation_allowed": False}}
        native, _ = worker_runtime_profile(task, Path("/runtime"))
        self.assertEqual(native, "danger-full-access")
    def test_builder_semantics_are_unchanged(self):
        task = {"type": "write", "role": "builder", "authority": {"runtime_mutation_allowed": False}}
        native, env = worker_runtime_profile(task, Path("/runtime"))
        self.assertEqual(native, "danger-full-access")
        self.assertEqual(env, {})
    def test_disposable_runtime_cleanup_and_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            runtime.mkdir()
            (runtime / "cache").write_text("ephemeral")
            remove_runtime_home(runtime)
            self.assertFalse(runtime.exists())
        with patch("ack.runner.shutil.rmtree", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(AckError, "cleanup failed: PermissionError"):
                remove_runtime_home(Path("/runtime/session"))


@unittest.skipUnless(bubblewrap_usable(), "bubblewrap namespaces unavailable")
class TesterProfileTests(unittest.TestCase):
    def test_source_git_immutable_but_runtime_executes(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as temp:
            root = make_project(Path(temp) / "project")
            script = "! touch tracked && ! touch .git/nope && mkdir -p /tmp/home/.cache && HOME=/tmp/home python3 -c 'import pathlib; pathlib.Path(\"/tmp/result\").write_text(\"ok\")' && test -f /tmp/result"
            command = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--chdir", str(root), "sh", "-c", script]
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
    def test_builder_writes_clone_not_canonical(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as temp:
            canonical = make_project(Path(temp) / "canonical")
            worker = Path(temp) / "worker"
            subprocess.run(["git", "clone", "--no-hardlinks", str(canonical), str(worker)], check=True, capture_output=True)
            script = f"touch {worker}/built && ! touch {canonical}/forbidden"
            command = ["bwrap", "--ro-bind", "/", "/", "--bind", str(worker), str(worker), "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "sh", "-c", script]
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__": unittest.main()
