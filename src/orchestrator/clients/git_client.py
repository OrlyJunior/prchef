from __future__ import annotations

import re
from pathlib import Path

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitClientError


class GitClient:
    def __init__(self, command_client: CommandClient, repository_path: Path) -> None:
        self._commands = command_client
        self._path = repository_path

    def ensure_clean(self) -> None:
        result = self._git("status", "--porcelain")
        if result.stdout.strip():
            raise GitClientError("Target repository has uncommitted changes")

    def create_branch(
        self, base_branch: str, working_branch: str, protected_branches: list[str]
    ) -> None:
        self._assert_safe_working_branch(working_branch, protected_branches)
        self.ensure_clean()
        self._git("fetch", "origin", base_branch)
        switched = self._git("switch", base_branch, check=False)
        if not switched.success:
            self._git("switch", "--create", base_branch, "--track", f"origin/{base_branch}")
        self._git("merge", "--ff-only", f"origin/{base_branch}")
        self._git("switch", "--create", working_branch)
        current = self.current_branch()
        if current != working_branch:
            raise GitClientError(f"Expected branch {working_branch}, got {current}")

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
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def push(self, working_branch: str, protected_branches: list[str]) -> None:
        self._assert_safe_working_branch(working_branch, protected_branches)
        if self.current_branch() != working_branch:
            raise GitClientError("Refusing to push outside the workflow branch")
        self._git("push", "--set-upstream", "origin", working_branch)

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
