from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import inspect
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.config import Config
from ack.errors import AckError
from ack.runner import (
    build_worker_command,
    build_worker_prompt,
    execution_task,
    finalize_completed_write,
    _bounded_diagnostic,
    _drain_worker_streams,
    _preserve_failure_session,
    _scoped_git_snapshot,
    _write_worker_diagnostic,
    worker_runtime_profile,
    worker_subprocess_environment,
    worker_template_home,
    Runner,
)
from ack.skills import compose_skills


class WorkerPromptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = {
            "id": "TASK",
            "project": "project",
            "type": "write",
            "role": "builder",
            "model": "trinity-fast",
            "project_root": "/project",
            "base_commit": "base",
            "worktree": ".ack/worktrees/TASK",
            "skills": [],
            "objective": "Create proof.md",
            "scope": ["proof.md"],
            "must_not": [],
            "acceptance": ["proof exists"],
            "dependencies": [],
            "risk": "low",
            "authority": {"mutation_allowed": True, "runtime_mutation_allowed": False},
        }

    def prompt(self) -> str:
        return build_worker_prompt("context", self.task, "result schema", "agent")

    def test_write_prompt_requires_tool_driven_mutation_before_output(self) -> None:
        prompt = self.prompt()

        self.assertIn("primary job is to execute the requested repository mutation using tools", prompt)
        self.assertIn("perform the required scoped filesystem mutation using available tools", prompt)
        self.assertIn("verify the resulting filesystem state", prompt)
        self.assertLess(prompt.index("perform the required scoped filesystem mutation"), prompt.index("Return only the structured result"))

    def test_completed_is_forbidden_without_observed_mutation(self) -> None:
        self.assertIn("`status: completed` is forbidden unless the required scoped filesystem changes actually exist", self.prompt())

    def test_result_is_post_execution_report(self) -> None:
        prompt = self.prompt()

        self.assertIn("Do not return the structured result until execution and verification are finished", prompt)
        self.assertIn("The structured result is a post-execution report only", prompt)

    def test_worker_commit_prohibition_remains_intact(self) -> None:
        prompt = self.prompt()

        self.assertIn("Workers must not run git commit, merge, integrate, accept, or reject", prompt)
        self.assertIn("leave valid scoped changes uncommitted for ACK Runner", prompt)


