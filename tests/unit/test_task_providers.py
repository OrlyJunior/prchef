from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.exceptions import GitHubClientError
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.models import DevelopmentTask
from orchestrator.services.task_provider import (
    GitHubIssueTaskProvider,
    TrelloTaskProvider,
)
from tests.unit.fakes import FakeTrelloClient


class StubCommands:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append(command)
        return self.result


class StubIssueClient:
    def get_issue(self, repository: str, issue_number: str) -> dict[str, Any]:
        return {
            "number": int(issue_number),
            "title": "Add audit log",
            "body": "Persist an audit record.",
            "url": f"https://github.com/{repository}/issues/{issue_number}",
            "labels": [{"name": "ai-ready"}, {"name": "backend"}],
            "state": "OPEN",
            "author": {"login": "octocat"},
            "milestone": {"title": "V2"},
        }


def test_trello_task_is_resolved_through_provider() -> None:
    task = TrelloTaskProvider(FakeTrelloClient()).get_task("card-1")

    assert task.id == "card-1"
    assert task.source == "trello"
    assert task.repository == "example"


def test_github_issue_is_mapped_to_common_task_with_labels() -> None:
    provider = GitHubIssueTaskProvider(StubIssueClient(), "owner/api")  # type: ignore[arg-type]

    task = provider.get_task("42")

    assert task == DevelopmentTask(
        id="42",
        source="github",
        title="Add audit log",
        description="Persist an audit record.",
        url="https://github.com/owner/api/issues/42",
        repository="owner/api",
        metadata={
            "issue_number": 42,
            "repository": "owner/api",
            "labels": ["ai-ready", "backend"],
            "state": "OPEN",
            "author": "octocat",
            "milestone": "V2",
        },
    )


def test_github_issue_client_uses_gh_and_reports_missing_issue(tmp_path: Path) -> None:
    commands = StubCommands(
        CommandResult(
            command=[],
            exit_code=1,
            stdout="",
            stderr="GraphQL: Could not resolve to an Issue with the number of 404",
            duration_seconds=0.1,
        )
    )
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(GitHubClientError, match="Could not resolve"):
        client.get_issue("owner/api", "404")

    assert commands.calls == [
        [
            "gh",
            "issue",
            "view",
            "404",
            "--repo",
            "owner/api",
            "--json",
            "number,title,body,url,labels,state,author,milestone",
        ]
    ]


def test_github_issue_client_returns_structured_issue(tmp_path: Path) -> None:
    commands = StubCommands(
        CommandResult(
            command=[],
            exit_code=0,
            stdout=json.dumps(
                {
                    "number": 42,
                    "title": "Add audit log",
                    "body": "Persist it",
                    "url": "https://github.com/owner/api/issues/42",
                    "labels": [{"name": "backend"}],
                }
            ),
            stderr="",
            duration_seconds=0.1,
        )
    )

    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]
    issue = client.get_issue("owner/api", "42")

    assert issue["number"] == 42
    assert issue["labels"] == [{"name": "backend"}]


@pytest.mark.parametrize("repository", ["owner", "https://github.com/owner/api", "owner/api/x"])
def test_github_issue_client_rejects_invalid_repository(
    repository: str, tmp_path: Path
) -> None:
    commands = StubCommands(CommandResult([], 0, "{}", "", 0.1))
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(GitHubClientError, match="owner/repository"):
        client.get_issue(repository, "42")

    assert commands.calls == []
