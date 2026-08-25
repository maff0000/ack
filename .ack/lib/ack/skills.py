from pathlib import Path

import yaml

from .errors import AckError
from .paths import resolve_inside


def compose_skills(root: str | Path, role: str, selected: list[str] | None = None) -> str:
    boundary = Path(root).resolve(strict=True)
    index_path = resolve_inside(boundary, ".ack/skills/INDEX.yaml", must_exist=True)
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    entries = index.get("skills", []) if isinstance(index, dict) else []
    catalogue = {entry["name"]: entry for entry in entries if isinstance(entry, dict) and "name" in entry}
    names = ["core", role, "project", *(selected or [])]
    chunks: list[str] = []
    for name in names:
        entry = catalogue.get(name)
        if not entry or not entry.get("path"):
            raise AckError(f"missing required skill: {name}")
        path = resolve_inside(boundary, entry["path"], must_exist=True)
        chunks.append(f"\n## SKILL: {name}\n\n{path.read_text(encoding='utf-8').strip()}\n")
    return "".join(chunks).lstrip()
