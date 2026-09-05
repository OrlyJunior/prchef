from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.git_client import GitClient
from orchestrator.config.project_config import WorkspaceSettings
from orchestrator.workspace.manager import WorkspaceManager


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repository = tmp_path / "control"
    subprocess.run(
        ["git", "init", "--initial-branch", "develop", str(repository)],
        check=True,
        capture_output=True,
    )
    _git(repository, "config", "user.email", "orchestrator@example.test")
    _git(repository, "config", "user.name", "Orchestrator Tests")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "Initial commit")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "develop")
    return repository, remote


def test_worktree_is_created_from_origin_without_touching_dirty_control_checkout(
    tmp_path: Path,
) -> None:
    control, _ = _repository_with_origin(tmp_path)
    (control / "tracked.txt").write_text("local change\n", encoding="utf-8")
    before_branch = _git(control, "branch", "--show-current")
    before_status = _git(control, "status", "--porcelain")
    workspace_root = tmp_path / "workspaces"
    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=workspace_root),
    )

    workspace = manager.create(
        "execution-1", "develop", "orchestrator/execution-1", ["main", "master", "develop"]
    )

    assert workspace == workspace_root / control.name / "execution-1"
    assert _git(workspace, "branch", "--show-current") == "orchestrator/execution-1"
    assert _git(workspace, "rev-parse", "HEAD") == _git(
        control, "rev-parse", "origin/develop"
    )
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(control, "branch", "--show-current") == before_branch
    assert _git(control, "status", "--porcelain") == before_status
    assert (control / "tracked.txt").read_text(encoding="utf-8") == "local change\n"


def test_workspaces_and_branches_are_unique_and_remain_available_for_recovery(
    tmp_path: Path,
) -> None:
    control, _ = _repository_with_origin(tmp_path)
    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=tmp_path / "workspaces"),
    )

    first = manager.create(
        "execution-1", "develop", "orchestrator/execution-1", ["develop"]
    )
    second = manager.create(
        "execution-2", "develop", "orchestrator/execution-2", ["develop"]
    )
    (first / "unfinished.txt").write_text("recover me", encoding="utf-8")

    assert first != second
    assert _git(first, "branch", "--show-current") == "orchestrator/execution-1"
    assert _git(second, "branch", "--show-current") == "orchestrator/execution-2"
    assert (first / "unfinished.txt").read_text(encoding="utf-8") == "recover me"


def test_workspace_root_inside_control_checkout_is_rejected(tmp_path: Path) -> None:
    control, _ = _repository_with_origin(tmp_path)
    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=control / ".orchestrator-workspaces"),
    )

    with pytest.raises(ValueError, match="must not create execution worktrees inside"):
        manager.workspace_path("execution-1")


def test_workspace_root_accepts_windows_style_paths() -> None:
    settings = WorkspaceSettings(root=Path("C:/Git/orchestrator-workspaces"))

    assert str(settings.root).replace("\\", "/").endswith("C:/Git/orchestrator-workspaces")
