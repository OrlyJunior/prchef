from __future__ import annotations

import logging
from pathlib import Path

from prchef.clients.exceptions import GitHubClientError
from prchef.clients.factory import WorkflowClients
from prchef.config.project_config import ProjectConfig
from prchef.models.code_review import CodeReview
from prchef.models.development_task import DevelopmentTask
from prchef.models.workflow_status import ReviewDecision, WorkflowStage
from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.runtime_logging import redact_text
from prchef.services.exceptions import WorkflowError
from prchef.services.review_service import format_pull_request_review
from prchef.services.workflow_stage_events import StageEvents
from prchef.services.workflow_stage_values import StageValues

logger = logging.getLogger("prchef.services.workflow_service")


class StageActions:
    """Apply Git and GitHub stage effects and render stage messages."""

    def __init__(self, repository: WorkflowRepository, events: StageEvents) -> None:
        self._repository = repository
        self._events = events

    def _commit_changes(
        self,
        execution_id: str,
        execution_clients: WorkflowClients,
        task: DevelopmentTask,
        working_branch: str,
        config: ProjectConfig,
        *,
        reconcile: bool = False,
    ) -> None:
        with self._events._step(execution_id, WorkflowStage.COMMIT):
            execution = self._repository.get_execution(execution_id)
            # Pre-migration executions retain their original local-branch comparison.
            execution_clients.git.ensure_merge_base(
                execution.base_commit or config.repository.base_branch
            )
            if reconcile and not execution_clients.git.changed_files():
                head = execution_clients.git.head_commit()
                if execution.commit_sha == head or (
                    head != (execution.commit_sha or execution.base_commit)
                    and execution_clients.git.head_commit_message()
                    == StageValues._commit_message(task)
                    and (
                        (execution.commit_sha or execution.base_commit) is None
                        or execution_clients.git.head_parent_commit()
                        == (execution.commit_sha or execution.base_commit)
                    )
                ):
                    self._repository.record_commit(execution_id, head)
                    return
                raise WorkflowError(
                    "Cannot verify whether the interrupted commit completed; "
                    "inspect the retained branch before retrying"
                )
            commit_sha = execution_clients.git.commit_all(
                StageValues._commit_message(task),
                working_branch,
                config.repository.protected_branches,
            )
            self._repository.record_commit(execution_id, commit_sha)

    @staticmethod
    def _assert_pull_request_open(
        execution_clients: WorkflowClients, repository_slug: str, pull_request_number: int
    ) -> None:
        try:
            is_open = execution_clients.github.is_pull_request_open(
                repository_slug, pull_request_number
            )
        except GitHubClientError as exc:
            raise WorkflowError(
                f"Persisted pull request #{pull_request_number} is unavailable; "
                "human intervention is required before continuing"
            ) from exc
        if not is_open:
            raise WorkflowError(
                f"Persisted pull request #{pull_request_number} is closed or unavailable; "
                "human intervention is required before continuing"
            )

    def _publish_review(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        repository_slug: str,
        pull_request_number: int,
        review: CodeReview,
        reconcile: bool = False,
    ) -> None:
        event = {
            ReviewDecision.APPROVED: "APPROVE",
            ReviewDecision.CHANGES_REQUESTED: "REQUEST_CHANGES",
            ReviewDecision.NEEDS_HUMAN: "COMMENT",
        }[review.result]
        body = format_pull_request_review(review)
        review_step_id = self._repository.get_execution(execution_id).review_step_id
        if review_step_id is not None:
            body += f"\n\n<!-- orchestrator-review:{execution_id}:{review_step_id} -->"
        try:
            if reconcile and review_step_id is None:
                raise WorkflowError(
                    "Cannot identify the interrupted review attempt; inspect the pull request "
                    "before retrying"
                )
            if reconcile and execution_clients.github.review_already_published(
                repository_slug, pull_request_number, event, body
            ):
                self._repository.finish_review_publication(execution_id)
                return
            execution_clients.github.submit_pull_request_review(
                repository=repository_slug,
                pull_request_number=pull_request_number,
                event=event,
                body=body,
            )
        except Exception as exc:
            if reconcile:
                raise WorkflowError(
                    "Cannot verify review publication; inspect the pull request before retrying"
                ) from exc
            message = f"GitHub review publication failed: {redact_text(str(exc))}"
            logger.warning("[github] %s", message)
            # Publication is an external side effect. Preserve the reviewer decision and let
            # the terminal workflow status reflect it even when GitHub is unavailable.
            self._repository.record_error(execution_id, message)
        finish_publication = getattr(self._repository, "finish_review_publication", None)
        if callable(finish_publication):
            finish_publication(execution_id)

    @staticmethod
    def _assign_implementer(
        *,
        execution_clients: WorkflowClients,
        pull_request_number: int,
        workspace_path: Path,
    ) -> None:
        """Assign the implementer App without making PR creation fail."""
        try:
            assignee = execution_clients.github.assign_implementer(
                pull_request_number, cwd=workspace_path
            )
            if assignee is not None:
                logger.info(
                    "[github] Assigned implementer %s to pull request #%s",
                    assignee,
                    pull_request_number,
                )
        except GitHubClientError as exc:
            logger.warning(
                "[github] Implementer assignment failed for pull request #%s; "
                "continuing workflow: %s",
                pull_request_number,
                redact_text(str(exc)),
            )
