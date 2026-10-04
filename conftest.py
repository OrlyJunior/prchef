from __future__ import annotations

import os
from hashlib import sha256
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


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep per-test paths compact enough for nested worktrees and local Qdrant.

    Pytest's default test-name prefix is repeated in isolated user homes and
    workflow workspace paths. Even with a short run directory, those paths can
    exceed Windows' legacy path limit before Qdrant or a nested pytest run adds
    its own directories. A stable node-id digest keeps paths identifiable while
    the factory's numbered suffix still isolates repeated requests.
    """
    prefix = "t" + sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    return tmp_path_factory.mktemp(prefix, numbered=True)
