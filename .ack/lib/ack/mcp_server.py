"""Narrow host-authority MCP bridge for the trusted Axiom Project Lead."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from .broker import BrokerOutcomeUnknown, TOOL_SCHEMAS, broker_call, broker_socket_path, dispatch
from .errors import AckError
from .pl import validate_project_root
from .redact import redact


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def serve(stdin: Any = sys.stdin, stdout: Any = sys.stdout) -> int:
    root_value = os.environ.get("ACK_PROJECT_ROOT")
    socket_value = os.environ.get("ACK_BROKER_SOCKET")
    if not root_value:
        raise AckError("ACK_PROJECT_ROOT is required for the PL MCP bridge")
    if not socket_value:
        raise AckError("ACK_BROKER_SOCKET is required for the PL MCP bridge")
    root = validate_project_root(root_value)
    socket_path = Path(socket_value)
    if socket_path != broker_socket_path(root):
        raise AckError("ACK_BROKER_SOCKET does not match project binding")
    for line in stdin:
        if not line.strip():
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                requested_version = (request.get("params") or {}).get("protocolVersion")
                protocol_version = requested_version if isinstance(requested_version, str) else "2025-06-18"
                response = _result(request_id, {"protocolVersion": protocol_version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "ack-pl", "version": "0.1.1"}})
            elif method == "ping":
                response = _result(request_id, {})
            elif method == "tools/list":
                response = _result(request_id, {"tools": TOOL_SCHEMAS})
            elif method == "tools/call":
                params = request.get("params") or {}
                value = broker_call(socket_path, root, params.get("name", ""), params.get("arguments") or {})
                response = _result(request_id, {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}], "isError": False})
            elif request_id is None:
                continue
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
        except BrokerOutcomeUnknown as exc:
            response = _result(request.get("id"), {"content": [{"type": "text", "text": json.dumps({"status": "OUTCOME_UNKNOWN", "reconcile_required": True, "task": exc.task, "operation": exc.operation, "message": str(exc)}, sort_keys=True)}], "isError": False})
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": redact(f"{type(exc).__name__}: {exc}")}}
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0
