from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.exceptions import GitHubClientError, TrelloClientError
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.config.project_config import GitHubAppSettings
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
    def get_comments(self, repository: str, issue_number: str) -> list[str]:
        assert (repository, issue_number) == ("owner/api", "42")
        return ["Human note", "Automated finding"]

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
            "comments": ["Human note", "Automated finding"],
        },
    )


def test_github_issues_can_be_browsed_as_common_tasks() -> None:
    provider = GitHubIssueTaskProvider(StubIssueClient(), "owner/api")  # type: ignore[arg-type]

    tasks = provider.list_tasks()

    assert [task.id for task in tasks] == ["42"]
    assert tasks[0].title == "Add audit log"
    assert "comments" not in tasks[0].metadata


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


def test_github_cli_loads_all_comment_pages_and_rejects_bad_responses(tmp_path: Path) -> None:
    pages = [
        [{"body": "Human note", "user": {"login": "human"}}],
        [{"body": "Automated finding", "user": {"login": "bot[bot]"}}],
    ]
    commands = StubCommands(CommandResult([], 0, json.dumps(pages), "", 0.1))
    client = GitHubIssueClient(commands, cwd=tmp_path)  # type: ignore[arg-type]

    assert client.get_comments("owner/api", "42") == ["Human note", "Automated finding"]
    assert commands.calls == [[
        "gh", "api", "--paginate", "--slurp",
        "repos/owner/api/issues/42/comments?per_page=100",
    ]]
    commands.result = CommandResult([], 0, "[[]]", "", 0.1)
    assert client.get_comments("owner/api", "42") == []
    for malformed in ("{", "{}", "[[{}]]", '[[{"body": null}]]'):
        commands.result = CommandResult([], 0, malformed, "", 0.1)
        with pytest.raises(GitHubClientError, match="comments"):
            client.get_comments("owner/api", "42")
    commands.result = CommandResult([], 1, "", "Forbidden", 0.1)
    with pytest.raises(GitHubClientError, match="Forbidden"):
        client.get_comments("owner/api", "42")


def test_github_provider_surfaces_comment_retrieval_failure() -> None:
    class FailedComments(StubIssueClient):
        def get_comments(self, repository: str, issue_number: str) -> list[str]:
            raise GitHubClientError("Comment retrieval failed")

    provider = GitHubIssueTaskProvider(FailedComments(), "owner/api")  # type: ignore[arg-type]
    with pytest.raises(GitHubClientError, match="Comment retrieval failed"):
        provider.get_task("42")


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


def test_planner_app_reads_issues_and_labels_without_gh(tmp_path: Path) -> None:
    commands = StubCommands(CommandResult([], 1, "", "gh must not run", 0.1))
    requests: list[httpx.Request] = []
    issue = {
        "number": 42, "title": "Plan", "body": "Details", "state": "open",
        "html_url": "https://github.com/owner/api/issues/42",
        "user": {"login": "author"}, "milestone": {"title": "V2"},
        "labels": [{"name": "planned"}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer planner-token"
        assert request.url.host == "github.example.test"
        assert request.url.path.startswith("/api/v3/repos/owner/api/")
        if request.url.path.endswith("/issues/42"):
            return httpx.Response(200, json=issue)
        if request.url.path.endswith("/issues/42/comments"):
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[
                    {"body": "Human note", "user": {"login": "human"}}
                ] * 100)
            return httpx.Response(200, json=[
                {"body": "Automated finding", "user": {"login": "bot[bot]"}}
            ])
        if request.url.path.endswith("/issues"):
            assert request.url.params["state"] == "open"
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[{**issue, "pull_request": {}}] * 100)
            return httpx.Response(200, json=[issue])
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"name": f"label-{n}"} for n in range(100)])
        return httpx.Response(200, json=[{"name": "final"}])

    class Token:
        def token(self) -> str:
            return "planner-token"

    client = GitHubIssueClient(
        commands, cwd=tmp_path,
        planner_app=GitHubAppSettings(
            app_id=1, installation_id=2, private_key_env="PLANNER_KEY",
            api_url="https://github.example.test/api/v3",
        ),
        authenticator=Token(),  # type: ignore[arg-type]
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    provider = GitHubIssueTaskProvider(client, "owner/api")

    task = provider.get_task("42")
    assert task.url == issue["html_url"]
    assert task.metadata["author"] == "author"
    assert task.metadata["milestone"] == "V2"
    assert task.metadata["labels"] == ["planned"]
    assert task.metadata["comments"] == ["Human note"] * 100 + ["Automated finding"]
    assert [item.id for item in provider.list_tasks(limit=1)] == ["42"]
    assert len(provider.list_labels()) == 101
    assert commands.calls == []
    assert len(requests) == 7


@pytest.mark.parametrize("payload", [[], {}, [{"body": None}]])
def test_planner_app_comment_responses(tmp_path: Path, payload: Any) -> None:
    class Token:
        def token(self) -> str:
            return "token"

    client = GitHubIssueClient(
        StubCommands(CommandResult([], 1, "", "gh must not run", 0.1)), cwd=tmp_path,
        planner_app=GitHubAppSettings(app_id=1, installation_id=2, private_key_env="KEY"),
        authenticator=Token(),  # type: ignore[arg-type]
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )),
    )
    if payload == []:
        assert client.get_comments("owner/api", "42") == []
    else:
        with pytest.raises(GitHubClientError, match="comments"):
            client.get_comments("owner/api", "42")


