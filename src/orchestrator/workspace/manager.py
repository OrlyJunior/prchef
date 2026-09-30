from __future__ import annotations

import re
from pathlib import Path

from orchestrator.clients.git_client import GitClient
from orchestrator.config.project_config import WorkspaceSettings
from orchestrator.config.state import default_state_directory


class WorkspaceManager:
    """Creates per-execution linked Git worktrees outside the control checkout."""

    def __init__(
        self,
        git: GitClient,
        control_repository: Path,
        settings: WorkspaceSettings,
    ) -> None:
        self._git = git
        self._control_repository = control_repository.resolve()
        self._settings = settings

    def create(
        self,
        execution_id: str,
        base_branch: str,
        working_branch: str,
        protected_branches: list[str],
        base_commit: str | None = None,
        *,
        recorded_path: str | None = None,
    ) -> Path:
        workspace = self.workspace_path(execution_id, recorded_path=recorded_path)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._git.create_worktree(
            workspace,
            base_branch,
            working_branch,
            protected_branches,
            base_commit,
        )
        return workspace

    def workspace_path(self, execution_id: str, *, recorded_path: str | None = None) -> Path:
        if recorded_path is not None:
            # Existing executions own their recorded location, regardless of current defaults.
            workspace = Path(recorded_path).expanduser().resolve()
        else:
            safe_execution_id = self._safe_component(execution_id, "execution id")
            repository_name = self._safe_component(self._control_repository.name, "repository name")
            workspace = (self.workspace_root() / repository_name / safe_execution_id).resolve()
        if self._is_within(workspace, self._control_repository):
            raise ValueError(
                "Workspace root must not create execution worktrees inside the configured "
                "repository"
            )
        return workspace

    def workspace_root(self) -> Path:
        """Return the resolved workspace root after applying control-checkout safety."""
        root = self._workspace_root()
        if self._is_within(root, self._control_repository):
            raise ValueError(
                "Workspace root must not create execution worktrees inside the configured "
                "repository"
            )
        return root

    def _workspace_root(self) -> Path:
        configured = self._settings.root
        if configured is None:
            return (default_state_directory() / "workspaces").resolve()
        root = configured.expanduser()
        if not root.is_absolute():
            root = self._control_repository.parent / root
        return root.resolve()

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
        if not normalized or normalized in {".", ".."}:
            raise ValueError(f"Unsafe {label}: {value!r}")
        return normalized

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True
