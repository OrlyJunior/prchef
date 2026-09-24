import subprocess
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


def test_verify_worktree_branch_rejects_a_changed_recorded_head(tmp_path: Path) -> None:
    class ChangedHeadCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            result = super().run(command, **kwargs)
            if command[1:] == ["rev-parse", "HEAD"]:
                return CommandResult(command, 0, "b" * 40, "", 0.01)
            return result

    client = GitClient(ChangedHeadCommands(), tmp_path)

    with pytest.raises(GitClientError, match="last recorded commit"):
        client.verify_worktree_branch("feature/test", "a" * 40)


def test_merge_base_distinguishes_disconnected_history_from_missing_ref(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str, cwd: Path = repository) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )

    git("init", "-b", "develop")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repository / "base.txt").write_text("base", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-m", "base")
    worktree = tmp_path / "worktree"
    git("worktree", "add", "-b", "feature/test", str(worktree))
    client = GitClient(CommandClient(), worktree)

    (repository / "base.txt").write_text("advanced", encoding="utf-8")
    git("commit", "-am", "advance develop")
    client.ensure_merge_base("develop")
    client.diff("develop")

    with pytest.raises(GitClientError, match="merge-base missing HEAD failed"):
        client.ensure_merge_base("missing")

    git("switch", "--orphan", "rewritten")
    (repository / "new-root.txt").write_text("new history", encoding="utf-8")
    git("add", "new-root.txt")
    git("commit", "-m", "new root")
    git("branch", "-f", "develop", "HEAD")

    with pytest.raises(GitClientError) as error:
        client.diff("develop")
    assert "disconnected history" in str(error.value)
    assert "develop" in str(error.value)
    assert "filter-repo" in str(error.value)
    assert "filter-branch" in str(error.value)
    assert "rebase --root" in str(error.value)


def test_merge_base_preserves_other_git_failure(tmp_path: Path) -> None:
    class FailingCommands(RecordingCommands):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            if command[1] == "merge-base":
                return CommandResult(command, 128, "", "repository unavailable", 0.01)
            return super().run(command, **kwargs)

    client = GitClient(FailingCommands(), tmp_path)

    with pytest.raises(GitClientError, match="repository unavailable") as error:
        client.ensure_merge_base("develop")
    assert "disconnected history" not in str(error.value)

