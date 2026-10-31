from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from prchef.clients.command_client import CommandClient, CommandResult
from prchef.clients.exceptions import (
    ExternalClientError,
    GitHubClientError,
    PartialGitHubIssueListError,
)
from prchef.clients.github_client import GitHubAppAuthenticator
from prchef.config.project_config import GitHubAppSettings


class GitHubIssueClient:
    """Read GitHub Issues with the Planner App or the authenticated GitHub CLI."""

    _REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    def __init__(
        self,
        command_client: CommandClient,
        command: Sequence[str] = ("gh",),
        timeout_seconds: int = 120,
        cwd: Path | None = None,
        planner_app: GitHubAppSettings | None = None,
        authenticator: GitHubAppAuthenticator | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._commands = command_client
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._cwd = (cwd or Path.cwd()).resolve()
        self._planner_app = planner_app if planner_app and planner_app.is_configured else None
        self._authenticator = None
        self._http = None
        if self._planner_app is not None:
            self._authenticator = authenticator or GitHubAppAuthenticator(self._planner_app)
            self._http = http_client or httpx.Client(timeout=30.0)

    def get_issue(self, repository: str, issue_number: str) -> dict[str, Any]:
        normalized_repository = self._normalize_repository(repository)
        try:
            normalized_number = int(issue_number)
        except ValueError as exc:
            raise GitHubClientError("GitHub issue number must be a positive integer") from exc
        if normalized_number <= 0:
            raise GitHubClientError("GitHub issue number must be a positive integer")

        if self._planner_app is not None:
            payload = self._api_get(
                f"repos/{normalized_repository}/issues/{normalized_number}"
            )
            return self._normalize_issue(payload)

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

    def get_comments(self, repository: str, issue_number: str) -> list[str]:
        """Return every comment body on one issue, oldest first."""
        normalized_repository = self._normalize_repository(repository)
        try:
            normalized_number = int(issue_number)
        except ValueError as exc:
            raise GitHubClientError("GitHub issue number must be a positive integer") from exc
        if normalized_number <= 0:
            raise GitHubClientError("GitHub issue number must be a positive integer")

        path = f"repos/{normalized_repository}/issues/{normalized_number}/comments"
        if self._planner_app is not None:
            comments: list[str] = []
            page = 1
            while True:
                payload = self._api_get(path, params={"per_page": 100, "page": page})
                comments.extend(self._comment_bodies(payload))
                if len(payload) < 100:
                    return comments
                page += 1

        result = self._run("api", "--paginate", "--slurp", f"{path}?per_page=100")
        try:
            pages = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise GitHubClientError("GitHub CLI returned invalid issue comments JSON") from exc
        if not isinstance(pages, list):
            raise GitHubClientError("GitHub CLI issue comments response must be a JSON array")
        comments = []
        for page in pages:
            comments.extend(self._comment_bodies(page))
        return comments

    @staticmethod
    def _comment_bodies(payload: Any) -> list[str]:
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) and isinstance(item.get("body"), str)
            for item in payload
        ):
            raise GitHubClientError("GitHub issue comments response contains invalid comments")
        return [item["body"] for item in payload]

    def list_issues(
        self, repository: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Read validated REST pages, retaining a prefix on discovery failure."""
        repository = self._normalize_repository(repository)
        if limit is not None and limit <= 0:
            raise GitHubClientError("GitHub issue list limit must be positive")
        issues: list[dict[str, Any]] = []
        seen: set[int] = set()
        page = 1
        while True:
            try:
                path = f"repos/{repository}/issues"
                if self._planner_app is not None:
                    payload = self._api_get(path, params={
                        "state": "open", "sort": "created", "direction": "desc",
                        "per_page": 100, "page": page,
                    })
                else:
                    result = self._run(
                        "api", f"{path}?state=open&sort=created&direction=desc"
                        f"&per_page=100&page={page}",
                    )
                    try:
                        payload = json.loads(result.stdout)
                    except (ValueError, TypeError) as exc:
                        raise GitHubClientError(
                            "GitHub CLI returned invalid issue list JSON"
                        ) from exc
                if not isinstance(payload, list):
                    raise GitHubClientError("GitHub issue list response must be a JSON array")
                validated: list[dict[str, Any]] = []
                for item in payload:
                    if not isinstance(item, dict):
                        raise GitHubClientError("GitHub issue list contains an invalid issue")
                    if "pull_request" not in item:
                        validated.append(self._normalize_issue(item))
            except ExternalClientError as exc:
                raise PartialGitHubIssueListError(str(exc), issues) from exc
            # Publish only whole validated pages. Raw length determines exhaustion:
            # even a page consisting entirely of pull requests may have a successor.
            for issue in validated:
                if issue["number"] not in seen:
                    seen.add(issue["number"])
                    issues.append(issue)
                    if limit is not None and len(issues) == limit:
                        return issues
            if len(payload) < 100:
                return issues
            page += 1

    def list_labels(self, repository: str) -> list[dict[str, Any]]:
        """Return the repository's complete label catalog for interactive task filtering."""

        normalized_repository = self._normalize_repository(repository)
        if self._planner_app is not None:
            planner_labels: list[dict[str, Any]] = []
            page = 1
            while True:
                payload = self._api_get(
                    f"repos/{normalized_repository}/labels",
                    params={"per_page": 100, "page": page},
                )
                if not isinstance(payload, list):
                    raise GitHubClientError("GitHub API label list response must be a JSON array")
                for item in payload:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                        raise GitHubClientError("GitHub API label list contains an invalid label")
                    planner_labels.append({"name": item["name"]})
                if len(payload) < 100:
                    return planner_labels
                page += 1
        # `gh label list` stops at a fixed --limit, which would silently drop
        # labels from large catalogs. The REST endpoint is paginated to the
        # end instead; jq emits each name as one JSON string per line so the
        # output stays parseable across every page and any label spelling.
        result = self._run(
            "api",
            "--paginate",
            f"repos/{normalized_repository}/labels?per_page=100",
            "--jq",
            ".[].name | tojson",
        )
        labels: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                name = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise GitHubClientError("GitHub CLI returned invalid label list JSON") from exc
            if not isinstance(name, str):
                raise GitHubClientError("GitHub CLI label list entries must be label names")
            labels.append({"name": name})
        return labels

    def _api_get(self, path: str, *, params: dict[str, str | int] | None = None) -> Any:
        assert self._planner_app is not None
        assert self._authenticator is not None
        assert self._http is not None
        token = self._authenticator.token()
        try:
            response = self._http.get(
                f"{self._planner_app.api_url}/{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GitHubClientError(f"GitHub API issue lookup failed: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:
            if path.endswith("/labels"):
                kind = "label list"
            elif path.endswith("/issues"):
                kind = "issue list"
            else:
                kind = "issue"
            raise GitHubClientError(f"GitHub API {kind} response is invalid JSON") from exc

    @staticmethod
    def _normalize_issue(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or "pull_request" in payload:
            raise GitHubClientError("GitHub API issue response must be an issue JSON object")
        if (
            not isinstance(payload.get("number"), int)
            or not isinstance(payload.get("title"), str)
            or not isinstance(payload.get("html_url"), str)
            or not isinstance(payload.get("state"), str)
        ):
            raise GitHubClientError("GitHub API issue response is missing required fields")
        labels = payload.get("labels", [])
        if not isinstance(labels, list) or not all(
            isinstance(label, dict) and isinstance(label.get("name"), str)
            for label in labels
        ):
            raise GitHubClientError("GitHub API issue response has invalid labels")
        return {
            "number": payload["number"],
            "title": payload["title"],
            "body": payload.get("body"),
            "url": payload.get("html_url"),
            "labels": labels,
            "state": payload["state"].upper(),
            "author": payload.get("user"),
            "milestone": payload.get("milestone"),
        }

    def _normalize_repository(self, repository: str) -> str:
        normalized_repository = repository.strip()
        if not self._REPOSITORY_PATTERN.fullmatch(normalized_repository):
            raise GitHubClientError(
                "Invalid GitHub repository; expected the form 'owner/repository'"
            )
        return normalized_repository

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
