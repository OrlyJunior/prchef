from pathlib import Path
from typing import Any

import pytest

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitClientError
from orchestrator.clients.git_client import GitClient


class RecordingCommands:
    def __init__(self, *, origin: str = "https://github.com/owner/repository.git") -> None:
        self.calls: list[dict[str, Any]] = []
        self.origin = origin

    def run(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append({"command": command, **kwargs})
        stdout = ""
        if command[1:] == ["branch", "--show-current"]:
            stdout = "feature/test\n"
        elif command[1:] == ["remote", "get-url", "origin"]:
            stdout = f"{self.origin}\n"
        elif command[1:] == ["diff", "--cached", "--name-only"]:
            stdout = "file.py\n"
        elif command[1:] == ["rev-parse", "HEAD"]:
            stdout = "a" * 40
        return CommandResult(command, 0, stdout, "", 0.01)


class AppAuthenticator:
    def token(self) -> str:
        return "ghs_implementer"

    def bot_identity(self) -> tuple[str, str]:
        return "implementer[bot]", "42+implementer[bot]@users.noreply.github.com"


@pytest.mark.parametrize("branch", ["main", "master", "develop", "release"])
def test_protected_branch_is_rejected(branch: str, tmp_path: Path) -> None:
    client = GitClient(CommandClient(), tmp_path)

    with pytest.raises(GitClientError, match="protected branch"):
        client._assert_safe_working_branch(branch, ["release"])


def test_implementer_app_sets_bot_commit_metadata_and_https_push_header(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitClient(commands, tmp_path, implementer_authenticator=AppAuthenticator())

    client.commit_all("feat: test", "feature/test", [])
    client.push("feature/test", [])

    commit = next(call for call in commands.calls if call["command"][1] == "commit")
    assert commit["env"]["GIT_AUTHOR_NAME"] == "implementer[bot]"
    assert (
        commit["env"]["GIT_COMMITTER_EMAIL"]
        == "42+implementer[bot]@users.noreply.github.com"
    )
    push = next(call for call in commands.calls if call["command"][1] == "push")
    assert push["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "ghs_implementer" not in push["env"]["GIT_CONFIG_VALUE_0"]


def test_implementer_app_uses_ambient_transport_for_ssh_push(tmp_path: Path) -> None:
    commands = RecordingCommands(origin="git@github.com:owner/repository.git")
    client = GitClient(commands, tmp_path, implementer_authenticator=AppAuthenticator())

    client.push("feature/test", [])

    push = next(call for call in commands.calls if call["command"][1] == "push")
    assert push["env"] is None


def test_no_implementer_app_preserves_ambient_git_identity(tmp_path: Path) -> None:
    commands = RecordingCommands()
    client = GitClient(commands, tmp_path)

    client.commit_all("feat: test", "feature/test", [])
    client.push("feature/test", [])

    commit = next(call for call in commands.calls if call["command"][1] == "commit")
    push = next(call for call in commands.calls if call["command"][1] == "push")
    assert commit["env"] is None
    assert push["env"] is None

