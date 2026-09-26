from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitClientError

logger = logging.getLogger(__name__)


class GitHubAppAuthenticator(Protocol):
    """The narrow App-auth boundary Git operations need."""

    def token(self) -> str: ...

    def bot_identity(self) -> tuple[str, str]: ...


class GitClient:
    def __init__(
        self,
        command_client: CommandClient,
        repository_path: Path,
        *,
        implementer_authenticator: GitHubAppAuthenticator | None = None,
    ) -> None:
        self._commands = command_client
        self._path = repository_path
        self._implementer_authenticator = implementer_authenticator

    def at_path(self, repository_path: Path) -> GitClient:
        return GitClient(
            self._commands,
            repository_path,
            implementer_authenticator=self._implementer_authenticator,
        )

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
        base_commit: str | None = None,
    ) -> None:
        """Create an isolated workflow branch from the recorded base commit.

        This intentionally performs no checkout, clean, stash, reset, or branch switch in
        the control checkout.  Git records the linked worktree in its shared metadata while
        the actual branch checkout happens only at ``workspace_path``.
        """
        self._assert_safe_working_branch(working_branch, protected_branches)
        if workspace_path.exists():
            raise GitClientError(f"Workspace path already exists: {workspace_path}")
        # The fallback preserves callers outside the execution workflow.
        if base_commit is None:
            base_commit = self.fetch_base(base_branch)
        base_ref = base_commit
        self._git("rev-parse", "--verify", f"{base_ref}^{{commit}}")
        logger.info("[git] Creating worktree branch %s from %s", working_branch, base_ref)
        self._git("worktree", "add", "-b", working_branch, str(workspace_path), base_ref)

    def fetch_base(self, base_branch: str) -> str:
        logger.info("[git] Fetching base branch: origin/%s", base_branch)
        self._git("fetch", "origin", base_branch)
        return self._git("rev-parse", "--verify", f"origin/{base_branch}^{{commit}}").stdout.strip()

    @contextmanager
    def planning_snapshot(self, base_commit: str) -> Iterator[Path]:
        """Expose an exact commit in a disposable detached worktree for local agents."""
        with tempfile.TemporaryDirectory(prefix="orchestrator-plan-") as directory:
            snapshot = Path(directory) / "checkout"
            self._git("worktree", "add", "--detach", str(snapshot), base_commit)
            try:
                yield snapshot
            finally:
                self._git("worktree", "remove", "--force", str(snapshot))

    def remove_worktree(self, workspace_path: Path) -> bool:
        """Remove a linked worktree and prune its shared Git metadata.

        Returns whether a broken Git link required a filesystem-only fallback.  A worktree
        with an intact link is never removed by the fallback: for example, Git can refuse
        to remove it because it contains uncommitted recovery work.
        """
        result = self._git("worktree", "remove", str(workspace_path), check=False)
        if result.success:
            self._git("worktree", "prune")
            return False

        if self._worktree_link_is_intact(workspace_path):
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitClientError(
                f"git worktree remove failed for {workspace_path}: {detail}"
            )

        if workspace_path.exists():
            shutil.rmtree(workspace_path)
        self._git("worktree", "prune")
        return True

    def current_branch(self) -> str:
        return self._git("branch", "--show-current").stdout.strip()

    def verify_worktree_branch(self, working_branch: str, expected_commit: str | None) -> None:
        """Confirm a retained workflow worktree is still on its recorded branch.

        An execution that already recorded a commit must still point at that
        commit.  Earlier failures can deliberately retain uncommitted agent
        work, so they have no commit value to compare here.
        """
        if not self._path.is_dir():
            raise GitClientError(f"Workflow workspace is unavailable: {self._path}")
        current = self.current_branch()
        if current != working_branch:
            raise GitClientError(
                "Workflow workspace is on branch "
                f"{current or '<detached>'}, expected {working_branch}"
            )
        if expected_commit:
            head = self._git("rev-parse", "HEAD").stdout.strip()
            if head != expected_commit:
                raise GitClientError(
                    "Workflow workspace HEAD no longer matches the last recorded commit"
                )

    def changed_files(self) -> list[str]:
        lines = self._git("status", "--porcelain").stdout.splitlines()
        return [line[3:].strip() for line in lines if len(line) > 3]

    def discard_uncommitted_changes(self) -> None:
        """Return this workflow worktree to HEAD after an optional polish attempt fails."""
        self._git("reset", "--hard", "HEAD")
        self._git("clean", "-fd")

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
        self._git("commit", "-m", message, env=self._commit_environment())
        commit_sha = self._git("rev-parse", "HEAD").stdout.strip()
        logger.info("[git] Commit created: %s", commit_sha)
        return commit_sha

    def push(self, working_branch: str, protected_branches: list[str]) -> None:
        self._assert_safe_working_branch(working_branch, protected_branches)
        if self.current_branch() != working_branch:
            raise GitClientError("Refusing to push outside the workflow branch")
        logger.info("[git] Pushing branch: %s", working_branch)
        environment = self._push_environment()
        self._git("push", "--set-upstream", "origin", working_branch, env=environment)
        logger.info("[git] Push completed: %s", working_branch)

    def diff(self, base_ref: str) -> str:
        self.ensure_merge_base(base_ref)
        return self._git("diff", "--no-ext-diff", f"{base_ref}...HEAD").stdout

    def ensure_merge_base(self, base_branch: str) -> None:
        """Require ancestry shared with the recorded base commit."""
        result = self._git("merge-base", base_branch, "HEAD", check=False)
        if result.exit_code == 1:
            raise GitClientError(
                f"Worktree HEAD has disconnected history from base "
                f"{base_branch}: no merge base. Rewriting base history with "
                "filter-repo, filter-branch, or rebase --root while this worktree "
                "was in flight can cause this."
            )
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitClientError(f"git merge-base {base_branch} HEAD failed: {detail}")

    @staticmethod
    def _assert_safe_working_branch(branch: str, protected_branches: list[str]) -> None:
        normalized = branch.casefold()
        protected = {item.casefold() for item in ["main", "master", "develop", *protected_branches]}
        if normalized in protected:
            raise GitClientError(f"Refusing operation on protected branch: {branch}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("-", "/")):
            raise GitClientError(f"Unsafe branch name: {branch}")

    def _commit_environment(self) -> Mapping[str, str] | None:
        if self._implementer_authenticator is None:
            return None
        name, email = self._implementer_authenticator.bot_identity()
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
            }
        )
        return environment

    def _push_environment(self) -> Mapping[str, str] | None:
        if self._implementer_authenticator is None:
            return None
        origin = self._git("remote", "get-url", "origin").stdout.strip()
        if not origin.casefold().startswith(("https://", "http://")):
            logger.info("[git] Origin does not use HTTP(S); using ambient push transport")
            return None
        token = self._implementer_authenticator.token()
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        environment = dict(os.environ)
        # Git reads these process-local config values. Keeping the header out of
        # argv prevents installation tokens from entering command logs.
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            }
        )
        return environment

    def _git(
        self,
        *arguments: str,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        result = self._commands.run(
            ["git", *arguments], cwd=self._path, timeout_seconds=300, env=env
        )
        if check and not result.success:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitClientError(f"git {' '.join(arguments)} failed: {detail}")
        return result

    @staticmethod
    def _worktree_link_is_intact(workspace_path: Path) -> bool:
        git_file = workspace_path / ".git"
        if not git_file.is_file():
            return False
        try:
            value = git_file.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        prefix = "gitdir:"
        if not value.startswith(prefix):
            return False
        gitdir = Path(value.removeprefix(prefix).strip())
        if not gitdir.is_absolute():
            gitdir = workspace_path / gitdir
        return gitdir.exists()
