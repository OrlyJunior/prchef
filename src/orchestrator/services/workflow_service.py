from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from orchestrator.clients.exceptions import JulesSessionNeedsHumanError
from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import ConfigurationError, ProjectConfigLoader
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.exceptions import WorkflowError
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.review_service import ReviewService
from orchestrator.services.spec_service import SpecService
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.validation_service import ValidationService


class WorkflowService:
    def __init__(
        self,
        workflow_repository: WorkflowRepository,
        task_provider: TaskProvider,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
    ) -> None:
        self._repository = workflow_repository
        self._task_provider = task_provider
        self._config_loader = config_loader
        self._client_factory = client_factory

    def run(self, task_id: str, repository_override: str | None = None) -> WorkflowExecution:
        execution = self._repository.create_execution(
            task_id, task_source=self._task_provider.source
        )
        self._repository.update_status(execution.id, WorkflowStatus.RUNNING)
        try:
            with self._step(execution.id, WorkflowStage.LOAD_TASK):
                task = self._task_provider.get_task(task_id)
                self._repository.update_task(execution.id, task)

            with self._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._config_loader.load(task, repository_override)
                self._repository.update_project(
                    execution.id, str(config.path), config.repository.base_branch
                )
                clients = self._client_factory.create(config)
                repository_slug = clients.git.github_repository()

            with self._step(execution.id, WorkflowStage.GENERATE_SPEC):
                spec = SpecService(clients.jules).generate(
                    task,
                    repository_slug,
                    config.repository.base_branch,
                    lambda session: self._repository.record_spec_jules_session(
                        execution.id, session.id, session.url
                    ),
                )
                self._repository.record_spec(execution.id, spec)
            if spec.has_blocking_ambiguity:
                message = "; ".join(spec.ambiguities) or "Jules reported blocking ambiguity"
                self._repository.record_error(execution.id, message)
                self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
                return self._repository.get_execution(execution.id)

            working_branch = self._branch_name(
                config.repository.branch_prefix, task.id, task.title
            )
            with self._step(execution.id, WorkflowStage.PREPARE_BRANCH):
                clients.git.create_branch(
                    config.repository.base_branch,
                    working_branch,
                    config.repository.protected_branches,
                )
                self._repository.record_branch(execution.id, working_branch)

            with self._step(execution.id, WorkflowStage.IMPLEMENT):
                implementation = ImplementationService(clients.codex, clients.git).implement(
                    task, spec, config.path
                )
                self._repository.record_implementation(execution.id, implementation)
                if not implementation.success:
                    raise WorkflowError(
                        implementation.error_message or "Codex implementation failed"
                    )

            with self._step(execution.id, WorkflowStage.VALIDATE):
                validation = ValidationService(clients.commands).validate(
                    config.path,
                    config.validation,
                    require_tests=config.safety.require_tests,
                )
                self._repository.record_validation(execution.id, validation)
                if not validation.success:
                    raise WorkflowError(validation.error_message or "Validation failed")

            with self._step(execution.id, WorkflowStage.COMMIT):
                commit_sha = clients.git.commit_all(
                    self._commit_message(task),
                    working_branch,
                    config.repository.protected_branches,
                )
                self._repository.record_commit(execution.id, commit_sha)

            with self._step(execution.id, WorkflowStage.PUSH):
                clients.git.push(working_branch, config.repository.protected_branches)

            with self._step(execution.id, WorkflowStage.CREATE_PR):
                pull_request = clients.github.create_pull_request(
                    title=task.title,
                    body=self._pull_request_body(
                        task,
                        validation,
                        self._task_provider.pull_request_reference(task, repository_slug),
                    ),
                    base_branch=config.repository.base_branch,
                    head_branch=working_branch,
                )
                self._repository.record_pull_request(execution.id, pull_request)

            with self._step(execution.id, WorkflowStage.REVIEW):
                review = ReviewService(clients.jules).review(
                    task=task,
                    spec=spec,
                    pull_request=pull_request,
                    diff=clients.git.diff(config.repository.base_branch),
                    validation=validation,
                    repository_slug=repository_slug,
                    base_branch=config.repository.base_branch,
                    feature_branch=working_branch,
                    on_session_created=lambda session: self._repository.record_review_jules_session(
                        execution.id, session.id, session.url
                    ),
                )
                self._repository.record_review(execution.id, review)
                if review.result is ReviewDecision.NEEDS_HUMAN:
                    self._repository.record_error(execution.id, review.summary)

            final_status = {
                ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
                ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
                ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
            }[review.result]
            self._repository.finish(execution.id, final_status)
        except ConfigurationError as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
        except JulesSessionNeedsHumanError as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
        except Exception as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.FAILED)
        return self._repository.get_execution(execution.id)

    @contextmanager
    def _step(self, execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        self._repository.update_stage(execution_id, stage)
        step_id = self._repository.start_step(execution_id, stage)
        try:
            yield
        except Exception as exc:
            self._repository.finish_step(step_id, StepStatus.FAILED, str(exc))
            raise
        else:
            self._repository.finish_step(step_id, StepStatus.SUCCEEDED)

    @staticmethod
    def _branch_name(prefix: str, card_id: str, title: str) -> str:
        normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()[:48]
        safe_card_id = re.sub(r"[^a-zA-Z0-9._-]", "-", card_id)[:32]
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        safe_prefix = prefix.strip("/") or "orchestrator"
        suffix = f"-{slug}" if slug else ""
        return f"{safe_prefix}/{safe_card_id}{suffix}-{timestamp}"

    @staticmethod
    def _commit_message(task: DevelopmentTask) -> str:
        title = " ".join(task.title.split())[:72]
        return f"feat: {title} ({task.id})"

    @staticmethod
    def _pull_request_body(
        task: DevelopmentTask, validation: ValidationResult, task_reference: str = ""
    ) -> str:
        checks = "\n".join(
            f"- {'PASS' if check.success else 'FAIL'}: `{check.name}`"
            for check in validation.checks
        )
        reference = f"\n\n{task_reference}" if task_reference else ""
        return (
            "## Source task\n\n"
            f"[{task.source}:{task.id}]({task.url}) — {task.title}{reference}\n\n"
            "## Validation\n\n"
            f"{checks or '- No commands executed'}\n\n"
            "Generated by autonomous-dev-orchestrator V1. No automatic merge is performed."
        )
