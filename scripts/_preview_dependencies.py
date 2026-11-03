"""Compare dependency declarations without changing the invoking environment."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def _declarations(root: Path) -> tuple[dict[str, Any], bytes]:
    with (root / "pyproject.toml").open("rb") as stream:
        document = tomllib.load(stream)
    project = document["project"]
    build = document["build-system"]
    if not isinstance(project, dict) or not isinstance(build, dict):
        raise ValueError("project and build-system must be tables")
    # Dynamic dependency declarations cannot be compared from this file alone.
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or any(
        name in dynamic for name in ("dependencies", "optional-dependencies", "requires-python")
    ):
        raise ValueError("dynamic dependency declarations cannot be evaluated")
    declarations = {
        "dependencies": project.get("dependencies", []),
        "optional-dependencies": project.get("optional-dependencies", {}),
        "requires-python": project.get("requires-python"),
        "build-requires": build["requires"],
    }
    return declarations, (root / "constraints-dev.txt").read_bytes()


def dependency_warning(control: Path, preview: Path) -> str | None:
    """Return a warning if declarations differ or cannot be compared."""
    try:
        different = _declarations(control) != _declarations(preview)
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        return f"Could not compare dependencies ({exc}); the current environment will be reused."
    if different:
        return "Dependencies differ; the current environment will be reused without installation."
    return None
