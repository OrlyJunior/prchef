from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from prchef.clients.command_client import CommandClient
from prchef.clients.exceptions import GitClientError
from prchef.clients.git_client import GitClient
from prchef.config.project_config import WorkspaceSettings
from prchef.models import WorkflowExecution, WorkflowStatus
from prchef.services.workspace_cleaner import WorkspaceCleanupService
from prchef.workspace.manager import WorkspaceManager


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
    _git(repository, "config", "user.email", "prchef@example.test")
    _git(repository, "config", "user.name", "Prchef Tests")
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
        "execution-1", "develop", "prchef/execution-1", ["main", "master", "develop"]
    )

    assert workspace == workspace_root / control.name / "execution-1"
    assert _git(workspace, "branch", "--show-current") == "prchef/execution-1"
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
        "execution-1", "develop", "prchef/execution-1", ["develop"]
    )
    second = manager.create(
        "execution-2", "develop", "prchef/execution-2", ["develop"]
    )
    (first / "unfinished.txt").write_text("recover me", encoding="utf-8")

    assert first != second
    assert _git(first, "branch", "--show-current") == "prchef/execution-1"
    assert _git(second, "branch", "--show-current") == "prchef/execution-2"
    assert (first / "unfinished.txt").read_text(encoding="utf-8") == "recover me"


def test_git_client_removes_worktree_and_prunes_its_metadata(tmp_path: Path) -> None:
    control, _ = _repository_with_origin(tmp_path)
    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=tmp_path / "workspaces"),
    )
    workspace = manager.create(
        "execution-1", "develop", "prchef/execution-1", ["develop"]
    )

    GitClient(CommandClient(), control).remove_worktree(workspace)

    assert not workspace.exists()
    assert str(workspace) not in _git(control, "worktree", "list", "--porcelain")


def test_pytest_creates_its_temp_directory_inside_a_fresh_execution_worktree(
    tmp_path: Path,
) -> None:
    control, _ = _repository_with_origin(tmp_path)
    project_root = Path(__file__).resolve().parents[2]
    for configuration_file in ("pyproject.toml", "conftest.py"):
        (control / configuration_file).write_text(
            (project_root / configuration_file).read_text(encoding="utf-8"), encoding="utf-8"
        )
    tests = control / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text(
        "def test_smoke(tmp_path) -> None:\n    assert tmp_path.is_dir()\n", encoding="utf-8"
    )
    _git(control, "add", "pyproject.toml", "conftest.py", "tests/test_smoke.py")
    _git(control, "commit", "-m", "Add pytest validation")
    _git(control, "push")

    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=tmp_path / "workspaces"),
    )
    workspace = manager.create(
        "windows-path-execution", "develop", "prchef/windows-path-execution", ["develop"]
    )
    workspace_temp = workspace / ".pytest-tmp"

    assert not workspace_temp.exists()
    assert not (control / ".pytest-tmp").exists()

    completed = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert workspace_temp.is_dir()
    assert (workspace_temp / "run").is_dir()
    assert not (control / ".pytest-tmp").exists()


def test_workspace_root_inside_control_checkout_is_rejected(tmp_path: Path) -> None:
    control, _ = _repository_with_origin(tmp_path)
    manager = WorkspaceManager(
        GitClient(CommandClient(), control),
        control,
        WorkspaceSettings(root=control / ".prchef-workspaces"),
    )

    with pytest.raises(ValueError, match="must not create execution worktrees inside"):
        manager.workspace_path("execution-1")


def test_workspace_root_accepts_windows_style_paths() -> None:
    settings = WorkspaceSettings(root=Path("C:/Git/prchef-workspaces"))

    assert str(settings.root).replace("\\", "/").endswith("C:/Git/prchef-workspaces")


def test_default_workspace_uses_shared_user_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, _ = _repository_with_origin(tmp_path)
    state = tmp_path / "custom-state"
    monkeypatch.setattr("prchef.workspace.manager.default_state_directory", lambda: state)
    manager = WorkspaceManager(GitClient(CommandClient(), control), control, WorkspaceSettings())

    workspace = manager.create("execution-1", "develop", "prchef/execution-1", ["develop"])

    assert workspace == state / "workspaces" / control.name / "execution-1"
    assert not (control.parent / "prchef-workspaces").exists()
    assert not (control.parent / "orchestrator-workspaces").exists()
    with pytest.raises(GitClientError, match="Workspace path already exists"):
        manager.create("execution-1", "develop", "prchef/execution-1", ["develop"])


@pytest.mark.parametrize("root", ["custom-workspaces", "../relative-workspaces"])
def test_explicit_workspace_root_still_wins(tmp_path: Path, root: str) -> None:
    control = tmp_path / "control"
    configured = tmp_path / root if root == "custom-workspaces" else Path(root)
    manager = WorkspaceManager(
        GitClient(CommandClient(), control), control, WorkspaceSettings(root=configured)
    )
    expected_root = configured if configured.is_absolute() else control.parent / configured

    assert manager.workspace_path("one") == (expected_root / control.name / "one").resolve()


def test_default_workspace_inside_control_checkout_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("prchef.workspace.manager.default_state_directory", lambda: tmp_path)
    manager = WorkspaceManager(GitClient(CommandClient(), tmp_path), tmp_path, WorkspaceSettings())

    with pytest.raises(ValueError, match="must not create execution worktrees inside"):
        manager.workspace_path("one")


def test_recorded_path_wins_over_current_root_and_preserves_checkout_safety(tmp_path: Path) -> None:
    control = tmp_path / "control"
    manager = WorkspaceManager(GitClient(CommandClient(), control), control, WorkspaceSettings())
    recorded = tmp_path / "previous-custom-root" / control.name / "one"

    assert manager.workspace_path("one", recorded_path=str(recorded)) == recorded.resolve()
    with pytest.raises(ValueError, match="must not create execution worktrees inside"):
        manager.workspace_path("one", recorded_path=str(control / "one"))


def test_recorded_custom_workspace_can_be_cleaned_after_root_override_is_removed(
    tmp_path: Path,
) -> None:
    from tests.unit.test_workspace_cleaner import StubRepository

    control, _ = _repository_with_origin(tmp_path)
    git = GitClient(CommandClient(), control)
    manager = WorkspaceManager(git, control, WorkspaceSettings(root=tmp_path / "previous-root"))
    recorded = manager.create("one", "develop", "orchestrator/one", ["develop"])
    execution = WorkflowExecution(
        id="one", workspace_path=str(recorded), status=WorkflowStatus.APPROVED
    )
    cleaner = WorkspaceCleanupService(
        StubRepository([execution]),  # type: ignore[arg-type]
        git, control, WorkspaceManager(git, control, WorkspaceSettings()).workspace_root(),
    )

    dry = cleaner.cleanup(dry_run=True)
    assert dry.workspaces[0].path == recorded
    assert dry.workspaces[0].eligible
    assert recorded.is_dir()

    report = cleaner.cleanup(dry_run=False)
    assert report.removed_count == 1
    assert not recorded.exists()
    assert recorded not in git.worktree_paths()
