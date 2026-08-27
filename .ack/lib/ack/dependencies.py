"""Prepare declared project dependencies outside worker repositories."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

def worker_environment_path(root: Path, task_id: str) -> Path:
    return root / ".ack/runtime/worker-env" / task_id


def prepare_worker_environment(root: Path, task_id: str) -> Path | None:
    """Install root requirements into a project-owned runtime outside the worker tree."""
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        return None
    environment = worker_environment_path(root, task_id)
    marker = environment.with_suffix(".requirements.sha256")
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    python = environment / "bin/python"
    if python.is_file() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        return environment
    if environment.exists():
        shutil.rmtree(environment)
    environment.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(requirements)],
        check=True,
        env={**os.environ, "PIP_NO_CACHE_DIR": "1"},
    )
    marker.write_text(digest + "\n", encoding="utf-8")
    return environment
