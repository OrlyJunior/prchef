from __future__ import annotations

import logging
import re
from pathlib import Path

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitClientError

logger = logging.getLogger(__name__)


class GitClient:
    def __init__(self, command_client: CommandClient, repository_path: Path) -> None:
        self._commands = command_client
        self._path = repository_path

    def at_path(self, repository_path: Path) -> GitClient:
        return GitClient(self._commands, repository_path)

    def ensure_clean(self) -> None:
        result = self._git("status", "--porcelain")
        if result.stdout.strip():
            raise GitClientError("Target repository has uncommitted changes")

    def create_branch(
        self, base_branch: str, working_branch: str, protected_branches: list[str]
    ) -> None:
        self._assert_safe_working_branch(working_branch, protected_branches)
        logger.info("[git] Preparing base branch: %s", base_branch)
        self.ensure_clean()
        self._git("fetch", "origin", base_branch)
        switched = self._git("switch", base_branch, check=False)
        if not switched.success:
            self._git("switch", "--create", base_branch, "--track", f"origin/{base_branch}")
        self._git("merge", "--ff-only", f"origin/{base_branch}")
        logger.info("[git] Creating working branch: %s", working_branch)
        self._git("switch", "--create", working_branch)
        current = self.current_branch()
        if current != working_branch:
            raise GitClientError(f"Expected branch {working_branch}, got {current}")
        logger.info("[git] Working branch ready: %s", working_branch)

    def create_worktree(
        self,
        workspace_path: Path,
        base_branch: str,
        working_branch: str,
        protected_branches: list[str],
    ) -> None:
        """Create an isolated workflow branch from the freshly fetched remote base ref.

        This intentionally performs no checkout, clean, stash, reset, or branch switch in
        the control checkout.  Git records the linked worktree in its shared metadata while
        the actual branch checkout happens only at ``workspace_path``.
        """
        self._assert_safe_working_branch(working_branch, protected_branches)
        if workspace_path.exists():
            raise GitClientError(f"Workspace path already exists: {workspace_path}")
        logger.info("[git] Fetching base branch: origin/%s", base_branch)
        self._git("fetch", "origin", base_branch)
        base_ref = f"origin/{base_branch}"
        self._git("rev-parse", "--verify", f"{base_ref}^{{commit}}")
        logger.info("[git] Creating worktree branch %s from %s", working_branch, base_ref)
        self._git("worktree", "add", "-b", working_branch, str(workspace_path), base_ref)

    def current_branch(self) -> str:
        return self._git("branch", "--show-current").stdout.strip()

    def changed_files(self) -> list[str]:
        lines = self._git("status", "--porcelain").stdout.splitlines()
        return [line[3:].strip() for line in lines if len(line) > 3]

    def github_repository(self) -> str:
        remote = self._git("remote", "get-url", "origin").stdout.strip()
        match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
        if not match:
            raise GitClientError(f"Cannot derive GitHub repository from origin URL: {remote}")
        return f"{match.group(1)}/{match.group(2)}"

    def commit_all(self, message: str, working_branch: str, protected_branches: list[str]) -> str:
        self._assert_safe_working_branch(working_branch, protected_branches)
        if self.current_branch() != working_branch:
            raise GitClientError("Refusing to commit outside the workflow branch")
        self._git("add", "--all")
        staged = self._git("diff", "--cached", "--name-only").stdout.strip()
        if not staged:
            raise GitClientError("Codex produced no changes to commit")
        logger.info("[git] Creating commit on %s", working_branch)
        self._git("commit", "-m", message)
        commit_sha = self._git("rev-parse", "HEAD").stdout.strip()
        logger.info("[git] Commit created: %s", commit_sha)
        return commit_sha

    def push(self, working_branch: str, protected_branches: list[str]) -> None:
        self._assert_safe_working_branch(working_branch, protected_branches)
        if self.current_branch() != working_branch:
            raise GitClientError("Refusing to push outside the workflow branch")
        logger.info("[git] Pushing branch: %s", working_branch)
        self._git("push", "--set-upstream", "origin", working_branch)
        logger.info("[git] Push completed: %s", working_branch)

    def diff(self, base_branch: str) -> str:
        return self._git("diff", "--no-ext-diff", f"{base_branch}...HEAD").stdout

    @staticmethod
    def _assert_safe_working_branch(branch: str, protected_branches: list[str]) -> None:
        normalized = branch.casefold()
        protected = {item.casefold() for item in ["main", "master", "develop", *protected_branches]}
        if normalized in protected:
            raise GitClientError(f"Refusing operation on protected branch: {branch}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("-", "/")):
            raise GitClientError(f"Unsafe branch name: {branch}")

    def _git(self, *arguments: str, check: bool = True) -> CommandResult:
        result = self._commands.run(["git", *arguments], cwd=self._path, timeout_seconds=300)
        if check and not result.success:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitClientError(f"git {' '.join(arguments)} failed: {detail}")
        return result