class WorkerRuntimeSeparationTests(unittest.TestCase):
    def command(self, task_type: str, role: str = "builder") -> list[str]:
        working_dir = Path("/project/worker")
        runtime_home = Path("/project/.ack/worktrees/.runtime-sessions/worker-token")
        config = Config(
            redis_url="redis://unused",
            sandbox_executable="/usr/bin/bwrap",
            agent_command=("codex", "exec", "--sandbox", "{sandbox_mode}"),
        )
        task = {"type": task_type, "role": role, "authority": {"runtime_mutation_allowed": role == "tester"}}
        sandbox_mode, _ = worker_runtime_profile(task, runtime_home)
        return build_worker_command(
            config,
            task,
            working_dir,
            runtime_home,
            {"sandbox_mode": sandbox_mode},
        )

    def test_codex_uses_externally_sandboxed_mode_inside_outer_bwrap(self) -> None:
        command = self.command("write")

        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertIn("--ro-bind", command)
        self.assertEqual(command[-4:], ["codex", "exec", "--sandbox", "danger-full-access"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_outer_bwrap_masks_managed_codex_policy(self) -> None:
        command = self.command("write")
        tmpfs_mounts = [
            command[index:index + 2]
            for index, value in enumerate(command)
            if value == "--tmpfs"
        ]

        self.assertEqual(tmpfs_mounts, [["--tmpfs", "/tmp"], ["--tmpfs", "/etc/codex"]])

    def test_read_worker_has_no_writable_project_bind(self) -> None:
        command = self.command("read")
        working_dir = "/project/worker"
        runtime_home = "/project/.ack/worktrees/.runtime-sessions/worker-token"

        self.assertEqual(command[command.index("--ro-bind"):command.index("--ro-bind") + 3], ["--ro-bind", "/", "/"])
        self.assertEqual(command.count(working_dir), 1)
        self.assertIn(["--bind", runtime_home, runtime_home], [command[index:index + 3] for index in range(len(command) - 2)])

    def test_write_worker_writable_bind_is_its_isolated_repo(self) -> None:
        command = self.command("write")
        binds = [command[index:index + 3] for index, value in enumerate(command) if value == "--bind"]

        self.assertEqual(binds, [
            ["--bind", "/project/.ack/worktrees/.runtime-sessions/worker-token", "/project/.ack/worktrees/.runtime-sessions/worker-token"],
            ["--bind", "/project/worker", "/project/worker"],
        ])

    def test_no_schema_write_invocation_preserves_controls(self) -> None:
        working_dir = Path("/project/worker")
        runtime_home = Path("/project/.ack/worktrees/.runtime-sessions/worker-token")
        config = Config(
            redis_url="redis://unused",
            sandbox_executable="/usr/bin/bwrap",
            agent_command=("codex", "exec", "-C", "{working_dir}", "-m", "{model}", "--sandbox", "{sandbox_mode}"),
        )
        task = {"type": "write", "role": "builder", "authority": {"runtime_mutation_allowed": False}}
        sandbox_mode, _ = worker_runtime_profile(task, runtime_home)

        command = build_worker_command(
            config,
            task,
            working_dir,
            runtime_home,
            {"model": "trinity-fast", "sandbox_mode": sandbox_mode, "working_dir": str(working_dir)},
        )

        self.assertNotIn("--output-schema", command)
        self.assertIn("danger-full-access", command)
        self.assertIn(["--bind", str(working_dir), str(working_dir)], [command[index:index + 3] for index, value in enumerate(command) if value == "--bind"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_write_prompt_is_final_positional_argument_without_stdin_transport(self) -> None:
        working_dir = Path("/project/worker")
        runtime_home = Path("/project/.ack/worktrees/.runtime-sessions/worker-token")
        config = Config(
            redis_url="redis://unused",
            sandbox_executable="/usr/bin/bwrap",
            agent_command=("codex", "exec", "-C", "{working_dir}", "-m", "{model}", "--sandbox", "{sandbox_mode}", "fixed instruction"),
        )
        task = {"type": "write", "role": "builder", "authority": {"runtime_mutation_allowed": False}}
        sandbox_mode, _ = worker_runtime_profile(task, runtime_home)
        prompt = "exact composed prompt"

        command = build_worker_command(
            config,
            task,
            working_dir,
            runtime_home,
            {"model": "trinity-fast", "sandbox_mode": sandbox_mode, "working_dir": str(working_dir)},
            prompt,
        )

        self.assertEqual(command[-1], prompt)
        self.assertNotIn("fixed instruction", command)
        self.assertNotIn("--output-schema", command)

    def test_tester_keeps_disposable_runtime_profile(self) -> None:
        runtime_home = Path("/project/.ack/worktrees/.runtime-sessions/tester-token")
        mode, environment = worker_runtime_profile(
            {"type": "read", "role": "tester", "authority": {"runtime_mutation_allowed": True}},
            runtime_home,
        )

        self.assertEqual(mode, "danger-full-access")
        self.assertEqual(environment, {
            "HOME": str(runtime_home / "home"),
            "XDG_CACHE_HOME": str(runtime_home / "home/.cache"),
            "TMPDIR": "/tmp",
        })

    def test_pl_codex_home_is_not_used_as_worker_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker-template").mkdir()
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(root / "pl-home"),
                    "ACK_WORKER_CODEX_HOME": str(root / "worker-template"),
                },
                clear=True,
            ):
                selected = worker_template_home(root)

        self.assertEqual(selected, root / "worker-template")

    def test_worker_runtime_receives_disposable_codex_home(self) -> None:
        runtime_home = Path("/project/.ack/worktrees/.runtime-sessions/worker-token")
        config = Config(redis_url="redis://unused", agent_env_allowlist=("PATH", "ACK_API_KEY"))
        with patch.dict(
            os.environ,
            {"PATH": "/bin", "ACK_API_KEY": "test-key", "CODEX_HOME": "/pl/home"},
            clear=True,
        ):
            environment = worker_subprocess_environment(config, runtime_home)

        self.assertEqual(environment["CODEX_HOME"], str(runtime_home))
        self.assertEqual(environment["ACK_API_KEY"], "test-key")
        self.assertNotIn("ACK_WORKER_CODEX_HOME", environment)

    def test_missing_worker_template_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CODEX_HOME": "/pl/home"}, clear=True):
                with self.assertRaisesRegex(
                    AckError,
                    "ACK_WORKER_CODEX_HOME provider template is required",
                ):
                    worker_template_home(Path(directory))


class CompletedWriteFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed"],
            check=True,
            capture_output=True,
        )
        self.base = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(self.root), "switch", "-c", "ack/TASK/worker"], check=True, capture_output=True)
        self.result_file = self.root / ".ack/results/TASK.yaml"
        self.task = {
            "id": "TASK",
            "base_commit": self.base,
            "scope": ["proof.md"],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def result(self, *, commit: str = "worker-claim", changed: list[str] | None = None) -> dict[str, object]:
        return {
            "id": "TASK",
            "agent_instance": "builder-test",
            "status": "completed",
            "summary": "completed",
            "changed": ["claimed.md"] if changed is None else changed,
            "commit": commit,
            "tests": {"commands": [], "passed": 0, "failed": 0},
            "findings": [],
            "risks": [],
            "blockers": [],
            "evidence": [],
            "started_at_utc": "2026-08-26T00:00:00Z",
            "completed_at_utc": "2026-08-26T00:00:01Z",
        }

    def finalize(self, result: dict[str, object]) -> dict[str, object]:
        return finalize_completed_write(
            self.task,
            self.root,
            "ack/TASK/worker",
            result,
            self.result_file,
            "builder-test",
            self.root,
        )

    def test_zero_change_completed_output_fails_non_vacuity(self) -> None:
        with self.assertRaisesRegex(AckError, "must produce at least one scoped change"):
            self.finalize(self.result())

        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_misleading_base_commit_claim_is_not_trusted(self) -> None:
        (self.root / "proof.md").write_text("proof\n", encoding="utf-8")

        result = self.finalize(self.result(commit=self.base, changed=[]))

        self.assertNotEqual(result["commit"], self.base)
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["changed"], ["proof.md"])

    def test_successful_scoped_mutation_is_exactly_one_commit(self) -> None:
        (self.root / "proof.md").write_text("proof\n", encoding="utf-8")

        result = self.finalize(self.result())

        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertEqual(self.git("diff", "--name-only", f"{self.base}..HEAD"), "proof.md")
        self.assertEqual(result["changed"], ["proof.md"])
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))

    def test_worker_created_commit_is_rejected_before_controller_commit(self) -> None:
        (self.root / "proof.md").write_text("proof\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "proof.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Worker", "-c", "user.email=worker@example.invalid", "commit", "-m", "worker commit"],
            check=True,
            capture_output=True,
        )
        worker_head = self.git("rev-parse", "HEAD")

        with self.assertRaisesRegex(AckError, "worker-created Git commits are prohibited"):
            self.finalize(self.result(commit=worker_head, changed=["proof.md"]))

        self.assertEqual(self.git("rev-parse", "HEAD"), worker_head)
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertFalse(self.result_file.exists())

    def test_rejected_completion_does_not_publish_false_result(self) -> None:
        self.result_file.parent.mkdir(parents=True)
        self.result_file.write_text("status: completed\n", encoding="utf-8")

        with self.assertRaises(AckError):
            self.finalize(self.result(commit=self.base, changed=[]))

        self.assertFalse(self.result_file.exists())


