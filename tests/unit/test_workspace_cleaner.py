from __future__ import annotations

from pathlib import Path

import pytest

from prchef.models import WorkflowExecution, WorkflowStatus
from prchef.services.workspace_cleaner import (
    WorkspaceCleanupService,
    WorkspaceRetention,
)


class StubRepository:
    def __init__(self, executions: list[WorkflowExecution]) -> None:
        self._executions = executions

    def list_executions(self) -> list[WorkflowExecution]:
        return self._executions


class StubGit:
    def __init__(self) -> None:
        self.removed: list[Path] = []

    def remove_worktree(self, path: Path) -> bool:
        self.removed.append(path)
        return False

    def worktree_paths(self) -> set[Path]:
        return set()


def _service(
    tmp_path: Path, executions: list[WorkflowExecution]
) -> tuple[WorkspaceCleanupService, StubGit, Path]:
    control = tmp_path / "control"
    control.mkdir()
    root = tmp_path / "workspaces"
    git = StubGit()
    return (
        WorkspaceCleanupService(  # type: ignore[arg-type]
            StubRepository(executions), git, control, root
        ),
        git,
        root / control.name,
    )


def _execution(path: Path, status: WorkflowStatus) -> WorkflowExecution:
    return WorkflowExecution(id=path.name, workspace_path=str(path), status=status)


