from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitHubClientError
from orchestrator.models.pull_request import PullRequest

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(
        self,
        command_client: CommandClient,
        repository_path: Path,
        command: Sequence[str],
        timeout_seconds: int,
    ) -> None:
        self._commands = command_client
        self._path = repository_path
        self._command = list(command)
        self._timeout_seconds = timeout_seconds

    def at_path(self, repository_path: Path) -> GitHubClient:
        return GitHubClient(
            self._commands,
            repository_path,
            self._command,
            self._timeout_seconds,
        )

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        base_branch: str,
        head_branch: str,
    ) -> PullRequest:
        logger.info("[github] Creating pull request from branch: %s", head_branch)
        created = self._run(
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base_branch,
            "--head",
            head_branch,
        )
        url = next(
            (
                line.strip()
                for line in reversed(created.stdout.splitlines())
                if line.startswith("http")
            ),
            "",
        )
        if not url:
            raise GitHubClientError("GitHub CLI did not return the pull request URL")
        viewed = self._run(
            "pr",
            "view",
            url,
            "--json",
            "number,url,title,baseRefName,headRefName",
        )
        try:
            data = json.loads(viewed.stdout)
            pull_request = PullRequest(
                number=int(data["number"]),
                url=str(data["url"]),
                title=str(data["title"]),
                base_branch=str(data["baseRefName"]),
                head_branch=str(data["headRefName"]),
            )
            logger.info(
                "[github] Pull request created: #%s %s",
                pull_request.number,
                pull_request.url,
            )
            return pull_request
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubClientError(f"Invalid GitHub CLI response: {viewed.stdout}") from exc

    def _run(self, *arguments: str) -> CommandResult:
        result = self._commands.run(
            [*self._command, *arguments],
            cwd=self._path,
            timeout_seconds=self._timeout_seconds,
        )
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitHubClientError(f"GitHub CLI failed: {detail}")
        return result
