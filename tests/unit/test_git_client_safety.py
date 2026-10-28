import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from prchef.clients import github_client
from prchef.clients.command_client import CommandClient, CommandResult
from prchef.clients.exceptions import GitClientError
from prchef.clients.git_client import GitClient
from prchef.clients.github_client import GitHubAppAuthenticator
from prchef.config.project_config import GitHubAppSettings
from prchef.models.development_task import DevelopmentTask
from prchef.services.repository_context import (
    HeuristicRepositoryContextStrategy,
    RepositoryContextRequest,
)


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


@pytest.mark.parametrize(("lookup_status", "expected_id"), [(200, 987), (403, 123)])
def test_commit_uses_real_app_identity_with_lookup_or_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup_status: int, expected_id: int,
) -> None:
    monkeypatch.setattr(github_client, "_BOT_USER_IDS", {})
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_client.jwt, "encode", lambda *args, **kwargs: "jwt")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"slug": "implementer", "id": 123})
        if request.method == "POST":
            return httpx.Response(201, json={
                "token": "token", "expires_at": "2030-01-01T01:00:00Z",
            })
        return httpx.Response(lookup_status, json={"id": 987})

    auth = GitHubAppAuthenticator(
        GitHubAppSettings(
            app_id=123, installation_id=456, private_key_env="GITHUB_APP_PRIVATE_KEY",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    commands = RecordingCommands()
    GitClient(commands, tmp_path, implementer_authenticator=auth).commit_all(
        "feat: test", "feature/test", []
    )
    commit = next(call for call in commands.calls if call["command"][1] == "commit")
    for role in ("AUTHOR", "COMMITTER"):
        assert commit["env"][f"GIT_{role}_NAME"] == "implementer[bot]"
        assert commit["env"][f"GIT_{role}_EMAIL"] == (
            f"{expected_id}+implementer[bot]@users.noreply.github.com"
        )


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


@pytest.mark.parametrize("local_changes", ["behind", "ahead-and-dirty"])
def test_fetched_base_is_shared_by_planning_worktree_and_diff(
    tmp_path: Path, local_changes: str
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    control = tmp_path / "control"

    def git(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Test", cwd=seed)
    git("config", "user.email", "test@example.com", cwd=seed)
    (seed / "base.txt").write_text("old", encoding="utf-8")
    git("add", "base.txt", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    git("clone", "-b", "main", str(remote), str(control))

    (seed / "base.txt").write_text("remote only", encoding="utf-8")
    git("commit", "-am", "remote update", cwd=seed)
    git("push", cwd=seed)
    if local_changes == "ahead-and-dirty":
        git("config", "user.name", "Test", cwd=control)
        git("config", "user.email", "test@example.com", cwd=control)
        (control / "local.txt").write_text("local commit", encoding="utf-8")
        git("add", "local.txt", cwd=control)
        git("commit", "-m", "local advance", cwd=control)
        (control / "base.txt").write_text("dirty", encoding="utf-8")
    original_status = git("status", "--porcelain", cwd=control)
    original_head = git("rev-parse", "HEAD", cwd=control)

    client = GitClient(CommandClient(), control)
    base_commit = client.fetch_base("main")
    assert base_commit == git("rev-parse", "HEAD", cwd=seed)
    with client.planning_snapshot(base_commit) as snapshot:
        assert (snapshot / "base.txt").read_text(encoding="utf-8") == "remote only"
        assert not (snapshot / "local.txt").exists()
        context = HeuristicRepositoryContextStrategy().render(
            RepositoryContextRequest(
                role="planner",
                repository_path=snapshot,
                task=DevelopmentTask(
                    id="1", source="github", title="remote only", description="remote only", url=""
                ),
                repository_slug="owner/repo",
                base_branch="main",
            )
        )
        assert "remote only" in context
        assert "dirty" not in context
    worktree = tmp_path / "feature"
    client.create_worktree(worktree, "main", "feature/test", [], base_commit)
    assert git("rev-parse", "HEAD", cwd=worktree) == base_commit
    git("config", "user.name", "Test", cwd=worktree)
    git("config", "user.email", "test@example.com", cwd=worktree)
    (worktree / "feature.txt").write_text("feature", encoding="utf-8")
    git("add", "feature.txt", cwd=worktree)
    git("commit", "-m", "feature", cwd=worktree)

    (seed / "later.txt").write_text("later", encoding="utf-8")
    git("add", "later.txt", cwd=seed)
    git("commit", "-m", "later base update", cwd=seed)
    git("push", cwd=seed)
    # Even a later remote-tracking update cannot change this execution's comparison.
    git("fetch", "origin", "main", cwd=control)
    diff = client.at_path(worktree).diff(base_commit)
    assert "feature.txt" in diff
    assert "base.txt" not in diff
    assert "later.txt" not in diff
    assert git("status", "--porcelain", cwd=control) == original_status
    assert git("rev-parse", "HEAD", cwd=control) == original_head

