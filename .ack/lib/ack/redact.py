"""Small, deterministic redaction for ACK-controlled diagnostics."""

from __future__ import annotations

import os
import re
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I)
_CREDENTIAL = re.compile(
    r"(?i)(\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_ENV = re.compile(r"(?i)(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL)")


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.password is None and parsed.username is None:
            return value
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, "***@" + hostname + port, parsed.path, parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        return "***"


def configured_secrets(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    return tuple(
        value for name, value in source.items()
        if value and len(value) >= 4 and _SECRET_ENV.search(name)
    )


def redact(value: object, secrets: Iterable[str] | None = None) -> str:
    """Return useful diagnostic text without configured or obvious credentials."""
    text = str(value)
    known = configured_secrets() if secrets is None else tuple(secrets)
    for secret in sorted(set(known), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    text = _URL.sub(lambda match: _redact_url(match.group(0)), text)
    return _CREDENTIAL.sub(r"\1***", text)