def test_trello_card_loads_paginated_comments_in_chronological_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    newest = [{"id": f"id-{index}", "type": "commentCard", "data": {"text": "Recent"},
               "memberCreator": {"username": "bot"}}
              for index in range(1000, 0, -1)]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/1/cards/card-1":
            return httpx.Response(200, json={
                "id": "card-1", "name": "Plan", "desc": "Details", "idBoard": "board-1",
            }, request=request)
        if request.url.path == "/1/boards/board-1/customFields":
            return httpx.Response(200, json=[], request=request)
        assert request.url.path == "/1/cards/card-1/actions"
        assert request.url.params["filter"] == "commentCard"
        if "before" not in request.url.params:
            return httpx.Response(200, json=newest, request=request)
        assert request.url.params["before"] == "id-1"
        return httpx.Response(200, json=[
            {"id": "id-0", "type": "commentCard", "data": {"text": "Old human note"},
             "memberCreator": {"username": "human"}},
        ], request=request)

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kwargs: respond(httpx.Request("GET", url, params=kwargs["params"])),
    )
    task = TrelloClient("key", "token").get_card("card-1")

    assert task.metadata["comments"][0] == "Old human note"
    assert task.metadata["comments"][-1] == "Recent"
    assert len(task.metadata["comments"]) == 1001
    assert len(requests) == 4


@pytest.mark.parametrize("actions", [[], {}, [{"id": "1", "type": "commentCard", "data": {}}]])
def test_trello_card_comment_responses(
    monkeypatch: pytest.MonkeyPatch, actions: Any
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1/cards/card-1":
            return httpx.Response(
                200, json={"id": "card-1", "idBoard": "board-1"}, request=request
            )
        if request.url.path == "/1/boards/board-1/customFields":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(200, json=actions, request=request)

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kwargs: respond(httpx.Request("GET", url, params=kwargs["params"])),
    )
    if actions == []:
        assert TrelloClient("key", "token").get_card("card-1").metadata["comments"] == []
    else:
        with pytest.raises(TrelloClientError, match="comments"):
            TrelloClient("key", "token").get_card("card-1")


def test_trello_comment_request_failure_stops_task_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1/cards/card-1":
            return httpx.Response(
                200, json={"id": "card-1", "idBoard": "board-1"}, request=request
            )
        if request.url.path == "/1/boards/board-1/customFields":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(403, json={"message": "Forbidden"}, request=request)

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kwargs: respond(httpx.Request("GET", url, params=kwargs["params"])),
    )
    with pytest.raises(TrelloClientError, match="/cards/card-1/actions"):
        TrelloClient("key", "token").get_card("card-1")


@pytest.mark.parametrize("payload", [None, {}, [{"number": "bad"}]])
def test_planner_app_rejects_invalid_issue_payload(tmp_path: Path, payload: Any) -> None:
    class Token:
        def token(self) -> str:
            return "token"

    client = GitHubIssueClient(
        StubCommands(CommandResult([], 0, "", "", 0.1)), cwd=tmp_path,
        planner_app=GitHubAppSettings(app_id=1, installation_id=2, private_key_env="KEY"),
        authenticator=Token(),  # type: ignore[arg-type]
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )),
    )
    with pytest.raises(GitHubClientError, match="issue response"):
        client.get_issue("owner/api", "42")


def test_planner_app_authentication_and_api_failures_do_not_use_gh(tmp_path: Path) -> None:
    commands = StubCommands(CommandResult([], 0, "{}", "", 0.1))
    app = GitHubAppSettings(app_id=1, installation_id=2, private_key_env="KEY")

    class FailedToken:
        def token(self) -> str:
            raise GitHubClientError("Could not mint a GitHub App installation token")

    auth_client = GitHubIssueClient(
        commands, cwd=tmp_path, planner_app=app,
        authenticator=FailedToken(),  # type: ignore[arg-type]
    )
    with pytest.raises(GitHubClientError, match="installation token"):
        auth_client.get_issue("owner/api", "42")

    class Token:
        def token(self) -> str:
            return "token"

    api_client = GitHubIssueClient(
        commands, cwd=tmp_path, planner_app=app,
        authenticator=Token(),  # type: ignore[arg-type]
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"message": "Forbidden"})
        )),
    )
    with pytest.raises(GitHubClientError, match="403"):
        api_client.list_labels("owner/api")
    assert commands.calls == []


def test_planner_app_rejects_invalid_list_payloads(tmp_path: Path) -> None:
    class Token:
        def token(self) -> str:
            return "token"

    client = GitHubIssueClient(
        StubCommands(CommandResult([], 0, "", "", 0.1)), cwd=tmp_path,
        planner_app=GitHubAppSettings(app_id=1, installation_id=2, private_key_env="KEY"),
        authenticator=Token(),  # type: ignore[arg-type]
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"name": "not an array"})
        )),
    )
    with pytest.raises(GitHubClientError, match="issue list response"):
        client.list_issues("owner/api")
    with pytest.raises(GitHubClientError, match="label list response"):
        client.list_labels("owner/api")


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
