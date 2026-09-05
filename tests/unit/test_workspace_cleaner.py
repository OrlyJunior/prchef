from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.models import WorkflowExecution, WorkflowStatus
from orchestrator.services.workspace_cleaner import (
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
        _execution(base / "human", WorkflowStatus.NEEDS_HUMAN),
        _execution(base / "pending", WorkflowStatus.PENDING),
        _execution(base / "running", WorkflowStatus.RUNNING),
    ]
    service, _, _ = _service(tmp_path, executions)

    items = {item.execution_id: item for item in service.inspect().workspaces}

    assert items["approved"].retention is WorkspaceRetention.CLEANABLE
    assert items["approved"].eligible
    assert items["changes"].eligible
    assert items["failed"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert not items["failed"].eligible
    assert items["human"].retention is WorkspaceRetention.RECOVERY_PRESERVED
    assert not items["human"].eligible
    assert items["pending"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert not items["pending"].eligible
    assert items["running"].retention is WorkspaceRetention.ACTIVE_PROTECTED
    assert not items["running"].eligible


def test_include_failed_keeps_active_workspaces_protected(tmp_path: Path) -> None:
    base = tmp_path / "workspaces" / "control"
    executions = [
        _execution(base / "failed", WorkflowStatus.FAILED),
        _execution(base / "human", WorkflowStatus.NEEDS_HUMAN),
        _execution(base / "pending", WorkflowStatus.PENDING),
        _execution(base / "running", WorkflowStatus.RUNNING),
    ]
    service, _, _ = _service(tmp_path, executions)

    items = {item.execution_id: item for item in service.inspect(include_failed=True).workspaces}

    assert items["failed"].eligible
    assert items["human"].eligible
    assert not items["pending"].eligible
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
