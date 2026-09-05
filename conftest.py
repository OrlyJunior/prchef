from __future__ import annotations

from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Create a configured pytest base-temp parent before pytest's tmpdir plugin.

    Execution worktrees only contain tracked files, so a repository-local
    ``--basetemp`` whose parent is ignored by Git must be created at runtime.
    """
    basetemp = config.getoption("basetemp")
    if basetemp is not None:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)
