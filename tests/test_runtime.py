from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.errors import AckError
from ack.runtime import load_startup_runtime


class StartupRuntimeTests(unittest.TestCase):
    def fixture(self, directory: str, environment: dict[str, object]) -> tuple[Path, Path]:
        root = Path(directory)
        worker_home = root / "worker-home"
        worker_home.mkdir()
        runtime = root / "runtime.yaml"
        runtime.write_text(yaml.safe_dump({"environment": environment}), encoding="utf-8")
        runtime.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return runtime, worker_home

    def test_loads_direct_and_derived_values_without_requiring_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret_source = root / "host.env"
            secret_source.write_text("LITELLM_MASTER_KEY=approved-secret\n", encoding="utf-8")
            runtime, worker_home = self.fixture(directory, {
                "ACK_REDIS_URL": "redis://runtime.invalid/0",
                "ACK_WORKER_CODEX_HOME": str(root / "worker-home"),
                "ACK_API_KEY": {"source": str(secret_source), "key": "LITELLM_MASTER_KEY"},
            })
            with patch.dict(os.environ, {}, clear=True):
                selected = load_startup_runtime(root, runtime)
                self.assertEqual(os.environ["ACK_API_KEY"], "approved-secret")
                self.assertEqual(os.environ["ACK_REDIS_URL"], "redis://runtime.invalid/0")
                self.assertEqual(os.environ["ACK_WORKER_CODEX_HOME"], str(worker_home))
                self.assertEqual(selected, runtime)

    def test_existing_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, _ = self.fixture(directory, {
                "ACK_REDIS_URL": "redis://file.invalid/0",
                "ACK_WORKER_CODEX_HOME": str(root / "worker-home"),
                "ACK_API_KEY": "file-key",
            })
            existing = {
                "ACK_REDIS_URL": "redis://environment.invalid/0",
                "ACK_WORKER_CODEX_HOME": str(root / "worker-home"),
                "ACK_API_KEY": "environment-key",
            }
            with patch.dict(os.environ, existing, clear=True):
                load_startup_runtime(root, runtime)
                self.assertEqual(os.environ["ACK_REDIS_URL"], existing["ACK_REDIS_URL"])
                self.assertEqual(os.environ["ACK_API_KEY"], existing["ACK_API_KEY"])

    def test_fails_loudly_without_required_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, _ = self.fixture(directory, {})
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(AckError, "ACK_REDIS_URL, ACK_WORKER_CODEX_HOME, ACK_API_KEY"):
                    load_startup_runtime(root, runtime)

    def test_rejects_unknown_environment_and_insecure_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, _ = self.fixture(directory, {"UNRELATED_SECRET": "no"})
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(AckError, "unsupported variables"):
                    load_startup_runtime(root, runtime)
            runtime.chmod(0o644)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(AckError, "must not grant group or other permissions"):
                    load_startup_runtime(root, runtime)


if __name__ == "__main__":
    unittest.main()