class WorkerInstructionContractTests(unittest.TestCase):
    def task(self, **extra: object) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "TASK",
            "project": "PROJECT",
            "type": "write",
            "role": "builder",
            "model": "trinity-fast",
            "project_root": "/project",
            "base_commit": "base",
            "worktree": ".ack/worktrees/TASK",
            "skills": [],
            "objective": "Create proof.md",
            "scope": ["proof.md"],
            "must_not": ["Commit work"],
            "acceptance": ["proof.md exists"],
            "dependencies": [],
            "risk": "low",
            "authority": {"mutation_allowed": True, "runtime_mutation_allowed": False},
            "status": "rejected",
            "decision": {"decided_by": "Axiom", "reason": "prior verdict"},
        }
        task.update(extra)
        return task

    def test_builder_context_does_not_instruct_worker_to_commit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        context = compose_skills(root, "builder", [])

        self.assertNotIn("commit focused work", context.lower())
        self.assertIn("never run `git commit`", context.lower())
        self.assertIn("ack runner/controller alone creates", context.lower())

    def test_write_prompt_requires_actual_mutation_before_completed(self) -> None:
        prompt = build_worker_prompt("context", self.task(), "result template", "builder-one")

        self.assertIn("primary job is to execute the requested repository mutation using tools", prompt)
        self.assertIn("`status: completed` is forbidden unless the required scoped filesystem changes actually exist", prompt)
        self.assertIn("do not merely repeat them as facts", prompt)
        self.assertIn("Workers must not run git commit, merge, integrate, accept, or reject", prompt)
        self.assertIn("The structured result is a post-execution report only", prompt)

    def test_archived_adjudication_metadata_is_not_in_execution_assignment(self) -> None:
        assignment = execution_task(self.task())
        prompt = build_worker_prompt("context", self.task(), "result template", "builder-one")

        self.assertNotIn("status", assignment)
        self.assertNotIn("decision", assignment)
        self.assertNotIn("prior verdict", prompt)
        self.assertNotIn("decided_by", prompt)


