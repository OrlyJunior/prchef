from pathlib import Path

import pytest

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.exceptions import GitClientError
from orchestrator.clients.git_client import GitClient


@pytest.mark.parametrize("branch", ["main", "master", "develop", "release"])
def test_protected_branch_is_rejected(branch: str, tmp_path: Path) -> None:
    client = GitClient(CommandClient(), tmp_path)

    with pytest.raises(GitClientError, match="protected branch"):
        client._assert_safe_working_branch(branch, ["release"])

