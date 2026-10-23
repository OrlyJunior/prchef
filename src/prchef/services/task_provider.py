from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from prchef.clients.github_issue_client import GitHubIssueClient
from prchef.clients.trello_client import TrelloClient
from prchef.models.development_task import DevelopmentTask


class TaskProvider(Protocol):
    source: str

    def get_task(self, task_id: str) -> DevelopmentTask: ...

    def list_tasks(self, *, limit: int = 50) -> list[DevelopmentTask]: ...

    def pull_request_reference(
        self, task: DevelopmentTask, target_repository: str
    ) -> str: ...


@runtime_checkable
class LabelCatalogProvider(Protocol):
    """Optional task-provider capability: name the labels its tasks can carry.

    Names are returned exactly as the provider spells them and match the
    ``labels`` entries in browsed task metadata. A provider without this
    method has no label filter; that is distinct from an empty catalog.
    """

    def list_labels(self) -> list[str]: ...


class TrelloTaskProvider:
    # Deliberately not a LabelCatalogProvider: it has no browsable task list
    # to filter, so the launcher offers no label filter for it.
    source = "trello"

    def __init__(self, client: TrelloClient) -> None:
        self._client = client

    def get_task(self, task_id: str) -> DevelopmentTask:
        return self._client.get_card(task_id)

    def list_tasks(self, *, limit: int = 50) -> list[DevelopmentTask]:
        # The configured task contract does not identify a Trello board/list.
        # A card ID is therefore entered directly in the TUI until that optional
        # configuration exists; returning no browseable tasks is explicit rather
        # than guessing a board from credentials.
        del limit
        return []

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
        task = self._to_task(issue, fallback_id=task_id)
        task.metadata["comments"] = self._client.get_comments(self._repository, task.id)
        return task

    def list_tasks(self, *, limit: int = 50) -> list[DevelopmentTask]:
        return [
            self._to_task(issue, fallback_id=str(issue.get("number", "")))
            for issue in self._client.list_issues(self._repository, limit=limit)
        ]

    def list_labels(self) -> list[str]:
        names = self._label_names(self._client.list_labels(self._repository))
        # Labels differing only in case are distinct names for exact matching.
        return sorted(set(names), key=lambda name: (name.casefold(), name))

    def _to_task(self, issue: dict[str, Any], *, fallback_id: str) -> DevelopmentTask:
        labels = self._label_names(issue.get("labels"))
        author = issue.get("author")
        milestone = issue.get("milestone")
        return DevelopmentTask(
            id=str(issue.get("number", fallback_id)),
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
