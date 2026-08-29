from __future__ import annotations

from typing import Any, Protocol

from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.models.development_task import DevelopmentTask


class TaskProvider(Protocol):
    source: str

    def get_task(self, task_id: str) -> DevelopmentTask: ...

    def pull_request_reference(
        self, task: DevelopmentTask, target_repository: str
    ) -> str: ...


class TrelloTaskProvider:
    source = "trello"

    def __init__(self, client: TrelloClient) -> None:
        self._client = client

    def get_task(self, task_id: str) -> DevelopmentTask:
        return self._client.get_card(task_id)

    def pull_request_reference(
        self, task: DevelopmentTask, target_repository: str
    ) -> str:
        return ""


class GitHubIssueTaskProvider:
    source = "github"

    def __init__(self, client: GitHubIssueClient, repository: str) -> None:
        self._client = client
        self._repository = repository.strip()

    def get_task(self, task_id: str) -> DevelopmentTask:
        issue = self._client.get_issue(self._repository, task_id)
        labels = self._label_names(issue.get("labels"))
        author = issue.get("author")
        milestone = issue.get("milestone")
        return DevelopmentTask(
            id=str(issue.get("number", task_id)),
            source=self.source,
            title=str(issue.get("title") or ""),
            description=str(issue.get("body") or ""),
            url=str(issue.get("url") or ""),
            repository=self._repository,
            metadata={
                "issue_number": issue.get("number"),
                "repository": self._repository,
                "labels": labels,
                "state": issue.get("state"),
                "author": author.get("login") if isinstance(author, dict) else None,
                "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
            },
        )

    def pull_request_reference(
        self, task: DevelopmentTask, target_repository: str
    ) -> str:
        if self._repository.casefold() == target_repository.casefold():
            return f"Closes #{task.id}"
        return f"Related issue: {self._repository}#{task.id}"

    @staticmethod
    def _label_names(raw_labels: Any) -> list[str]:
        if not isinstance(raw_labels, list):
            return []
        return [
            str(label["name"])
            for label in raw_labels
            if isinstance(label, dict) and label.get("name")
        ]
