"""Prepare a disposable copy of global configuration for a preview.

This is a building block, not a complete isolation boundary: callers must also
enforce these constraints on every subsequent configuration load.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def isolated_document(document: Any, source: Path, root: Path) -> dict[str, Any]:
    """Return a copy with repository paths anchored and storage redirected."""
    if not isinstance(document, dict):
        raise ValueError("Configuration root must be a mapping")
    result = deepcopy(document)
    defaults = result.setdefault("defaults", {})
    repositories = result.setdefault("repositories", {})
    if not isinstance(defaults, dict) or not isinstance(repositories, dict):
        raise ValueError("Configuration defaults and repositories must be mappings")

    root = root.resolve()
    defaults.pop("database_url", None)
    defaults.pop("database_path", None)
    persistence = defaults.get("persistence") or {}
    if not isinstance(persistence, dict):
        raise ValueError("Persistence must be a mapping")
    persistence.pop("database_url", None)
    persistence["database_path"] = str(root / "history.db")
    defaults["persistence"] = persistence

    def redirect(entry: dict[str, Any]) -> None:
        workspace = entry.setdefault("workspace", {})
        context = entry.setdefault("repository_context", {})
        if not isinstance(workspace, dict) or not isinstance(context, dict):
            raise ValueError("Workspace and repository context must be mappings")
        workspace["root"] = str(root / "workspaces")
        context["qdrant"] = {"mode": "local", "path": str(root / "qdrant")}

    redirect(defaults)
    for key, entry in repositories.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Repository entry {key!r} must be a mapping")
        if "path" in entry:
            path = Path(str(entry["path"])).expanduser()
            if not path.is_absolute():
                path = source.parent / path
            entry["path"] = str(path.resolve())
        redirect(entry)
    return result


def copy_configuration(source: Path, destination: Path, root: Path) -> None:
    """Write a sanitized YAML copy, leaving the source untouched."""
    document = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    sanitized = isolated_document(document, source, root)
    with destination.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(sanitized, stream, sort_keys=False, allow_unicode=True)
