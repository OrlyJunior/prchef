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
    LabelCatalogProvider,
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

    def list_issues(self, repository: str, *, limit: int = 50) -> list[dict[str, Any]]:
        assert limit == 50
        return [self.get_issue(repository, "42")]

    def list_labels(self, repository: str) -> list[dict[str, Any]]:
        assert repository == "owner/api"
        return [
            {"name": "bug"},
            {"name": "Tester Agent"},
            {"name": "Bug"},
            {"name": "RAG"},
            {"name": "bug"},
            {"color": "ffffff"},
        ]


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


def test_github_issues_can_be_browsed_as_common_tasks() -> None:
    provider = GitHubIssueTaskProvider(StubIssueClient(), "owner/api")  # type: ignore[arg-type]

    tasks = provider.list_tasks()

    assert [task.id for task in tasks] == ["42"]
    assert tasks[0].title == "Add audit log"


def test_github_provider_supplies_exact_label_catalog_names() -> None:
    provider = GitHubIssueTaskProvider(StubIssueClient(), "owner/api")  # type: ignore[arg-type]

    assert isinstance(provider, LabelCatalogProvider)
    # Case-distinct names stay distinct; duplicates and nameless entries drop.
    assert provider.list_labels() == ["Bug", "bug", "RAG", "Tester Agent"]


def test_trello_provider_explicitly_lacks_label_filter_capability() -> None:
    provider = TrelloTaskProvider(FakeTrelloClient())

    assert not isinstance(provider, LabelCatalogProvider)
    assert not hasattr(provider, "list_labels")


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


def test_github_issue_client_lists_open_issues_for_task_browser(tmp_path: Path) -> None:
    commands = StubCommands(
        CommandResult(
            command=[],
            exit_code=0,
            stdout=json.dumps([{"number": 7, "title": "Interactive task", "labels": []}]),
            stderr="",
            duration_seconds=0.1,
        )
    )

    issues = GitHubIssueClient(commands, cwd=tmp_path).list_issues("owner/api", limit=25)  # type: ignore[arg-type]

    assert issues == [{"number": 7, "title": "Interactive task", "labels": []}]
    assert commands.calls == [
        [
            "gh",
            "issue",
            "list",
            "--repo",
            "owner/api",
            "--state",
            "open",
            "--limit",
            "25",
            "--json",
            "number,title,body,url,labels,state,author,milestone",
        ]
    ]


def test_github_issue_client_lists_complete_paginated_label_catalog(tmp_path: Path) -> None:
    # More labels than any single page or fixed `gh label list` limit; the
    # paginated jq output is one JSON-encoded name per line across pages.
    names = [f"label-{index}" for index in range(250)] + ['Quoted "RAG"', "Tester Agent"]
    commands = StubCommands(
        CommandResult(
            command=[],
            exit_code=0,
            stdout="".join(f"{json.dumps(name)}\n" for name in names) + "\n",
            stderr="",
            duration_seconds=0.1,
        )
    )

    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]
    labels = client.list_labels("owner/api")

    assert labels == [{"name": name} for name in names]
    assert commands.calls == [
        [
            "gh",
            "api",
            "--paginate",
            "repos/owner/api/labels?per_page=100",
            "--jq",
            ".[].name | tojson",
        ]
    ]


@pytest.mark.parametrize("stdout", ["not json\n", '{"name": "bug"}\n', "null\n"])
def test_github_issue_client_rejects_malformed_label_catalog(
    stdout: str, tmp_path: Path
) -> None:
    commands = StubCommands(CommandResult([], 0, stdout, "", 0.1))
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(GitHubClientError, match="label list"):
        client.list_labels("owner/api")


def test_github_issue_client_reports_label_catalog_failure(tmp_path: Path) -> None:
    commands = StubCommands(CommandResult([], 1, "", "HTTP 403: Forbidden", 0.1))
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(GitHubClientError, match="403"):
        client.list_labels("owner/api")


@pytest.mark.parametrize("repository", ["owner", "https://github.com/owner/api", "owner/api/x"])
def test_github_issue_client_rejects_invalid_repository(
    repository: str, tmp_path: Path
) -> None:
    commands = StubCommands(CommandResult([], 0, "{}", "", 0.1))
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(GitHubClientError, match="owner/repository"):
        client.get_issue(repository, "42")

    assert commands.calls == []
