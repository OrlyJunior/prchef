from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import GitHubClientError


class GitHubIssueClient:
    """Read GitHub Issues through the authenticated GitHub CLI."""

    _REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    def __init__(
        self,
        command_client: CommandClient,
        command: Sequence[str] = ("gh",),
        timeout_seconds: int = 120,
        cwd: Path | None = None,
    ) -> None:
        self._commands = command_client
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._cwd = (cwd or Path.cwd()).resolve()

    def get_issue(self, repository: str, issue_number: str) -> dict[str, Any]:
        normalized_repository = repository.strip()
        if not self._REPOSITORY_PATTERN.fullmatch(normalized_repository):
            raise GitHubClientError(
                "Invalid GitHub repository; expected the form 'owner/repository'"
            )
        try:
            normalized_number = int(issue_number)
        except ValueError as exc:
            raise GitHubClientError("GitHub issue number must be a positive integer") from exc
        if normalized_number <= 0:
            raise GitHubClientError("GitHub issue number must be a positive integer")

        result = self._run(
            "issue",
            "view",
            str(normalized_number),
            "--repo",
            normalized_repository,
            "--json",
            "number,title,body,url,labels,state,author,milestone",
        )
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise GitHubClientError("GitHub CLI returned invalid issue JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub CLI issue response must be a JSON object")
        return payload

    def _run(self, *arguments: str) -> CommandResult:
        result = self._commands.run(
            [*self._command, *arguments],
            cwd=self._cwd,
            timeout_seconds=self._timeout_seconds,
        )
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise GitHubClientError(f"GitHub issue lookup failed: {detail}")
        return result
