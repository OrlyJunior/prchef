from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import log_review_summary, log_spec_summary, redact_text
from orchestrator.services.exceptions import WorkflowError
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.review_service import ReviewService
from orchestrator.services.spec_service import SpecService
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.validation_service import ValidationService
from orchestrator.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class _ValidationFailed(WorkflowError):
    def __init__(self, validation: ValidationResult) -> None:
        super().__init__(validation.error_message or "Validation failed")
        self.validation = validation


_STAGE_LABELS = {
    WorkflowStage.LOAD_TASK: "task loading",
    WorkflowStage.LOAD_CONFIG: "repository/config resolution",
    WorkflowStage.GENERATE_SPEC: "spec generation",
    WorkflowStage.PREPARE_BRANCH: "branch preparation",
    WorkflowStage.IMPLEMENT: "implementation",
    WorkflowStage.VALIDATE: "local validation",
    WorkflowStage.COMMIT: "commit",
    WorkflowStage.PUSH: "push",
    WorkflowStage.CREATE_PR: "pull request creation",
    WorkflowStage.REVIEW: "review",
}


class WorkflowService:
    def __init__(
        self,
        workflow_repository: WorkflowRepository,
        task_provider: TaskProvider,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        *,
        verbose: bool = False,
    ) -> None:
        self._repository = workflow_repository
        self._task_provider = task_provider
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._verbose = verbose

    def run(self, task_id: str, repository_override: str | None = None) -> WorkflowExecution:
        execution = self._repository.create_execution(
            task_id, task_source=self._task_provider.source
        )
        self._repository.update_status(execution.id, WorkflowStatus.RUNNING)
        workspace_path: Path | None = None
        try:
            logger.info(
                "[workflow] Loading %s task %s", self._task_provider.source, task_id
            )
            with self._step(execution.id, WorkflowStage.LOAD_TASK):
                task = self._task_provider.get_task(task_id)
                self._repository.update_task(execution.id, task)

            with self._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._config_loader.load(task, repository_override)
                self._repository.update_project(
                    execution.id, str(config.path), config.repository.base_branch
                )
                logger.info(
                    "[workflow] Repository resolved: %s (base: %s)",
                    config.path,
                    config.repository.base_branch,
                )
                clients = self._client_factory.create(config)
                repository_slug = clients.git.github_repository()

            with self._step(execution.id, WorkflowStage.GENERATE_SPEC):
                spec = SpecService(clients.agent(AgentRole.PLANNER)).generate(
                    task,
                    config.path,
                    repository_slug,
                    config.repository.base_branch,
                    lambda reference: self._repository.record_spec_jules_session(
                        execution.id, reference.external_id, reference.url
                    ),
                )
                self._repository.record_spec(execution.id, spec)
                if self._verbose:
                    log_spec_summary(logger, spec)
            if spec.has_blocking_ambiguity:
                message = "; ".join(spec.ambiguities) or "Planner reported blocking ambiguity"
                self._repository.record_error(execution.id, message)
                self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
                return self._repository.get_execution(execution.id)

            working_branch = self._branch_name(
                config.repository.branch_prefix, task.id, task.title, execution.id
            )
            workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
            with self._step(execution.id, WorkflowStage.PREPARE_BRANCH):
                workspace_path = workspace_manager.workspace_path(execution.id)
                self._repository.record_workspace(execution.id, str(workspace_path))
                logger.info("[workspace] Base repository: %s", config.path)
                logger.info("[workspace] Base ref: origin/%s", config.repository.base_branch)
                logger.info("[workspace] Branch: %s", working_branch)
                logger.info("[workspace] Path: %s", workspace_path)
                workspace_path = workspace_manager.create(
                    execution.id,
                    config.repository.base_branch,
                    working_branch,
                    config.repository.protected_branches,
                )
                self._repository.record_branch(execution.id, working_branch)
            execution_clients = clients.at_path(workspace_path)

            implementation_service = ImplementationService(
                execution_clients.agent(AgentRole.IMPLEMENTER), execution_clients.git
            )
            with self._step(execution.id, WorkflowStage.IMPLEMENT):
                implementation = implementation_service.implement(task, spec, workspace_path)
                self._repository.record_implementation(execution.id, implementation)
                if not implementation.success:
                    raise WorkflowError(
                        implementation.error_message or "Implementation provider failed"
                    )

            validation = self._validate_with_corrections(
                execution_id=execution.id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )

            with self._step(execution.id, WorkflowStage.COMMIT):
                commit_sha = execution_clients.git.commit_all(
                    self._commit_message(task),
                    working_branch,
                    config.repository.protected_branches,
                )
                self._repository.record_commit(execution.id, commit_sha)

            with self._step(execution.id, WorkflowStage.PUSH):
                execution_clients.git.push(working_branch, config.repository.protected_branches)

            with self._step(execution.id, WorkflowStage.CREATE_PR):
                pull_request = execution_clients.github.create_pull_request(
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

            review_attempt = 0
            while True:
                with self._step(execution.id, WorkflowStage.REVIEW):
                    review = ReviewService(clients.agent(AgentRole.REVIEWER)).review(
                        task=task,
                        spec=spec,
                        pull_request=pull_request,
                        diff=execution_clients.git.diff(config.repository.base_branch),
                        validation=validation,
                        repository_path=workspace_path,
                        repository_slug=repository_slug,
                        base_branch=config.repository.base_branch,
                        feature_branch=working_branch,
                        on_execution_started=lambda reference: (
                            self._repository.record_review_jules_session(
                                execution.id, reference.external_id, reference.url
                            )
                        ),
                    )
                    self._repository.record_review(execution.id, review)
                    if self._verbose:
                        log_review_summary(logger, review)
                    if review.result is ReviewDecision.NEEDS_HUMAN:
                        self._repository.record_error(execution.id, review.summary)

                if review.result is not ReviewDecision.CHANGES_REQUESTED:
                    break
                if review_attempt >= config.review.max_review_retries:
                    break

                review_attempt += 1
                logger.info(
                    "[workflow] Review requested changes; requesting implementation "
                    "correction %s/%s",
                    review_attempt,
                    config.review.max_review_retries,
                )
                with self._step(execution.id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_review_findings(
                        task, spec, review, workspace_path
                    )
                    self._repository.record_implementation(execution.id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message
                            or "Review correction provider failed"
                        )

                validation = self._validate_with_corrections(
                    execution_id=execution.id,
                    execution_clients=execution_clients,
                    implementation_service=implementation_service,
                    task=task,
                    spec=spec,
                    workspace_path=workspace_path,
                    config=config,
                )

                with self._step(execution.id, WorkflowStage.COMMIT):
                    commit_sha = execution_clients.git.commit_all(
                        self._commit_message(task),
                        working_branch,
                        config.repository.protected_branches,
                    )
                    self._repository.record_commit(execution.id, commit_sha)

                with self._step(execution.id, WorkflowStage.PUSH):
                    execution_clients.git.push(
                        working_branch, config.repository.protected_branches
                    )

            final_status = {
                ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
                ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
                ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
            }[review.result]
            self._repository.finish(execution.id, final_status)
        except ConfigurationError as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
        except AgentNeedsHumanError as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow needs human input; preserving workspace: %s",
                    workspace_path,
                )
        except Exception as exc:
            self._repository.record_error(execution.id, str(exc))
            self._repository.finish(execution.id, WorkflowStatus.FAILED)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow failed; preserving workspace: %s", workspace_path
                )
        return self._repository.get_execution(execution.id)

    @contextmanager
    def _step(self, execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        label = _STAGE_LABELS.get(stage, stage.value.casefold().replace("_", " "))
        logger.info("[workflow] Entering %s", label)
        self._repository.update_stage(execution_id, stage)
        step_id = self._repository.start_step(execution_id, stage)
        try:
            yield
        except Exception as exc:
            self._repository.finish_step(step_id, StepStatus.FAILED, str(exc))
            logger.error("[workflow] %s failed: %s", label, redact_text(str(exc)))
            raise
        else:
            self._repository.finish_step(step_id, StepStatus.SUCCEEDED)
            logger.info("[workflow] Completed %s", label)

    def _validate_with_corrections(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        workspace_path: Path,
        config: ProjectConfig,
    ) -> ValidationResult:
        validation_attempt = 0
        while True:
            try:
                with self._step(execution_id, WorkflowStage.VALIDATE):
                    validation = ValidationService(execution_clients.commands).validate(
                        workspace_path,
                        config.validation,
                        require_tests=config.safety.require_tests,
                    )
                    self._repository.record_validation(execution_id, validation)
                    if not validation.success:
                        raise _ValidationFailed(validation)
            except _ValidationFailed as exc:
                if validation_attempt >= config.validation.max_validation_retries:
                    raise WorkflowError(str(exc)) from exc
                validation_attempt += 1
                logger.info(
                    "[workflow] Validation failed; requesting implementation correction %s/%s",
                    validation_attempt,
                    config.validation.max_validation_retries,
                )
                with self._step(execution_id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_validation_failures(
                        task, spec, exc.validation, workspace_path
                    )
                    self._repository.record_implementation(execution_id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message
                            or "Implementation correction provider failed"
                        ) from exc
                continue
            return validation

    @staticmethod
    def _branch_name(prefix: str, card_id: str, title: str, execution_id: str) -> str:
        normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()[:48]
        safe_card_id = re.sub(r"[^a-zA-Z0-9._-]", "-", card_id)[:32]
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        safe_prefix = prefix.strip("/") or "orchestrator"
        suffix = f"-{slug}" if slug else ""
        safe_execution_id = re.sub(r"[^a-zA-Z0-9._-]", "-", execution_id)[:12]
        return f"{safe_prefix}/{safe_card_id}{suffix}-{timestamp}-{safe_execution_id}"

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