def test_inspect_retains_active_and_recovery_workspaces_by_default(tmp_path: Path) -> None:
    base = tmp_path / "workspaces" / "control"
    executions = [
        _execution(base / "approved", WorkflowStatus.APPROVED),
        _execution(base / "changes", WorkflowStatus.CHANGES_REQUESTED),
        _execution(base / "failed", WorkflowStatus.FAILED),
        _execution(base / "cancelled", WorkflowStatus.CANCELLED),
        _execution(base / "human", WorkflowStatus.NEEDS_HUMAN),
        _execution(base / "pending", WorkflowStatus.PENDING),
        _execution(base / "waiting", WorkflowStatus.WAITING),
        _execution(base / "running", WorkflowStatus.RUNNING),
    ]
    service, _, _ = _service(tmp_path, executions)

    items = {item.execution_id: item for item in service.inspect().workspaces}

    assert items["approved"].retention is WorkspaceRetention.CLEANABLE
    assert items["approved"].eligible
    assert items["changes"].eligible
    assert items["failed"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert not items["failed"].eligible
    assert items["cancelled"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert not items["cancelled"].eligible
    assert items["human"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert not items["human"].eligible
    assert items["pending"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert not items["pending"].eligible
    # Work queued for provider capacity has a live worktree it will return to.
    assert items["waiting"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert not items["waiting"].eligible
    assert items["running"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert not items["running"].eligible


def test_include_failed_keeps_active_workspaces_protected(tmp_path: Path) -> None:
    base = tmp_path / "workspaces" / "control"
    executions = [
        _execution(base / "failed", WorkflowStatus.FAILED),
        _execution(base / "cancelled", WorkflowStatus.CANCELLED),
        _execution(base / "human", WorkflowStatus.NEEDS_HUMAN),
        _execution(base / "pending", WorkflowStatus.PENDING),
        _execution(base / "waiting", WorkflowStatus.WAITING),
        _execution(base / "running", WorkflowStatus.RUNNING),
    ]
    service, _, _ = _service(tmp_path, executions)

    items = {item.execution_id: item for item in service.inspect(include_failed=True).workspaces}

    assert items["failed"].eligible
    assert items["cancelled"].eligible
    assert items["human"].eligible
    assert not items["pending"].eligible
    assert not items["waiting"].eligible
    assert not items["running"].eligible


@pytest.mark.parametrize("unsafe_path", ["outside", "control"])
def test_paths_outside_workspace_scope_are_rejected(tmp_path: Path, unsafe_path: str) -> None:
    control = tmp_path / "control"
    path = tmp_path / "outside" if unsafe_path == "outside" else control
    execution = WorkflowExecution(
        id="unsafe", workspace_path=str(path), status=WorkflowStatus.APPROVED
    )
    service, git, _ = _service(tmp_path, [execution])

    report = service.cleanup(dry_run=False)

    assert report.workspaces[0].retention is WorkspaceRetention.REJECTED
    assert not report.workspaces[0].eligible
    assert git.removed == []


def test_cleanup_removes_only_eligible_workspaces(tmp_path: Path) -> None:
    base = tmp_path / "workspaces" / "control"
    executions = [
        _execution(base / "approved", WorkflowStatus.APPROVED),
        _execution(base / "failed", WorkflowStatus.FAILED),
        _execution(base / "running", WorkflowStatus.RUNNING),
    ]
    service, git, _ = _service(tmp_path, executions)

    report = service.cleanup(dry_run=False)

    assert git.removed == [(base / "approved").resolve()]
    assert report.removed_count == 1


def test_unmanaged_directories_are_listed_but_never_cleaned(tmp_path: Path) -> None:
    service, git, base = _service(tmp_path, [])
    orphan = base / "unmanaged"
    orphan.mkdir(parents=True)

    report = service.cleanup(dry_run=False)

    assert report.workspaces[0].path == orphan.resolve()
    assert report.workspaces[0].retention is WorkspaceRetention.UNMANAGED
    assert not report.workspaces[0].eligible
    assert git.removed == []


def test_index_deletion_runs_before_git_and_failure_preserves_worktree(tmp_path: Path) -> None:
    base = tmp_path / "workspaces" / "control"
    approved = (base / "approved").resolve()
    running = (base / "running").resolve()
    executions = [
        _execution(approved, WorkflowStatus.APPROVED),
        _execution(running, WorkflowStatus.RUNNING),
    ]
    service, git, _ = _service(tmp_path, executions)
    (base / "unmanaged").mkdir(parents=True)
    deleted: list[Path] = []

    def fail_deletion(path: Path) -> None:
        deleted.append(path)
        raise RuntimeError("index unavailable")

    service._delete_index_points = fail_deletion
    dry = service.cleanup(dry_run=True)
    assert deleted == []
    assert git.removed == []
    assert dry.removed_count == 0

    report = service.cleanup(dry_run=False)
    assert deleted == [approved]
    assert git.removed == []
    assert report.error_count == 1
    assert report.workspaces[0].error == "index unavailable"
    assert not report.workspaces[1].eligible
    assert not report.workspaces[2].eligible

    service._delete_index_points = deleted.append
    retry = service.cleanup(dry_run=False)
    assert git.removed == [approved]
    assert retry.removed_count == 1


@pytest.mark.parametrize("legacy_location", ["sibling", "old-state"])
def test_cleanup_lists_both_defaults_and_removes_only_finished_recorded_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_location: str,
) -> None:
    state = tmp_path / "user-state"
    monkeypatch.setattr(
        "prchef.services.workspace_cleaner.default_state_directory", lambda: state
    )
    control = tmp_path / "control"
    control.mkdir()
    root = state / "workspaces"
    legacy = (
        tmp_path / "orchestrator-workspaces" / control.name
        if legacy_location == "sibling"
        else Path.home() / ".orchestrator" / "workspaces" / control.name
    )
    current = root / control.name
    executions = [
        _execution(legacy / "old-approved", WorkflowStatus.APPROVED),
        _execution(legacy / "old-interrupted", WorkflowStatus.INTERRUPTED),
        _execution(legacy / "old-failed", WorkflowStatus.FAILED),
        _execution(current / "new-approved", WorkflowStatus.APPROVED),
    ]
    for execution in executions:
        Path(execution.workspace_path).mkdir(parents=True)
    orphan = legacy / "orphan"
    orphan.mkdir()
    git = StubGit()
    service = WorkspaceCleanupService(  # type: ignore[arg-type]
        StubRepository(executions), git, control, root
    )

    dry = service.cleanup(dry_run=True)
    items = {item.path: item for item in dry.workspaces}
    assert items[legacy / "old-approved"].eligible
    assert items[current / "new-approved"].eligible
    assert items[legacy / "old-interrupted"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert items[legacy / "old-failed"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert items[orphan].retention is WorkspaceRetention.UNMANAGED
    assert len(items) == 5
    assert git.removed == []
    assert all(path.exists() for path in items)

    report = service.cleanup(dry_run=False, include_failed=True)
    assert report.removed_count == 3
    assert git.removed == [legacy / "old-approved", legacy / "old-failed", current / "new-approved"]


def test_cleanup_deduplicates_roots_when_configured_root_is_legacy(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()
    legacy = tmp_path / "orchestrator-workspaces"
    orphan = legacy / control.name / "orphan"
    orphan.mkdir(parents=True)
    service = WorkspaceCleanupService(  # type: ignore[arg-type]
        StubRepository([]), StubGit(), control, legacy
    )

    assert [item.path for item in service.inspect().workspaces] == [orphan]
