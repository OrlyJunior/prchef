from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from orchestrator.clients.git_client import GitClient
from orchestrator.config.state import default_state_directory
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import ACTIVE_WORKFLOW_STATUSES, WorkflowStatus
from orchestrator.repositories.workflow_repository import WorkflowRepository


class WorkspaceRetention(StrEnum):
    CLEANABLE = "CLEANABLE"
    RECOVERY_PRESERVED = "RECOVERY_PRESERVED"
    ACTIVE_PROTECTED = "ACTIVE_PROTECTED"
    UNMANAGED = "UNMANAGED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class CleanupWorkspace:
    path: Path
    execution_id: str | None
    status: WorkflowStatus | None
    retention: WorkspaceRetention
    eligible: bool
    exists: bool
    removed: bool = False
    error: str | None = None


@dataclass(slots=True)
class WorkspaceCleanupReport:
    workspaces: list[CleanupWorkspace] = field(default_factory=list)
    dry_run: bool = False

    @property
    def eligible_count(self) -> int:
        return sum(item.eligible for item in self.workspaces)

    @property
    def removed_count(self) -> int:
        return sum(item.removed for item in self.workspaces)

    @property
    def error_count(self) -> int:
        return sum(item.error is not None for item in self.workspaces)


class WorkspaceCleanupService:
    """Classify and safely remove retained execution worktrees for one control checkout."""

    _SUCCESSFUL_STATUSES = {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}
    _RECOVERY_STATUSES = {
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.NEEDS_HUMAN,
    }
    # WAITING work is queued for provider capacity, not finished: its worktree
    # is still needed as soon as a slot frees up.
    _ACTIVE_STATUSES = ACTIVE_WORKFLOW_STATUSES | {WorkflowStatus.INTERRUPTED}

    def __init__(
        self,
        workflow_repository: WorkflowRepository,
        git: GitClient,
        control_repository: Path,
        workspace_root: Path,
        *,
        delete_index_points: Callable[[Path], None] | None = None,
    ) -> None:
        self._repository = workflow_repository
        self._git = git
        self._control_repository = control_repository.resolve()
        roots = (
            workspace_root,
            default_state_directory() / "workspaces",
            self._control_repository.parent / "orchestrator-workspaces",
        )
        self._workspace_bases = tuple(
            dict.fromkeys((root / self._control_repository.name).resolve() for root in roots)
        )
        self._delete_index_points = delete_index_points
        self._registered_workspaces: set[Path] | None = None

    def inspect(self, *, include_failed: bool = False) -> WorkspaceCleanupReport:
        self._registered_workspaces = None
        report = WorkspaceCleanupReport(dry_run=True)
        known_paths: set[Path] = set()
        for execution in self._repository.list_executions():
            if not execution.workspace_path:
                continue
            workspace = self._resolve(execution.workspace_path)
            item = self._classify(execution, workspace, include_failed=include_failed)
            report.workspaces.append(item)
            if self._is_expected_workspace_path(workspace):
                known_paths.add(workspace)

        # A corrupted database must not turn a path shared with an active or recovery-preserved
        # execution into a deletion candidate.
        protected_paths = {
            item.path
            for item in report.workspaces
            if item.retention in {
                WorkspaceRetention.ACTIVE_PROTECTED,
                WorkspaceRetention.RECOVERY_PRESERVED,
                WorkspaceRetention.REJECTED,
            }
        }
        for item in report.workspaces:
            if item.eligible and item.path in protected_paths:
                item.eligible = False
                item.retention = WorkspaceRetention.ACTIVE_PROTECTED

        for base in self._workspace_bases:
            if not base.is_dir():
                continue
            for path in base.iterdir():
                resolved = path.resolve()
                if resolved not in known_paths:
                    report.workspaces.append(
                        CleanupWorkspace(
                            path=resolved,
                            execution_id=None,
                            status=None,
                            retention=WorkspaceRetention.UNMANAGED,
                            eligible=False,
                            exists=True,
                        )
                    )
        return report

    def cleanup(
        self, *, dry_run: bool, include_failed: bool = False
    ) -> WorkspaceCleanupReport:
        report = self.inspect(include_failed=include_failed)
        report.dry_run = dry_run
        if dry_run:
            return report
        for item in report.workspaces:
            if not item.eligible:
                continue
            try:
                if self._delete_index_points is not None:
                    self._delete_index_points(item.path)
                self._git.remove_worktree(item.path)
                item.removed = True
            except Exception as exc:
                item.error = str(exc)
        return report

    def _classify(
        self,
        execution: WorkflowExecution,
        workspace: Path,
        *,
        include_failed: bool,
    ) -> CleanupWorkspace:
        if not self._is_expected_workspace_path(workspace):
            return CleanupWorkspace(
                path=workspace,
                execution_id=execution.id,
                status=execution.status,
                retention=WorkspaceRetention.REJECTED,
                eligible=False,
                exists=workspace.exists(),
                error=(
                    "Workspace path is outside the configured workspace root or is not an "
                    "execution workspace for this control checkout"
                ),
            )
        if execution.status in self._ACTIVE_STATUSES:
            retention = WorkspaceRetention.ACTIVE_PROTECTED
            eligible = False
        elif execution.status in self._RECOVERY_STATUSES:
            retention = (
                WorkspaceRetention.CLEANABLE
                if include_failed
                else WorkspaceRetention.RECOVERY_PRESERVED
            )
            eligible = include_failed
        elif execution.status in self._SUCCESSFUL_STATUSES:
            retention = WorkspaceRetention.CLEANABLE
            eligible = True
        else:
            retention = WorkspaceRetention.ACTIVE_PROTECTED
            eligible = False
        return CleanupWorkspace(
            path=workspace,
            execution_id=execution.id,
            status=execution.status,
            retention=retention,
            eligible=eligible,
            exists=workspace.exists(),
        )

    def _is_expected_workspace_path(self, path: Path) -> bool:
        if self._is_within(path, self._control_repository):
            return False
        if path.parent in self._workspace_bases:
            return True
        # A recorded execution can outlive an explicit root override, too. Accept
        # those paths only when Git still ties them to this control checkout.
        if self._registered_workspaces is None:
            self._registered_workspaces = self._git.worktree_paths()
        return path in self._registered_workspaces

    @staticmethod
    def _resolve(path: str) -> Path:
        return Path(path).expanduser().resolve()

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True
