from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Isolate and create the configured base temp before pytest's tmpdir plugin.

    Execution worktrees only contain tracked files, so a repository-local
    ``--basetemp`` whose parent is ignored by Git must be created at runtime.
    A unique child per process/run prevents concurrent or interrupted Qdrant
    local-mode runs from leaving locks that break every fixture in a later
    run, while retaining the configured path as the stable parent.
    """
    basetemp = config.getoption("basetemp")
    if basetemp is not None:
        configured = Path(basetemp)
        # Keep this compact: Qdrant local mode adds collection paths beneath
        # pytest's per-test directories, and Windows still has deployments
        # constrained by legacy path-length limits.
        isolated = configured / f"p{os.getpid():x}-{uuid4().hex[:10]}"
        isolated.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = isolated