class WorkerDiagnosticTests(unittest.TestCase):
    def test_oversized_stderr_is_drained_without_terminating_child(self) -> None:
        marker = b"terminal-tail-marker"
        script = (
            "import sys; "
            "sys.stderr.buffer.write(b'x' * (1024 * 1024 + 123)); "
            f"sys.stderr.buffer.write({marker!r}); sys.stderr.flush()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr, stdout_thread, stderr_thread = _drain_worker_streams(process)
        self.assertEqual(process.wait(timeout=10), 0)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()

        self.assertFalse(stdout_thread.is_alive())
        self.assertFalse(stderr_thread.is_alive())
        self.assertEqual(stdout.stats(), {"total_bytes": 0, "retained_bytes": 0, "truncated": False})
        self.assertEqual(stderr.total_bytes, 1024 * 1024 + 123 + len(marker))
        self.assertEqual(stderr.stats()["retained_bytes"], 16_384)
        self.assertTrue(stderr.stats()["truncated"])
        self.assertTrue(stderr.text().endswith(marker.decode()))

    def test_small_worker_streams_and_structured_result_are_unchanged(self) -> None:
        result = b"status: completed\nsummary: ok\n"
        process = subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({result!r}); sys.stderr.write('diagnostic')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr, stdout_thread, stderr_thread = _drain_worker_streams(process)
        self.assertEqual(process.wait(timeout=10), 0)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(stdout.text(), result.decode())
        self.assertEqual(stderr.text(), "diagnostic")
        self.assertEqual(stdout.stats(), {"total_bytes": len(result), "retained_bytes": len(result), "truncated": False})
        self.assertEqual(stderr.stats(), {"total_bytes": 10, "retained_bytes": 10, "truncated": False})

    def test_lease_loss_termination_remains_governed(self) -> None:
        source = inspect.getsource(Runner.run)
        self.assertIn("if stop.wait(1) and lost:", source)
        self.assertIn("process.terminate()", source)
        self.assertIn("worker lease lost", source)

    def test_diagnostic_output_is_redacted_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".ack/runtime/diagnostics/attempt.yaml"
            with patch.dict(os.environ, {"ACK_REDIS_URL": "redis://:super-secret@localhost/0"}):
                _write_worker_diagnostic(path, {"stdout": _bounded_diagnostic("redis://:super-secret@localhost/0 " + "x" * 20000)})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("super-secret", text)
            self.assertIn("...[truncated]", text)

    def test_failure_session_is_preserved_redacted_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime-home"
            trace = runtime / "sessions/2026/08/26/rollout.jsonl"
            trace.parent.mkdir(parents=True)
            trace.write_text(
                '{"type":"session_meta","payload":{"session_id":"session-1"}}\n'
                '{"type":"response_item","payload":{"arguments":"api_key=super-secret"}}\n'
                + "x" * 20_000,
                encoding="utf-8",
            )
            diagnostic = root / "diagnostics/attempt.yaml"
            with patch.dict(os.environ, {"ACK_API_KEY": "super-secret"}):
                metadata = _preserve_failure_session(runtime, diagnostic, "stdout", "stderr")

            preserved = Path(metadata["trace_path"])
            self.assertEqual(metadata["session_id"], "session-1")
            self.assertEqual(preserved.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(preserved.stat().st_size, 1_048_576)
            self.assertNotIn("super-secret", preserved.read_text(encoding="utf-8"))
            self.assertEqual(Path(metadata["stdout_path"]).read_text(encoding="utf-8"), "stdout")
            self.assertEqual(Path(metadata["stderr_path"]).read_text(encoding="utf-8"), "stderr")
            self.assertEqual(Path(metadata["stdout_path"]).stat().st_mode & 0o777, 0o600)

    def test_scoped_git_snapshot_observes_only_allowed_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "proof.md").write_text("proof\n", encoding="utf-8")
            task = {"scope": ["proof.md"]}
            self.assertTrue(_scoped_git_snapshot(root, task))
            (root / "proof.md").unlink()
            (root / "other.txt").write_text("other\n", encoding="utf-8")
            self.assertFalse(_scoped_git_snapshot(root, task))


if __name__ == "__main__":
    unittest.main()
