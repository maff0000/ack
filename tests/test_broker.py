from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.broker import (
    BrokerOutcomeUnknown,
    BrokerServer,
    BrokerUnavailable,
    TOOL_SCHEMAS,
    _reconcile_outcome,
    broker_call,
    broker_identity,
    bwrap_probe,
    dispatch,
    framework_identity,
    serve_broker,
)
from ack.errors import AckError
from ack.mcp_server import serve
from ack.pl import BrokerProcess, launch


class BrokerTests(unittest.TestCase):
    def project(self, directory: str) -> Path:
        root = Path(directory)
        (root / "PID.md").write_text(f"# Test\n\nPROJECT_ROOT: `{root}`\n", encoding="utf-8")
        (root / "AXIOM.md").write_text("# Test\n", encoding="utf-8")
        (root / ".ack/state").mkdir(parents=True)
        (root / ".ack/state/project.yaml").write_text(yaml.safe_dump({"project_root": str(root)}), encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        return root

    def test_socket_permissions_project_binding_and_forged_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            socket_path = root / ".ack/runtime/broker.sock"
            with BrokerServer(root, socket_path, "first") as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(socket_path.parent.stat().st_mode), 0o700)
                    with self.assertRaisesRegex(AckError, "already active"):
                        BrokerServer(root, socket_path, "second")
                    with self.assertRaisesRegex(AckError, "project root mismatch"):
                        broker_call(socket_path, Path("/forged"), "ack_worker_validate", {"task": "x"})
                    with self.assertRaisesRegex(AckError, "unknown ACK broker operation"):
                        broker_call(socket_path, root, "run_shell", {})
                    with self.assertRaisesRegex(AckError, "schema"):
                        broker_call(socket_path, root, "ack_worker_validate", {"task": "x", "command": "id"})
                    identity = broker_identity(socket_path, root)
                    self.assertEqual(identity["nonce"], "first")
                    self.assertEqual(identity["pid"], os.getpid())
                    self.assertEqual(identity["ack_framework"], framework_identity())
                finally:
                    server.shutdown()
                    thread.join()

    def test_duplicate_launcher_cannot_claim_or_unlink_live_broker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            socket_path = root / ".ack/runtime/broker.sock"
            with BrokerServer(root, socket_path, "first") as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with self.assertRaisesRegex(AckError, "already active"):
                        with BrokerProcess(root):
                            self.fail("duplicate broker unexpectedly started")
                    self.assertTrue(socket_path.is_socket())
                    self.assertEqual(broker_identity(socket_path, root)["nonce"], "first")
                    with self.assertRaisesRegex(AckError, "unknown ACK broker operation"):
                        broker_call(socket_path, root, "not_a_tool", {})
                finally:
                    server.shutdown()
                    thread.join()

    def test_launcher_losing_creation_race_preserves_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            broker_tool = root / ".ack/tools/ack-broker"
            broker_tool.parent.mkdir(parents=True)
            broker_tool.write_text("#!/bin/true\n", encoding="utf-8")
            socket_path = root / ".ack/runtime/broker.sock"
            winner: dict[str, object] = {}
            child = Mock()
            child.pid = 424242
            child.poll.return_value = None
            loser = BrokerProcess(root)

            def start_losing_child(*_: object, **__: object) -> Mock:
                server = BrokerServer(root, socket_path, "winner")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                winner.update(server=server, thread=thread)
                return child

            try:
                with patch("ack.broker.validate_project_root", return_value=root), patch("ack.pl.subprocess.Popen", side_effect=start_losing_child):
                    with self.assertRaisesRegex(AckError, "lost ownership race"):
                        with loser:
                            self.fail("losing broker unexpectedly claimed readiness")
                child.terminate.assert_called_once()
                self.assertTrue(socket_path.is_socket())
                self.assertEqual(broker_identity(socket_path, root)["nonce"], "winner")
                with self.assertRaisesRegex(AckError, "unknown ACK broker operation"):
                    broker_call(socket_path, root, "still_callable", {})
            finally:
                server = winner.get("server")
                thread = winner.get("thread")
                if isinstance(server, BrokerServer):
                    server.shutdown()
                    server.server_close()
                if isinstance(thread, threading.Thread):
                    thread.join()

    def test_owned_broker_shutdown_removes_its_socket(self) -> None:
        root = Path(__file__).resolve().parents[1]
        process = BrokerProcess.__new__(BrokerProcess)
        process.root = root
        with tempfile.TemporaryDirectory() as directory:
            process.socket_path = Path(directory) / "broker.sock"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(process.socket_path))
            endpoint = process.socket_path.stat()
            process.socket_identity = (endpoint.st_dev, endpoint.st_ino)
            process.process = Mock()
            process.process.poll.return_value = 0
            try:
                process.stop()
            finally:
                listener.close()
            self.assertFalse(process.socket_path.exists())

    def test_stale_socket_fails_loud_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            socket_path = root / ".ack/runtime/broker.sock"
            socket_path.parent.mkdir(parents=True)
            stale = socket.socket(socket.AF_UNIX)
            stale.bind(str(socket_path))
            stale.close()
            with self.assertRaisesRegex(AckError, "stale socket"):
                with BrokerProcess(root):
                    self.fail("stale socket unexpectedly accepted")
            self.assertTrue(socket_path.exists())

    def test_mcp_is_proxy_only_and_preserves_tool_surface(self) -> None:
        root = Path(__file__).resolve().parents[1]
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ack_worker_validate", "arguments": {"task": "task.yaml"}}}) + "\n"
        output = io.StringIO()
        with patch.dict(os.environ, {"ACK_PROJECT_ROOT": str(root), "ACK_BROKER_SOCKET": str(root / ".ack/runtime/broker.sock")}, clear=True), patch("ack.mcp_server.broker_call", return_value={"status": "PASS"}) as proxy:
            serve(io.StringIO(request), output)
        proxy.assert_called_once()
        self.assertEqual([tool["name"] for tool in TOOL_SCHEMAS], [
            "ack_worker_validate", "ack_worker_prepare", "ack_worker_run",
            "ack_worker_reconcile", "ack_worker_integrate", "ack_git_commit", "ack_git_push",
        ])

    def test_genuine_broker_unavailable_is_not_outcome_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(BrokerUnavailable):
                broker_call(root / "missing.sock", root, "ack_worker_run", {"task": "task.yaml", "agent": "builder"}, timeout=0.01)

    def test_timeout_after_dispatch_is_outcome_unknown_while_broker_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "broker.sock"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(socket_path))
            listener.listen(1)
            received = threading.Event()

            def serve_one() -> None:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    received.set()
                    time.sleep(0.08)
                    try:
                        connection.sendall(b'{"ok":true,"result":{"status":"PASS"}}\n')
                    except BrokenPipeError:
                        pass
            thread = threading.Thread(target=serve_one)
            thread.start()
            try:
                with self.assertRaises(BrokerOutcomeUnknown) as caught:
                    broker_call(socket_path, root, "ack_worker_run", {"task": "task.yaml", "agent": "builder"}, timeout=0.01)
                self.assertEqual(caught.exception.task, "task.yaml")
                self.assertTrue(received.wait(1))
            finally:
                thread.join(1)
                listener.close()

    def test_mcp_timeout_returns_reconcile_required_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ack_worker_run", "arguments": {"task": "task.yaml", "agent": "builder"}}}) + "\n"
        output = io.StringIO()
        with patch.dict(os.environ, {"ACK_PROJECT_ROOT": str(root), "ACK_BROKER_SOCKET": str(root / ".ack/runtime/broker.sock")}, clear=True), patch(
            "ack.mcp_server.broker_call", side_effect=BrokerOutcomeUnknown("ack_worker_run", "task.yaml")
        ):
            serve(io.StringIO(request), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response["result"]["isError"], False)
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "OUTCOME_UNKNOWN")
        self.assertTrue(payload["reconcile_required"])

    def test_timeout_followed_by_successful_reconciliation(self) -> None:
        self.assertEqual(_reconcile_outcome("completed", {"commit": "worker", "changed": ["MCP-RUN-PROOF.md"]}, {"available": True, "head": "worker", "changed": ["MCP-RUN-PROOF.md"]}), "COMPLETED")

    def test_timeout_followed_by_terminal_failure(self) -> None:
        self.assertEqual(_reconcile_outcome("failed", None, {"available": False}), "FAILED")

    def test_timeout_with_still_unknown_outcome(self) -> None:
        self.assertEqual(_reconcile_outcome("working", None, {"available": False}), "OUTCOME_UNKNOWN")

    def test_timeout_does_not_duplicate_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "broker.sock"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(socket_path))
            listener.listen(1)
            calls = 0

            def serve_one() -> None:
                nonlocal calls
                connection, _ = listener.accept()
                with connection:
                    calls += 1
                    connection.recv(4096)
                    time.sleep(0.05)
            thread = threading.Thread(target=serve_one)
            thread.start()
            try:
                with self.assertRaises(BrokerOutcomeUnknown):
                    broker_call(socket_path, root, "ack_worker_run", {"task": "task.yaml", "agent": "builder"}, timeout=0.01)
            finally:
                thread.join(1)
                listener.close()
            self.assertEqual(calls, 1)

    def test_worker_run_executes_in_broker_and_acceptance_stays_separate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("ack.broker.validate_project_root", return_value=root), patch("ack.broker._task_path", return_value=root / "task.yaml"), patch("ack.broker.load_config", return_value=Mock()), patch("ack.broker.Runner") as runner:
            runner.return_value.run.return_value = 0
            result = dispatch(root, "ack_worker_run", {"task": "task.yaml", "agent": "builder-one"})
        self.assertEqual(result, {"status": "PASS", "exit_code": 0})
        runner.return_value.run.assert_called_once()
        self.assertFalse(any("accept" in tool["name"] for tool in TOOL_SCHEMAS))

        with patch("ack.broker.validate_project_root", return_value=root), patch("ack.broker._task_path", return_value=root / "task.yaml"), patch("ack.broker.integrate_worker_commit", return_value=("worker", "canonical")):
            integrated = dispatch(root, "ack_worker_integrate", {"task": "task.yaml", "expected_canonical_head": "head"})
        self.assertEqual(integrated, {
            "status": "PASS", "worker_commit": "worker",
            "integrated_commit": "canonical", "acceptance_recorded": False,
        })

    def test_fixed_host_bwrap_probe(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("ack.broker.subprocess.run", return_value=completed) as run:
            self.assertEqual(bwrap_probe(), (0, ""))
        run.assert_called_once_with(
            ["bwrap", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "/bin/true"],
            shell=False, capture_output=True, text=True,
        )

    def test_host_probe_red_blocks_broker_start(self) -> None:
        with patch("ack.broker.bwrap_probe", return_value=(1, "namespace denied")):
            with self.assertRaisesRegex(AckError, "host broker bwrap probe failed: namespace denied"):
                serve_broker(Path("/srv/codex/ACK"), Path("/tmp/ack-test-broker.sock"), "nonce")

    def test_host_probe_green_starts_broker_for_managed_control(self) -> None:
        server = MagicMock()
        server.__enter__.return_value = server
        with patch("ack.broker.bwrap_probe", return_value=(0, "")), patch("ack.broker.BrokerServer", return_value=server) as broker:
            serve_broker(Path("/srv/codex/ACK"), Path("/tmp/ack-test-broker.sock"), "nonce")
        broker.assert_called_once_with(Path("/srv/codex/ACK"), Path("/tmp/ack-test-broker.sock"), "nonce")
        server.serve_forever.assert_called_once_with(poll_interval=0.2)

    def test_broker_failure_prevents_codex_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        broker = MagicMock()
        broker.return_value.__enter__.side_effect = AckError("broker failed")
        with patch("ack.pl.BrokerProcess", broker), patch("ack.pl.subprocess.run") as run:
            with self.assertRaisesRegex(AckError, "broker failed"):
                launch(root, ["codex"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
