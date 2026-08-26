from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.pl import BROKER_ONLY_ENV, MCP_ENV_ALLOWLIST, _mcp_overrides, managed_codex_environment


class McpEnvironmentOverridesTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def overrides(self) -> dict[str, object]:
        arguments = _mcp_overrides(self.root)
        self.assertEqual(arguments[::2], ["-c"] * (len(arguments) // 2))
        return {
            item.split("=", 1)[0]: json.loads(item.split("=", 1)[1])
            for item in arguments[1::2]
        }

    def test_allowlisted_present_variables_are_forwarded(self) -> None:
        self.assertEqual(MCP_ENV_ALLOWLIST, ())
        environment = {"ACK_BROKER_SOCKET": "/forged/socket"}
        with patch.dict(os.environ, environment, clear=True):
            overrides = self.overrides()

        self.assertEqual(
            overrides["mcp_servers.ack_pl.env.ACK_PROJECT_ROOT"],
            str(self.root),
        )
        self.assertNotEqual(overrides["mcp_servers.ack_pl.env.ACK_BROKER_SOCKET"], environment["ACK_BROKER_SOCKET"])

        self.assertNotIn("CODEX_HOME", MCP_ENV_ALLOWLIST)

    def test_worker_secrets_are_absent_from_managed_codex_environment(self) -> None:
        secrets = {name: f"secret-{index}" for index, name in enumerate(BROKER_ONLY_ENV)}
        with patch.dict(os.environ, {**secrets, "PATH": "/bin"}, clear=True):
            environment = managed_codex_environment(self.root)
            arguments = _mcp_overrides(self.root)

        self.assertTrue(BROKER_ONLY_ENV.isdisjoint(environment))
        self.assertNotIn("secret-", repr(arguments))
        self.assertEqual(environment["ACK_BROKER_SOCKET"], str(self.root / ".ack/runtime/broker.sock"))

    def test_pl_codex_home_is_not_forwarded_to_worker_bridge(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "/pl/provider/home"}, clear=True):
            overrides = self.overrides()

        self.assertNotIn("mcp_servers.ack_pl.env.CODEX_HOME", overrides)

    def test_absent_allowlisted_and_unrelated_variables_are_not_forwarded(self) -> None:
        with patch.dict(os.environ, {"UNRELATED_SECRET": "must-not-forward"}, clear=True):
            overrides = self.overrides()

        self.assertEqual(
            overrides["mcp_servers.ack_pl.env.ACK_PROJECT_ROOT"],
            str(self.root),
        )
        self.assertEqual(
            overrides["mcp_servers.ack_pl.env.ACK_BROKER_SOCKET"],
            str(self.root / ".ack/runtime/broker.sock"),
        )
        self.assertNotIn("mcp_servers.ack_pl.env.UNRELATED_SECRET", overrides)
        self.assertNotIn("must-not-forward", repr(overrides))


if __name__ == "__main__":
    unittest.main()
