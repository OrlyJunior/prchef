from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.clients.exceptions import GitHubClientError
from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.models.workflow_event import (
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowEventListener,
)
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    PolishOutcome,
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.providers.agent_provider import (
    AgentNeedsHumanError,
    AgentProvider,
    AgentProviderError,
)
from orchestrator.providers.concurrency import (
    ConcurrencyLimitedAgentProvider,
    ProviderConcurrencyCoordinator,
)
from orchestrator.providers.diagnostic_provider import DiagnosticAgentProvider
from orchestrator.providers.fallback_provider import FallbackAgentProvider
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import (
    log_review_summary,
    log_spec_summary,
    redact_payload,
    redact_text,
)
from orchestrator.services.exceptions import WorkflowCancelledError, WorkflowError
from orchestrator.services.execution_dispatcher import ExecutionDispatcher, ExecutionHandle
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.notifications import NotificationService
from orchestrator.services.repository_context import (
    RepositoryContextStrategy,
    resolve_repository_context_strategy,
)
from orchestrator.services.review_service import ReviewService, format_pull_request_review
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
    WorkflowStage.POLISH: "optional implementation polish",
    WorkflowStage.VALIDATE: "local validation",
    WorkflowStage.COMMIT: "commit",
    WorkflowStage.PUSH: "push",
    WorkflowStage.CREATE_PR: "pull request creation",
    WorkflowStage.REVIEW: "review",
}

_RUNTIME_OVERRIDES_DIAGNOSTIC_KIND = "RUNTIME_OVERRIDES_APPLIED"
_PROVIDER_WAIT_DIAGNOSTIC_KIND = "PROVIDER_SLOT_WAIT"
_PROVIDER_ADMITTED_DIAGNOSTIC_KIND = "PROVIDER_SLOT_ADMITTED"
_QUOTA_EXHAUSTED_DIAGNOSTIC_KIND = "PROVIDER_QUOTA_EXHAUSTED"
_MANUAL_CONTINUATION_DIAGNOSTIC_KIND = "MANUAL_CONTINUATION"


def _latest_failure_was_quota(diagnostics: Sequence[ExecutionDiagnostic]) -> bool:
    """Whether the newest quota/continuation marker is a quota exhaustion.

    A continuation records its own marker, so a quota failure that was already
    continued does not make a later, ordinary failure eligible again.
    """
    for diagnostic in reversed(diagnostics):
        if diagnostic.category is not DiagnosticCategory.WORKFLOW_EVENT:
            continue
        kind = diagnostic.payload.get("kind")
        if kind == _QUOTA_EXHAUSTED_DIAGNOSTIC_KIND:
            return True
        if kind == _MANUAL_CONTINUATION_DIAGNOSTIC_KIND:
            return False
    return False


@dataclass(slots=True)
class _ExecutionRuntime:
    """Mutable state owned by exactly one in-flight execution.

    Concurrent executions share a service instance, so the observer and the
    stage used for diagnostic attribution can never live on the service itself.
    """

    listener: WorkflowEventListener | None = None
    active_stage: WorkflowStage | None = None


class _StageSequencer:
    """Sequence fresh and resumed workflow stages."""

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

    def _execute_workflow(
        self,
        execution: WorkflowExecution,
        task_id: str,
        repository_override: str | None,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, object]],
        admission_task: DevelopmentTask | None = None,
        admission_error: Exception | None = None,
    ) -> WorkflowExecution:
        """Run a workflow using the same persisted transitions used by the CLI."""

        try:
            self._service._start_admitted(execution.id)
        except WorkflowCancelledError:
            return self._service._finish(execution.id, WorkflowStatus.CANCELLED, None)
        workspace_path: Path | None = None
        config: ProjectConfig | None = None
        try:
            logger.info(
                "[workflow] Loading %s task %s", self._service._task_provider.source, task_id
            )
            with self._service._step(execution.id, WorkflowStage.LOAD_TASK):
                # A CLI submission without an explicit repository reads the task
                # to establish its admission identity. Use that same snapshot or
                # error after dispatch, so a later provider edit cannot change
                # the repository being executed.
                if admission_error is not None:
                    raise admission_error
                task = admission_task or self._service._task_provider.get_task(task_id)
                self._service._repository.update_task(execution.id, task)

            with self._service._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._service._config_loader.load(task, repository_override)
                if provider_overrides or runtime_overrides:
                    config = config.with_agent_runtime_overrides(
                        provider_overrides,
                        runtime_overrides,
                    )
                    self._service._record_runtime_overrides(
                        execution.id, provider_overrides, runtime_overrides
                    )
                self._service._repository.update_project(
                    execution.id,
                    str(config.path),
                    config.repository.base_branch,
                    repository_override or task.repository or str(config.path),
                )
                logger.info(
                    "[workflow] Repository resolved: %s (base: %s)",
                    config.path,
                    config.repository.base_branch,
                )
                clients = self._service._client_factory.create(config)
                repository_slug = clients.git.github_repository()
                repository_context_strategy = self._service._repository_context_strategy(config)

            with self._service._step(execution.id, WorkflowStage.GENERATE_SPEC):
                spec = SpecService(
                    self._service._diagnostic_agent(
                        clients.agent(AgentRole.PLANNER),
                        config,
                        AgentRole.PLANNER,
                        execution.id,
                    ),
                    repository_context_strategy,
                ).generate(
                    task,
                    config.path,
                    repository_slug,
                    config.repository.base_branch,
                    lambda reference: self._service._repository.record_spec_jules_session(
                        execution.id, reference.external_id, reference.url
                    ),
                )
                self._service._repository.record_spec(execution.id, spec)
                if self._service._verbose:
                    log_spec_summary(logger, spec)
            if spec.has_blocking_ambiguity:
                return self._service._pause_for_human(
                    execution.id,
                    WorkflowStage.GENERATE_SPEC,
                    self._service._planner_prompt(spec),
                    config,
                )

            working_branch = self._service._branch_name(
                config.repository.branch_prefix, task.id, task.title, execution.id
            )
            workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
            with self._service._step(execution.id, WorkflowStage.PREPARE_BRANCH):
                workspace_path = workspace_manager.workspace_path(execution.id)
                self._service._repository.record_workspace(execution.id, str(workspace_path))
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
                self._service._repository.record_branch(execution.id, working_branch)
                clients.git.at_path(workspace_path).ensure_merge_base(config.repository.base_branch)
            execution_clients = clients.at_path(workspace_path)

            implementation_service = ImplementationService(
                self._service._diagnostic_agent(
                    execution_clients.agent(AgentRole.IMPLEMENTER),
                    config,
                    AgentRole.IMPLEMENTER,
                    execution.id,
                ),
                execution_clients.git,
            )
            with self._service._step(execution.id, WorkflowStage.IMPLEMENT):
                implementation = implementation_service.implement(
                    task,
                    spec,
                    workspace_path,
                    instructions_repository_path=config.path,
                )
                self._service._repository.record_implementation(execution.id, implementation)
                if not implementation.success:
                    raise WorkflowError(
                        implementation.error_message or "Implementation provider failed"
                    )

            validation = self._service._validate_with_corrections(
                execution_id=execution.id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )

            self._service._commit_changes(
                execution.id, execution_clients, task, working_branch, config
            )

            with self._service._step(execution.id, WorkflowStage.PUSH):
                execution_clients.git.push(working_branch, config.repository.protected_branches)

            with self._service._step(execution.id, WorkflowStage.CREATE_PR):
                pull_request = execution_clients.github.create_pull_request(
                    title=task.title,
                    body=self._service._pull_request_body(
                        task,
                        validation,
                        self._service._task_provider.pull_request_reference(task, repository_slug),
                    ),
                    base_branch=config.repository.base_branch,
                    head_branch=working_branch,
                )
                self._service._repository.record_pull_request(execution.id, pull_request)
                self._service._assign_implementer(
                    execution_clients=execution_clients,
                    pull_request_number=pull_request.number,
                    workspace_path=workspace_path,
                )

            review_attempt = 0
            polish_attempted = execution.polish_outcome is not None
            while True:
                with self._service._step(execution.id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._service._diagnostic_agent(
                            clients.agent(AgentRole.REVIEWER),
                            config,
                            AgentRole.REVIEWER,
                            execution.id,
                        ),
                        repository_context_strategy,
                    ).review(
                        task=task,
                        spec=spec,
                        pull_request=pull_request,
                        diff=execution_clients.git.diff(config.repository.base_branch),
                        validation=validation,
                        repository_path=workspace_path,
                        instructions_repository_path=config.path,
                        repository_slug=repository_slug,
                        base_branch=config.repository.base_branch,
                        feature_branch=working_branch,
                        on_execution_started=lambda reference: (
                            self._service._repository.record_review_jules_session(
                                execution.id, reference.external_id, reference.url
                            )
                        ),
                    )
                    self._service._repository.record_review(execution.id, review)
                    if self._service._verbose:
                        log_review_summary(logger, review)
                    if review.result is ReviewDecision.NEEDS_HUMAN:
                        self._service._repository.record_error(
                            execution.id, self._service._review_prompt(review)
                        )
                    self._service._publish_review(
                        execution_id=execution.id,
                        execution_clients=execution_clients,
                        repository_slug=repository_slug,
                        pull_request_number=pull_request.number,
                        review=review,
                    )

                if (
                    review.result is ReviewDecision.APPROVED
                    and review.suggestions
                    and not polish_attempted
                ):
                    polish_attempted = True
                    polish_outcome, polish_validation = self._service._evaluate_optional_polish(
                        execution_id=execution.id,
                        execution_clients=execution_clients,
                        implementation_service=implementation_service,
                        task=task,
                        spec=spec,
                        suggestions=review.suggestions,
                        workspace_path=workspace_path,
                        pull_request=pull_request,
                        working_branch=working_branch,
                        config=config,
                    )
                    if polish_outcome is PolishOutcome.SKIPPED:
                        break
                    if polish_validation is None:
                        raise WorkflowError("Applied optional polish is missing validation results")
                    validation = polish_validation
                    continue

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
                with self._service._step(execution.id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_review_findings(
                        task,
                        spec,
                        review,
                        workspace_path,
                        instructions_repository_path=config.path,
                    )
                    self._service._repository.record_implementation(execution.id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message or "Review correction provider failed"
                        )

                validation = self._service._validate_with_corrections(
                    execution_id=execution.id,
                    execution_clients=execution_clients,
                    implementation_service=implementation_service,
                    task=task,
                    spec=spec,
                    workspace_path=workspace_path,
                    config=config,
                )

                self._service._commit_changes(
                    execution.id, execution_clients, task, working_branch, config
                )

                with self._service._step(execution.id, WorkflowStage.PUSH):
                    execution_clients.git.push(working_branch, config.repository.protected_branches)

            final_status = {
                ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
                ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
                ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
            }[review.result]
            if final_status is WorkflowStatus.NEEDS_HUMAN:
                self._service._repository.request_human_input(
                    execution.id, WorkflowStage.REVIEW, self._service._review_prompt(review)
                )
            return self._service._finish(execution.id, final_status, config)
        except WorkflowCancelledError:
            result = self._service._finish(execution.id, WorkflowStatus.CANCELLED, config)
            if workspace_path is not None:
                logger.info(
                    "[workspace] Workflow cancelled; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except ConfigurationError as exc:
            return self._service._pause_for_human(
                execution.id,
                self._service._repository.get_execution(execution.id).current_stage,
                str(exc),
                config,
            )
        except AgentNeedsHumanError as exc:
            stage = self._service._repository.get_execution(execution.id).current_stage
            result = self._service._pause_for_human(execution.id, stage, str(exc), config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow needs human input; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except Exception as exc:
            self._service._repository.record_error(execution.id, str(exc))
            self._service._record_quota_failure(execution.id, exc)
            result = self._service._finish(execution.id, WorkflowStatus.FAILED, config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow failed; preserving workspace: %s", workspace_path
                )
            return result

    def _resume_planning(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        repository_context_strategy: RepositoryContextStrategy,
    ) -> WorkflowExecution:
        """Repeat only planning, then continue through a newly safe workspace."""
        with self._service._step(execution.id, WorkflowStage.GENERATE_SPEC):
            spec = SpecService(
                self._service._diagnostic_agent(
                    clients.agent(AgentRole.PLANNER), config, AgentRole.PLANNER, execution.id
                ),
                repository_context_strategy,
            ).generate(
                task,
                config.path,
                repository_slug,
                config.repository.base_branch,
                lambda reference: self._service._repository.record_spec_jules_session(
                    execution.id, reference.external_id, reference.url
                ),
            )
            self._service._repository.record_spec(execution.id, spec)
        if spec.has_blocking_ambiguity:
            return self._service._pause_for_human(
                execution.id,
                WorkflowStage.GENERATE_SPEC,
                self._service._planner_prompt(spec),
                config,
            )

        working_branch = self._service._branch_name(
            config.repository.branch_prefix, task.id, task.title, execution.id
        )
        workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
        with self._service._step(execution.id, WorkflowStage.PREPARE_BRANCH):
            workspace_path = workspace_manager.workspace_path(execution.id)
            self._service._repository.record_workspace(execution.id, str(workspace_path))
            workspace_path = workspace_manager.create(
                execution.id,
                config.repository.base_branch,
                working_branch,
                config.repository.protected_branches,
            )
            self._service._repository.record_branch(execution.id, working_branch)
            clients.git.at_path(workspace_path).ensure_merge_base(config.repository.base_branch)
        return self._service._implement_and_review(
            execution.id,
            task,
            spec,
            config,
            clients,
            repository_slug,
            workspace_path,
            working_branch,
            repository_context_strategy,
        )

    def _resume_review(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        repository_context_strategy: RepositoryContextStrategy,
    ) -> WorkflowExecution:
        if not execution.workspace_path or not execution.working_branch:
            raise WorkflowError(
                "Cannot resume review: the persisted workspace or branch is missing"
            )
        workspace_path = Path(execution.workspace_path)
        if not workspace_path.is_dir():
            raise WorkflowError(f"Cannot resume review: workspace is unavailable: {workspace_path}")
        if not execution.spec_snapshot or not execution.pull_request_snapshot:
            raise WorkflowError("Cannot resume review: persisted spec or pull request is missing")
        spec = RepositorySpec(**execution.spec_snapshot)
        pull_request = PullRequest(**execution.pull_request_snapshot)
        validation = self._service._validation_from_snapshot(execution.validation_snapshot)
        execution_clients = clients.at_path(workspace_path)
        implementation_service = ImplementationService(
            self._service._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution.id,
            ),
            execution_clients.git,
        )
        polish_attempted = execution.polish_outcome is not None
        while True:
            with self._service._step(execution.id, WorkflowStage.REVIEW):
                review = ReviewService(
                    self._service._diagnostic_agent(
                        clients.agent(AgentRole.REVIEWER), config, AgentRole.REVIEWER, execution.id
                    ),
                    repository_context_strategy,
                ).review(
                    task=task,
                    spec=spec,
                    pull_request=pull_request,
                    diff=execution_clients.git.diff(config.repository.base_branch),
                    validation=validation,
                    repository_path=workspace_path,
                    instructions_repository_path=config.path,
                    repository_slug=repository_slug,
                    base_branch=config.repository.base_branch,
                    feature_branch=execution.working_branch,
                    on_execution_started=lambda reference: (
                        self._service._repository.record_review_jules_session(
                            execution.id, reference.external_id, reference.url
                        )
                    ),
                )
                self._service._repository.record_review(execution.id, review)
                self._service._publish_review(
                    execution_id=execution.id,
                    execution_clients=execution_clients,
                    repository_slug=repository_slug,
                    pull_request_number=pull_request.number,
                    review=review,
                )
            if (
                review.result is ReviewDecision.APPROVED
                and review.suggestions
                and not polish_attempted
            ):
                polish_attempted = True
                polish_outcome, polish_validation = self._service._evaluate_optional_polish(
                    execution_id=execution.id,
                    execution_clients=execution_clients,
                    implementation_service=implementation_service,
                    task=task,
                    spec=spec,
                    suggestions=review.suggestions,
                    workspace_path=workspace_path,
                    pull_request=pull_request,
                    working_branch=execution.working_branch,
                    config=config,
                )
                if polish_outcome is PolishOutcome.APPLIED:
                    if polish_validation is None:
                        raise WorkflowError("Applied optional polish is missing validation results")
                    validation = polish_validation
                    continue
            break
        status = {
            ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
            ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
            ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
        }[review.result]
        if status is WorkflowStatus.NEEDS_HUMAN:
            self._service._repository.request_human_input(
                execution.id, WorkflowStage.REVIEW, self._service._review_prompt(review)
            )
        return self._service._finish(execution.id, status, config)

    def _implement_and_review(
        self,
        execution_id: str,
        task: DevelopmentTask,
        spec: RepositorySpec,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        workspace_path: Path,
        working_branch: str,
        repository_context_strategy: RepositoryContextStrategy,
        continuation_feedback: str | None = None,
        force_polish: bool = False,
    ) -> WorkflowExecution:
        """Continue a resumed planning execution without creating another execution."""
        execution_clients = clients.at_path(workspace_path)
        implementation_service = ImplementationService(
            self._service._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution_id,
            ),
            execution_clients.git,
        )
        try:
            with self._service._step(execution_id, WorkflowStage.IMPLEMENT):
                if continuation_feedback is None:
                    implementation = implementation_service.implement(
                        task, spec, workspace_path, instructions_repository_path=config.path
                    )
                else:
                    implementation = implementation_service.continue_implementation(
                        task,
                        spec,
                        continuation_feedback,
                        workspace_path,
                        instructions_repository_path=config.path,
                    )
                self._service._repository.record_implementation(execution_id, implementation)
                if not implementation.success:
                    raise WorkflowError(
                        implementation.error_message or "Implementation provider failed"
                    )
            validation = self._service._validate_with_corrections(
                execution_id=execution_id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )
            self._service._commit_changes(
                execution_id, execution_clients, task, working_branch, config
            )
            with self._service._step(execution_id, WorkflowStage.PUSH):
                pull_request_snapshot = self._service._repository.get_execution(
                    execution_id
                ).pull_request_snapshot
                if pull_request_snapshot:
                    existing_pr = PullRequest(**pull_request_snapshot)
                    self._service._assert_pull_request_open(
                        execution_clients, repository_slug, existing_pr.number
                    )
                execution_clients.git.push(working_branch, config.repository.protected_branches)
            if pull_request_snapshot:
                pull_request = PullRequest(**pull_request_snapshot)
            else:
                with self._service._step(execution_id, WorkflowStage.CREATE_PR):
                    pull_request = execution_clients.github.create_pull_request(
                        title=task.title,
                        body=self._service._pull_request_body(
                            task,
                            validation,
                            self._service._task_provider.pull_request_reference(
                                task, repository_slug
                            ),
                        ),
                        base_branch=config.repository.base_branch,
                        head_branch=working_branch,
                    )
                    self._service._repository.record_pull_request(execution_id, pull_request)
                    self._service._assign_implementer(
                        execution_clients=execution_clients,
                        pull_request_number=pull_request.number,
                        workspace_path=workspace_path,
                    )
            polish_attempted = not force_polish and (
                self._service._repository.get_execution(execution_id).polish_outcome is not None
            )
            review_attempt = 0
            while True:
                with self._service._step(execution_id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._service._diagnostic_agent(
                            clients.agent(AgentRole.REVIEWER),
                            config,
                            AgentRole.REVIEWER,
                            execution_id,
                        ),
                        repository_context_strategy,
                    ).review(
                        task=task,
                        spec=spec,
                        pull_request=pull_request,
                        diff=execution_clients.git.diff(config.repository.base_branch),
                        validation=validation,
                        repository_path=workspace_path,
                        instructions_repository_path=config.path,
                        repository_slug=repository_slug,
                        base_branch=config.repository.base_branch,
                        feature_branch=working_branch,
                        on_execution_started=lambda reference: (
                            self._service._repository.record_review_jules_session(
                                execution_id, reference.external_id, reference.url
                            )
                        ),
                    )
                    self._service._repository.record_review(execution_id, review)
                    self._service._publish_review(
                        execution_id=execution_id,
                        execution_clients=execution_clients,
                        repository_slug=repository_slug,
                        pull_request_number=pull_request.number,
                        review=review,
                    )
                if (
                    review.result is ReviewDecision.APPROVED
                    and review.suggestions
                    and not polish_attempted
                ):
                    polish_attempted = True
                    polish_outcome, polish_validation = self._service._evaluate_optional_polish(
                        execution_id=execution_id,
                        execution_clients=execution_clients,
                        implementation_service=implementation_service,
                        task=task,
                        spec=spec,
                        suggestions=review.suggestions,
                        workspace_path=workspace_path,
                        pull_request=pull_request,
                        working_branch=working_branch,
                        config=config,
                        force=force_polish,
                        before_push=lambda: self._service._assert_pull_request_open(
                            execution_clients, repository_slug, pull_request.number
                        ),
                    )
                    if polish_outcome is PolishOutcome.APPLIED:
                        if polish_validation is None:
                            raise WorkflowError(
                                "Applied optional polish is missing validation results"
                            )
                        validation = polish_validation
                        continue
                if (
                    review.result is ReviewDecision.CHANGES_REQUESTED
                    and review_attempt < config.review.max_review_retries
                ):
                    review_attempt += 1
                    with self._service._step(execution_id, WorkflowStage.IMPLEMENT):
                        implementation = implementation_service.correct_review_findings(
                            task,
                            spec,
                            review,
                            workspace_path,
                            instructions_repository_path=config.path,
                        )
                        self._service._repository.record_implementation(
                            execution_id, implementation
                        )
                        if not implementation.success:
                            raise WorkflowError(
                                implementation.error_message or "Review correction provider failed"
                            )
                    validation = self._service._validate_with_corrections(
                        execution_id=execution_id,
                        execution_clients=execution_clients,
                        implementation_service=implementation_service,
                        task=task,
                        spec=spec,
                        workspace_path=workspace_path,
                        config=config,
                    )
                    self._service._commit_changes(
                        execution_id, execution_clients, task, working_branch, config
                    )
                    with self._service._step(execution_id, WorkflowStage.PUSH):
                        self._service._assert_pull_request_open(
                            execution_clients, repository_slug, pull_request.number
                        )
                        execution_clients.git.push(
                            working_branch, config.repository.protected_branches
                        )
                    continue
                break
            status = {
                ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
                ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
                ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
            }[review.result]
            if status is WorkflowStatus.NEEDS_HUMAN:
                self._service._repository.request_human_input(
                    execution_id, WorkflowStage.REVIEW, self._service._review_prompt(review)
                )
            return self._service._finish(execution_id, status, config)
        except WorkflowCancelledError:
            return self._service._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._service._pause_for_human(
                execution_id, WorkflowStage.IMPLEMENT, str(exc), config
            )
        except Exception as exc:
            self._service._repository.record_error(execution_id, str(exc))
            self._service._record_quota_failure(execution_id, exc)
            return self._service._finish(execution_id, WorkflowStatus.FAILED, config)

    def _commit_changes(
        self,
        execution_id: str,
        execution_clients: WorkflowClients,
        task: DevelopmentTask,
        working_branch: str,
        config: ProjectConfig,
    ) -> None:
        with self._service._step(execution_id, WorkflowStage.COMMIT):
            execution_clients.git.ensure_merge_base(config.repository.base_branch)
            commit_sha = execution_clients.git.commit_all(
                self._service._commit_message(task),
                working_branch,
                config.repository.protected_branches,
            )
            self._service._repository.record_commit(execution_id, commit_sha)


class _CorrectionPolicy:
    """Own validation retries and optional polish policy."""

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

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
                with self._service._step(execution_id, WorkflowStage.VALIDATE):
                    validation = ValidationService(execution_clients.commands).validate(
                        workspace_path,
                        config.validation,
                        require_tests=config.safety.require_tests,
                        on_check_complete=lambda payload: self._service._record_validation_check(
                            execution_id, payload
                        ),
                    )
                    self._service._repository.record_validation(execution_id, validation)
                    if not validation.success:
                        self._service._record_diagnostic(
                            execution_id,
                            DiagnosticCategory.ERROR,
                            {
                                "operation": "validation",
                                "message": validation.error_message,
                                "failing_commands": [
                                    self._service._failing_command_diagnostic(check)
                                    for check in validation.checks
                                    if not check.success
                                ],
                            },
                            stage=WorkflowStage.VALIDATE,
                        )
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
                with self._service._step(execution_id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_validation_failures(
                        task,
                        spec,
                        exc.validation,
                        workspace_path,
                        instructions_repository_path=config.path,
                    )
                    self._service._repository.record_implementation(execution_id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message
                            or "Implementation correction provider failed"
                        ) from exc
                continue
            return validation

    def _validate_polish_changes(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        workspace_path: Path,
        config: ProjectConfig,
    ) -> ValidationResult | None:
        """Validate optional changes once and discard them if they are not safe to push."""
        try:
            with self._service._step(execution_id, WorkflowStage.VALIDATE):
                validation = ValidationService(execution_clients.commands).validate(
                    workspace_path,
                    config.validation,
                    require_tests=config.safety.require_tests,
                    on_check_complete=lambda payload: self._service._record_validation_check(
                        execution_id, payload
                    ),
                )
                self._service._repository.record_validation(execution_id, validation)
                if not validation.success:
                    self._service._record_diagnostic(
                        execution_id,
                        DiagnosticCategory.ERROR,
                        {
                            "operation": "optional_polish_validation",
                            "message": validation.error_message,
                            "failing_commands": [
                                self._service._failing_command_diagnostic(check)
                                for check in validation.checks
                                if not check.success
                            ],
                        },
                        stage=WorkflowStage.VALIDATE,
                    )
                    raise _ValidationFailed(validation)
        except _ValidationFailed:
            logger.warning(
                "[workflow] Optional polish failed validation; discarding polish changes"
            )
            self._service._discard_polish_changes(execution_clients)
            return None

        return validation

    def _evaluate_optional_polish(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        suggestions: list[str],
        workspace_path: Path,
        pull_request: PullRequest,
        working_branch: str,
        config: ProjectConfig,
        force: bool = False,
        before_push: Callable[[], None] | None = None,
    ) -> tuple[PolishOutcome, ValidationResult | None]:
        """Evaluate approved-review suggestions once and persist an auditable outcome."""
        persisted = self._service._repository.get_execution(execution_id).polish_outcome
        if persisted is not None and not force:
            return persisted, None

        with self._service._step(execution_id, WorkflowStage.POLISH):
            implementation = implementation_service.run_polish_pass(
                task,
                spec,
                suggestions,
                workspace_path,
                pull_request,
                instructions_repository_path=config.path,
            )
            self._service._repository.record_implementation(execution_id, implementation)

        if not implementation.success:
            logger.warning("[workflow] Optional polish did not complete; retaining approved review")
            self._service._discard_polish_changes(execution_clients)
            self._service._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
                fallback_reason=(
                    "The optional polish pass did not complete safely, so no changes were applied."
                ),
            )
            return PolishOutcome.SKIPPED, None

        if not implementation.changed_files:
            logger.info("[workflow] Implementer declined optional polish suggestions")
            self._service._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
            )
            return PolishOutcome.SKIPPED, None

        validation = self._service._validate_polish_changes(
            execution_id=execution_id,
            execution_clients=execution_clients,
            workspace_path=workspace_path,
            config=config,
        )
        if validation is None:
            self._service._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
                fallback_reason=(
                    "The optional changes did not pass validation, so no changes were applied."
                ),
            )
            return PolishOutcome.SKIPPED, None

        self._service._commit_changes(execution_id, execution_clients, task, working_branch, config)
        with self._service._step(execution_id, WorkflowStage.PUSH):
            if before_push is not None:
                before_push()
            execution_clients.git.push(working_branch, config.repository.protected_branches)

        self._service._repository.record_polish_outcome(execution_id, PolishOutcome.APPLIED)
        return PolishOutcome.APPLIED, validation


class _StageEvents:
    """Persist stage transitions and deliver diagnostic and workflow events."""

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

    @contextmanager
    def _step(self, execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        self._service._raise_if_cancellation_requested(execution_id)
        label = _STAGE_LABELS.get(stage, stage.value.casefold().replace("_", " "))
        runtime = self._service._runtime(execution_id)
        previous_stage = runtime.active_stage
        runtime.active_stage = stage
        logger.info("[workflow] Entering %s", label)
        self._service._repository.update_stage(execution_id, stage)
        step_id = self._service._repository.start_step(execution_id, stage)
        self._service._emit(
            execution_id,
            WorkflowEventKind.STAGE_STARTED,
            stage=stage,
            step_status=StepStatus.RUNNING,
        )
        try:
            yield
            self._service._raise_if_cancellation_requested(execution_id)
        except WorkflowCancelledError as exc:
            self._service._repository.finish_step(step_id, StepStatus.CANCELLED, str(exc))
            self._service._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.CANCELLED,
                message=str(exc),
            )
            logger.info("[workflow] %s cancelled", label)
            raise
        except Exception as exc:
            self._service._repository.finish_step(step_id, StepStatus.FAILED, str(exc))
            self._service._record_diagnostic(
                execution_id,
                DiagnosticCategory.ERROR,
                {
                    "operation": "workflow_stage",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                stage=stage,
            )
            self._service._emit(
                execution_id,
                WorkflowEventKind.STAGE_FAILED,
                stage=stage,
                step_status=StepStatus.FAILED,
                message=redact_text(str(exc)),
            )
            logger.error("[workflow] %s failed: %s", label, redact_text(str(exc)))
            raise
        else:
            self._service._repository.finish_step(step_id, StepStatus.SUCCEEDED)
            self._service._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.SUCCEEDED,
            )
            logger.info("[workflow] Completed %s", label)
        finally:
            runtime.active_stage = previous_stage

    def _record_diagnostic(
        self,
        execution_id: str,
        category: DiagnosticCategory,
        payload: dict[str, Any],
        *,
        stage: WorkflowStage | None = None,
        role: AgentRole | None = None,
        notify: bool = True,
    ) -> None:
        """Persist first, then notify observers. Diagnostics cannot affect a run."""
        sanitized = redact_payload(payload)
        if not isinstance(sanitized, dict):
            sanitized = {"value": sanitized}
        diagnostic = ExecutionDiagnostic(
            execution_id=execution_id,
            category=category,
            payload=sanitized,
            stage=stage,
            role=role,
        )
        try:
            self._service._repository.record_diagnostic(diagnostic)
        except AttributeError:
            # Lightweight repository fakes used by integrations predating the
            # diagnostics table retain their existing workflow behavior.
            pass
        except Exception:
            logger.warning("[workflow] Could not persist diagnostic event", exc_info=True)
        if self._service._verbose:
            logger.debug(
                "[diagnostic] %s",
                json.dumps(
                    {
                        "category": category.value,
                        "stage": stage.value if stage else None,
                        "role": role.value if role else None,
                        "payload": sanitized,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        if notify:
            self._service._notify_event(
                WorkflowEvent(
                    execution_id=execution_id,
                    kind=WorkflowEventKind.DIAGNOSTIC,
                    stage=stage,
                    category=category.value,
                    payload=sanitized,
                )
            )

    def _emit(
        self,
        execution_id: str,
        kind: WorkflowEventKind,
        *,
        stage: WorkflowStage | None = None,
        status: WorkflowStatus | None = None,
        step_status: StepStatus | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload: dict[str, Any] = {
            "kind": kind.value,
            "status": status.value if status else None,
            "step_status": step_status.value if step_status else None,
            "message": message,
        }
        if payload is not None:
            event_payload.update(payload)
        self._service._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            event_payload,
            stage=stage,
            notify=False,
        )
        sanitized_payload = redact_payload(payload) if payload is not None else None
        self._service._notify_event(
            WorkflowEvent(
                execution_id=execution_id,
                kind=kind,
                stage=stage,
                status=status,
                step_status=step_status,
                message=redact_text(message) if message is not None else None,
                payload=sanitized_payload if isinstance(sanitized_payload, dict) else None,
            )
        )

    def _notify_event(self, event: WorkflowEvent) -> None:
        runtime = self._service._peek_runtime(event.execution_id)
        listener = runtime.listener if runtime is not None else None
        if listener is None:
            return
        try:
            listener(event)
        except Exception:
            # A presentation observer is never allowed to change workflow
            # semantics or make a successfully persisted transition fail.
            logger.warning("[workflow] Event listener failed")


class WorkflowService:
    def __init__(
        self,
        workflow_repository: WorkflowRepository,
        task_provider: TaskProvider,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        *,
        verbose: bool = False,
        notification_service: NotificationService | None = None,
        dispatcher: ExecutionDispatcher | None = None,
        provider_coordinator: ProviderConcurrencyCoordinator | None = None,
        repository_index_clients: Any | None = None,
    ) -> None:
        self._repository = workflow_repository
        self._task_provider = task_provider
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._verbose = verbose
        self._notification_service = notification_service or NotificationService()
        # A composing process supplies one dispatcher and one coordinator so
        # every execution competes for the same bounded capacity. Isolated
        # callers get private, strictly sequential ones.
        self._dispatcher = dispatcher or ExecutionDispatcher()
        self._provider_coordinator = provider_coordinator or ProviderConcurrencyCoordinator()
        self._repository_index_clients = repository_index_clients
        self._runtimes: dict[str, _ExecutionRuntime] = {}
        self._runtime_lock = threading.RLock()
        self._sequencer = _StageSequencer(self)
        self._corrections = _CorrectionPolicy(self)
        self._stage_events = _StageEvents(self)

    def cancel_execution(self, execution_id: str) -> bool:
        """Request cancellation without coupling the caller to a provider.

        Queued, provider-blocked, running, and human-blocked executions are
        cancellable. Human-blocked work has no running stage and settles
        immediately in the repository.
        Providers remain responsible for any interruption mechanism they can
        safely offer. The workflow itself observes this persisted request at
        stage and provider-admission boundaries, so a request made by another
        process is restart-safe.
        """
        accepted = self._repository.request_cancellation(execution_id)
        if not accepted:
            return False
        runtime = self._peek_runtime(execution_id)
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            {
                "kind": "CANCELLATION_REQUESTED",
                "message": "Cancellation requested by user",
            },
            stage=runtime.active_stage if runtime is not None else None,
            notify=False,
        )
        # Wake work queued for a provider slot first, then drop work that has
        # not started at all. Both paths are idempotent: whichever observes the
        # persisted request first finishes the execution as CANCELLED.
        self._provider_coordinator.cancel(execution_id)
        self._dispatcher.cancel(execution_id)
        return True

    def _begin_runtime(
        self, execution_id: str, listener: WorkflowEventListener | None
    ) -> _ExecutionRuntime:
        runtime = _ExecutionRuntime(listener=listener)
        with self._runtime_lock:
            self._runtimes[execution_id] = runtime
        return runtime

    def _runtime(self, execution_id: str) -> _ExecutionRuntime:
        with self._runtime_lock:
            runtime = self._runtimes.get(execution_id)
            if runtime is None:
                runtime = _ExecutionRuntime()
                self._runtimes[execution_id] = runtime
            return runtime

    def _peek_runtime(self, execution_id: str) -> _ExecutionRuntime | None:
        with self._runtime_lock:
            return self._runtimes.get(execution_id)

    def _end_runtime(self, execution_id: str) -> None:
        with self._runtime_lock:
            self._runtimes.pop(execution_id, None)

    def _active_stage_of(self, execution_id: str) -> WorkflowStage | None:
        runtime = self._peek_runtime(execution_id)
        return runtime.active_stage if runtime is not None else None

    def submit_execution(
        self,
        task_id: str,
        repository_override: str | None = None,
        *,
        agent_providers: Mapping[AgentRole, str] | None = None,
        agent_runtime_options: Mapping[AgentRole, Mapping[str, object]] | None = None,
        event_listener: WorkflowEventListener | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Persist a fresh execution and queue it for bounded admission.

        This is the mandatory submission seam for workflow execution. The row
        exists (as ``PENDING``) and its ID is available before any work starts,
        so batch, scheduling, continuous-execution, and label-selection
        features submit here instead of adding their own concurrency handling.
        Queued work is in-memory only; recovering it after a restart is
        deliberately out of scope.

        ``event_listener`` is an observational hook for interactive clients. It
        is called after the corresponding repository update and failures in the
        listener are isolated from workflow execution.
        """

        # ProjectConfigLoader treats an empty override as absent. Resolve the
        # same repository for admission before creating the execution row.
        repository_override = repository_override or None
        repository_ref = repository_override
        admission_task: DevelopmentTask | None = None
        admission_error: Exception | None = None
        if isinstance(self._repository, WorkflowRepository) and repository_ref is None:
            # An explicit CLI task may derive its repository from the provider.
            # Preserve a LOAD_TASK failure record if this preliminary lookup fails.
            try:
                admission_task = deepcopy(self._task_provider.get_task(task_id))
            except Exception as error:
                admission_error = error
            if admission_task is not None:
                repository_ref = (
                    self._config_loader.repository_reference_for_task(admission_task)
                    if isinstance(self._config_loader, ProjectConfigLoader)
                    else admission_task.repository
                )
            if not repository_ref:

                def historical_aliases(reference: str) -> tuple[str, ...]:
                    if not isinstance(self._config_loader, ProjectConfigLoader):
                        return (reference,)
                    try:
                        return self._config_loader.repository_identity_aliases(reference)
                    except (ConfigurationError, OSError):
                        # LOAD_CONFIG can report a broken configuration after
                        # the provider's LOAD_TASK error is recorded.
                        return (reference,)

                repository_ref = self._repository.inferred_repository_for_task(
                    self._task_provider.source, task_id, historical_aliases
                )
        if isinstance(self._repository, WorkflowRepository):
            aliases: tuple[str, ...] = ()
            if repository_ref and isinstance(self._config_loader, ProjectConfigLoader):
                try:
                    aliases = self._config_loader.repository_identity_aliases(repository_ref)
                except (ConfigurationError, OSError):
                    # The normal LOAD_CONFIG stage records a configuration error.
                    pass
            execution = self._repository.create_execution(
                task_id,
                task_source=self._task_provider.source,
                repository_ref=repository_ref,
                repository_aliases=aliases,
            )
        else:
            execution = self._repository.create_execution(
                task_id, task_source=self._task_provider.source
            )
        self._begin_runtime(execution.id, event_listener)
        self._emit(
            execution.id,
            WorkflowEventKind.EXECUTION_CREATED,
            stage=execution.current_stage,
            status=execution.status,
        )
        provider_overrides = {
            role.value: provider for role, provider in (agent_providers or {}).items()
        }
        runtime_overrides = {
            role.value: dict(options) for role, options in (agent_runtime_options or {}).items()
        }
        return self._submit(
            execution.id,
            lambda: self._execute_workflow(
                execution,
                task_id,
                repository_override,
                provider_overrides,
                runtime_overrides,
                admission_task,
                admission_error,
            ),
            admission_gate=admission_gate,
        )

    def run(
        self,
        task_id: str,
        repository_override: str | None = None,
        *,
        agent_providers: Mapping[AgentRole, str] | None = None,
        agent_runtime_options: Mapping[AgentRole, Mapping[str, object]] | None = None,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Submit a workflow and wait for it, preserving synchronous callers."""

        return self.submit_execution(
            task_id,
            repository_override,
            agent_providers=agent_providers,
            agent_runtime_options=agent_runtime_options,
            event_listener=event_listener,
        ).result()

    def _submit(
        self,
        execution_id: str,
        work: Callable[[], WorkflowExecution],
        *,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Queue already-persisted work and clean its runtime up afterwards."""

        def run_job() -> WorkflowExecution:
            try:
                return work()
            finally:
                self._end_runtime(execution_id)

        def drop_job() -> WorkflowExecution:
            try:
                return self._finish(execution_id, WorkflowStatus.CANCELLED, None)
            finally:
                self._end_runtime(execution_id)

        handle = self._dispatcher.submit(
            execution_id, run_job, on_dropped=drop_job, ready=admission_gate
        )
        # Cancellation may commit after the execution becomes PENDING but
        # before this job is registered. In that window cancel_execution has
        # no queued job to remove. Check again after registration so a closed
        # admission gate cannot strand the accepted request indefinitely.
        # If cancellation arrives after this read, cancel_execution sees the
        # registered job; if a worker already started, it checks the request.
        if self._repository.cancellation_requested(execution_id):
            self._dispatcher.cancel(execution_id)
        return handle

    def _start_admitted(self, execution_id: str) -> None:
        """Move admitted work from the queue into a genuinely running state."""

        self._raise_if_cancellation_requested(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.RUNNING)
        self._emit(execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING)

    def _execute_workflow(
        self,
        execution: WorkflowExecution,
        task_id: str,
        repository_override: str | None,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, object]],
        admission_task: DevelopmentTask | None = None,
        admission_error: Exception | None = None,
    ) -> WorkflowExecution:
        return self._sequencer._execute_workflow(
            execution=execution,
            task_id=task_id,
            repository_override=repository_override,
            provider_overrides=provider_overrides,
            runtime_overrides=runtime_overrides,
            admission_task=admission_task,
            admission_error=admission_error,
        )

    def submit_response(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: WorkflowEventListener | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Persist a human response and queue the same execution for admission.

        A resume competes for capacity exactly like a fresh run, so it returns
        to ``PENDING`` until a worker starts it rather than claiming a running
        state it cannot yet use.
        """

        execution = self._repository.get_execution(execution_id)
        if execution.status is not WorkflowStatus.NEEDS_HUMAN:
            raise ValueError(f"Execution {execution_id} is not awaiting human input")
        interaction = self._repository.respond_to_human_input(execution_id, response)
        self._begin_runtime(execution_id, event_listener)
        # The repository atomically records the answer and queues it, so a
        # concurrent human-blocked cancellation cannot be undone here.
        self._emit(execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING)
        return self._submit(
            execution_id,
            lambda: self._execute_response(execution, interaction, response),
            admission_gate=admission_gate,
        )

    def respond_and_resume(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Persist a response and continue the same execution at its blocked stage.

        Provider sessions are deliberately not required for this operation. The
        next provider request receives the persisted task/spec/review context
        plus the human answer, which makes the workflow-level behaviour
        provider agnostic and restart-safe.
        """
        return self.submit_response(execution_id, response, event_listener=event_listener).result()

    def _execute_response(
        self,
        execution: WorkflowExecution,
        interaction: HumanInteraction,
        response: str,
    ) -> WorkflowExecution:
        execution_id = execution.id
        try:
            self._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, None)
        config: ProjectConfig | None = None
        try:
            task = self._task_from_snapshot(execution)
            config = self._config_loader.load(
                task, execution.repository_ref or execution.repository or None
            )
            provider_overrides, runtime_overrides = self._runtime_overrides_for_execution(
                execution_id
            )
            if provider_overrides or runtime_overrides:
                config = config.with_agent_runtime_overrides(provider_overrides, runtime_overrides)
            clients = self._client_factory.create(config)
            repository_slug = clients.git.github_repository()
            repository_context_strategy = self._repository_context_strategy(config)
            previous_responses = [
                (item.prompt, item.response)
                for item in execution.human_interactions
                if item.id != interaction.id and item.response
            ]
            clarified_task = self._task_with_responses(
                task,
                [
                    *previous_responses,
                    (interaction.prompt, interaction.response or response.strip()),
                ],
            )
            if interaction.requesting_stage is WorkflowStage.GENERATE_SPEC:
                return self._resume_planning(
                    execution,
                    clarified_task,
                    config,
                    clients,
                    repository_slug,
                    repository_context_strategy,
                )
            if interaction.requesting_stage is WorkflowStage.REVIEW:
                return self._resume_review(
                    execution,
                    clarified_task,
                    config,
                    clients,
                    repository_slug,
                    repository_context_strategy,
                )
            raise WorkflowError(
                f"Cannot resume human input requested during {interaction.requesting_stage.value}"
            )
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._pause_for_human(
                execution_id, interaction.requesting_stage, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            self._record_quota_failure(execution_id, exc)
            return self._finish(execution_id, WorkflowStatus.FAILED, config)

    def continuation_feedback(self, execution_id: str) -> str:
        """Return the editable feedback shown before a terminal execution is continued."""
        execution = self._repository.get_execution(execution_id)
        if self._assert_continuation_eligible(execution) is WorkflowStage.GENERATE_SPEC:
            return (
                "Planning stopped because the provider's quota was exhausted before a "
                "workspace was created. Repeat planning now that the provider is available."
            )
        if execution.status is WorkflowStatus.CHANGES_REQUESTED:
            if not execution.review_snapshot:
                raise WorkflowError("Cannot continue: the terminal review output was not persisted")
            return ImplementationService.render_review_findings(
                CodeReview(**execution.review_snapshot)
            )
        if execution.validation_snapshot:
            validation = self._validation_from_snapshot(execution.validation_snapshot)
            if not validation.success:
                return ImplementationService.render_validation_failures(validation)
        implementation_error = (execution.implementation_snapshot or {}).get("error_message")
        if isinstance(implementation_error, str) and implementation_error.strip():
            return implementation_error
        if execution.error_message:
            return execution.error_message
        return "The previous workflow attempt failed. Inspect the retained workspace and continue."

    def submit_continuation(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: WorkflowEventListener | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Validate a manual continuation, then queue it for bounded admission."""

        if not feedback.strip():
            raise ValueError("Continuation feedback must not be empty")
        execution = self._repository.get_execution(execution_id)
        stage = self._assert_continuation_eligible(execution)
        task = self._task_from_snapshot(execution)
        if stage is WorkflowStage.IMPLEMENT and not execution.spec_snapshot:
            raise WorkflowError("Cannot continue: the original repository spec was not persisted")
        config = self._config_loader.load(
            task, execution.repository_ref or execution.repository or None
        )
        provider_overrides, runtime_overrides = self._runtime_overrides_for_execution(execution_id)
        if provider_overrides or runtime_overrides:
            config = config.with_agent_runtime_overrides(provider_overrides, runtime_overrides)
        clients = self._client_factory.create(config)
        if stage is WorkflowStage.GENERATE_SPEC:
            # No workspace exists yet, so there is nothing to verify: planning
            # runs again and creates the workspace exactly as a fresh run would.
            self._begin_runtime(execution_id, event_listener)
            self._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {"kind": _MANUAL_CONTINUATION_DIAGNOSTIC_KIND, "feedback": feedback},
                stage=WorkflowStage.GENERATE_SPEC,
            )
            self._queue_resumed(execution_id)
            return self._submit(
                execution_id,
                lambda: self._execute_planning_retry(execution, task, config, clients, feedback),
                admission_gate=admission_gate,
            )
        assert execution.workspace_path is not None and execution.working_branch is not None
        assert execution.spec_snapshot
        workspace_path = Path(execution.workspace_path)
        working_branch = execution.working_branch
        execution_clients = clients.at_path(workspace_path)
        # Do this before changing status or queueing the implementer: a human
        # can safely correct a missing/moved worktree and try again.
        execution_clients.git.verify_worktree_branch(working_branch, execution.commit_sha)
        self._begin_runtime(execution_id, event_listener)
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            {"kind": _MANUAL_CONTINUATION_DIAGNOSTIC_KIND, "feedback": feedback},
            stage=WorkflowStage.IMPLEMENT,
        )
        self._queue_resumed(execution_id)
        spec = RepositorySpec(**execution.spec_snapshot)
        return self._submit(
            execution_id,
            lambda: self._execute_continuation(
                execution_id,
                task,
                spec,
                config,
                clients,
                workspace_path,
                working_branch,
                feedback,
            ),
            admission_gate=admission_gate,
        )

    def continue_execution(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Manually continue an eligible terminal execution in its retained workspace."""
        return self.submit_continuation(
            execution_id, feedback, event_listener=event_listener
        ).result()

    def _execute_continuation(
        self,
        execution_id: str,
        task: DevelopmentTask,
        spec: RepositorySpec,
        config: ProjectConfig,
        clients: WorkflowClients,
        workspace_path: Path,
        working_branch: str,
        feedback: str,
    ) -> WorkflowExecution:
        try:
            self._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        repository_slug = clients.git.github_repository()
        repository_context_strategy = self._repository_context_strategy(config)
        return self._implement_and_review(
            execution_id,
            task,
            spec,
            config,
            clients,
            repository_slug,
            workspace_path,
            working_branch,
            repository_context_strategy,
            continuation_feedback=feedback,
            force_polish=True,
        )

    def _execute_planning_retry(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        feedback: str,
    ) -> WorkflowExecution:
        execution_id = execution.id
        try:
            self._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        try:
            repository_slug = clients.git.github_repository()
            repository_context_strategy = self._repository_context_strategy(config)
            # Earlier answers still apply to the repeated planning attempt.
            clarified_task = self._task_with_responses(
                task,
                [
                    *(
                        (item.prompt, item.response)
                        for item in execution.human_interactions
                        if item.response
                    ),
                    ("Continuation feedback for repeating planning:", feedback.strip()),
                ],
            )
            return self._resume_planning(
                execution,
                clarified_task,
                config,
                clients,
                repository_slug,
                repository_context_strategy,
            )
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._pause_for_human(
                execution_id, WorkflowStage.GENERATE_SPEC, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            self._record_quota_failure(execution_id, exc)
            return self._finish(execution_id, WorkflowStatus.FAILED, config)

    def _queue_resumed(self, execution_id: str) -> None:
        """Park a resumed or continued execution in the shared submission queue."""

        self._repository.queue_execution(execution_id)
        self._emit(execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING)

    def _repository_context_strategy(self, config: ProjectConfig) -> RepositoryContextStrategy:
        """Resolve the configured strategy, sharing any process index clients."""

        return resolve_repository_context_strategy(
            config.repository_context.strategy,
            settings=config.repository_context,
            repository_key=config.configured_repository_key,
            configured_root=config.path,
            client_manager=self._repository_index_clients,
        )

    @staticmethod
    def continuation_stage(
        execution: WorkflowExecution,
        diagnostics: Callable[[], Sequence[ExecutionDiagnostic]],
    ) -> WorkflowStage | None:
        """Return where a manual continuation restarts, or ``None`` if it cannot.

        Continuations normally re-enter implementation in the retained
        workspace. A provider quota failure during planning happens before any
        workspace exists, so continuing it repeats planning instead. Diagnostics
        are only loaded for that case.
        """
        if execution.status is WorkflowStatus.CHANGES_REQUESTED:
            return WorkflowStage.IMPLEMENT
        if execution.status is not WorkflowStatus.FAILED:
            return None
        if execution.workspace_path is not None and execution.working_branch is not None:
            return WorkflowStage.IMPLEMENT
        if execution.workspace_path is None and _latest_failure_was_quota(diagnostics()):
            return WorkflowStage.GENERATE_SPEC
        return None

    def _assert_continuation_eligible(self, execution: WorkflowExecution) -> WorkflowStage:
        list_diagnostics = getattr(self._repository, "list_diagnostics", None)
        stage = self.continuation_stage(
            execution,
            lambda: list_diagnostics(execution.id) if callable(list_diagnostics) else [],
        )
        if stage is not None:
            return stage
        raise WorkflowError(
            "Only CHANGES_REQUESTED executions, FAILED executions with a retained "
            "workspace and branch, or executions whose planning hit a provider quota "
            "can be continued"
        )

    def _resume_planning(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        repository_context_strategy: RepositoryContextStrategy,
    ) -> WorkflowExecution:
        return self._sequencer._resume_planning(
            execution=execution,
            task=task,
            config=config,
            clients=clients,
            repository_slug=repository_slug,
            repository_context_strategy=repository_context_strategy,
        )

    def _resume_review(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        repository_context_strategy: RepositoryContextStrategy,
    ) -> WorkflowExecution:
        return self._sequencer._resume_review(
            execution=execution,
            task=task,
            config=config,
            clients=clients,
            repository_slug=repository_slug,
            repository_context_strategy=repository_context_strategy,
        )

    def _implement_and_review(
        self,
        execution_id: str,
        task: DevelopmentTask,
        spec: RepositorySpec,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        workspace_path: Path,
        working_branch: str,
        repository_context_strategy: RepositoryContextStrategy,
        continuation_feedback: str | None = None,
        force_polish: bool = False,
    ) -> WorkflowExecution:
        return self._sequencer._implement_and_review(
            execution_id=execution_id,
            task=task,
            spec=spec,
            config=config,
            clients=clients,
            repository_slug=repository_slug,
            workspace_path=workspace_path,
            working_branch=working_branch,
            repository_context_strategy=repository_context_strategy,
            continuation_feedback=continuation_feedback,
            force_polish=force_polish,
        )

    def _commit_changes(
        self,
        execution_id: str,
        execution_clients: WorkflowClients,
        task: DevelopmentTask,
        working_branch: str,
        config: ProjectConfig,
    ) -> None:
        self._sequencer._commit_changes(
            execution_id, execution_clients, task, working_branch, config
        )

    def _record_quota_failure(self, execution_id: str, exc: Exception) -> None:
        if not isinstance(exc, AgentProviderError) or exc.category != "quota":
            return
        if not exc.provider_name:
            return
        try:
            stage = self._repository.get_execution(execution_id).current_stage
        except Exception:
            stage = None
        stage_roles = {
            WorkflowStage.GENERATE_SPEC: AgentRole.PLANNER,
            WorkflowStage.IMPLEMENT: AgentRole.IMPLEMENTER,
            WorkflowStage.POLISH: AgentRole.IMPLEMENTER,
            WorkflowStage.REVIEW: AgentRole.REVIEWER,
        }
        role = stage_roles.get(stage) if stage is not None else None
        payload = {"kind": _QUOTA_EXHAUSTED_DIAGNOSTIC_KIND, "provider": exc.provider_name}
        if role is not None:
            payload["role"] = role.value
        self._record_diagnostic(
            execution_id, DiagnosticCategory.WORKFLOW_EVENT, payload, stage=stage, role=role
        )

    def _pause_for_human(
        self,
        execution_id: str,
        stage: WorkflowStage,
        prompt: str,
        config: ProjectConfig | None,
    ) -> WorkflowExecution:
        if self._repository.cancellation_requested(execution_id):
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        self._repository.record_error(execution_id, prompt)
        self._repository.request_human_input(execution_id, stage, prompt)
        return self._finish(execution_id, WorkflowStatus.NEEDS_HUMAN, config)

    def _record_runtime_overrides(
        self,
        execution_id: str,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, object]],
    ) -> None:
        """Durably persist validated one-run selections for restart-safe resume.

        Unlike ordinary diagnostics this record is load-bearing: human resume and
        manual continuation rebuild the role-local provider/model/effort choices
        from it. It is written before any agent starts, and a failed or lossy
        write stops the launch with an explicit error instead of letting a later
        resume silently fall back to YAML defaults.
        """

        providers = dict(provider_overrides)
        options = {role: dict(values) for role, values in runtime_overrides.items()}
        failure = (
            "Could not persist the selected agent runtime overrides "
            "(provider/model/effort); the run was stopped before any agent started "
            "so a later resume cannot silently use different settings. "
            "Retry the launch."
        )
        diagnostic = ExecutionDiagnostic(
            execution_id=execution_id,
            category=DiagnosticCategory.WORKFLOW_EVENT,
            payload={
                "kind": _RUNTIME_OVERRIDES_DIAGNOSTIC_KIND,
                "provider_overrides": providers,
                "runtime_overrides": options,
            },
            stage=WorkflowStage.LOAD_CONFIG,
        )
        try:
            self._repository.record_diagnostic(diagnostic)
            persisted = self._runtime_overrides_for_execution(execution_id)
        except Exception as exc:
            logger.warning("[workflow] Could not persist runtime overrides", exc_info=True)
            raise WorkflowError(failure) from exc
        expected = json.loads(json.dumps((providers, options), default=str))
        if json.loads(json.dumps(persisted, default=str)) != expected:
            raise WorkflowError(failure)

    def _runtime_overrides_for_execution(
        self, execution_id: str
    ) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
        """Return the launch selections recorded for a resumable execution."""

        list_diagnostics = getattr(self._repository, "list_diagnostics", None)
        if not callable(list_diagnostics):
            return {}, {}
        for diagnostic in reversed(list_diagnostics(execution_id)):
            payload = diagnostic.payload
            if payload.get("kind") != _RUNTIME_OVERRIDES_DIAGNOSTIC_KIND:
                continue
            raw_providers = payload.get("provider_overrides")
            raw_options = payload.get("runtime_overrides")
            if not isinstance(raw_providers, Mapping) or not isinstance(raw_options, Mapping):
                raise WorkflowError("Persisted runtime overrides are invalid")
            providers = {
                role: provider
                for role, provider in raw_providers.items()
                if isinstance(role, str) and isinstance(provider, str)
            }
            options = {
                role: dict(value)
                for role, value in raw_options.items()
                if isinstance(role, str) and isinstance(value, Mapping)
            }
            if len(providers) != len(raw_providers) or len(options) != len(raw_options):
                raise WorkflowError("Persisted runtime overrides are invalid")
            return providers, options
        return {}, {}

    @staticmethod
    def _planner_prompt(spec: RepositorySpec) -> str:
        questions = "\n".join(f"- {question}" for question in spec.ambiguities)
        return questions or "Planner reported blocking ambiguity"

    @staticmethod
    def _review_prompt(review: CodeReview) -> str:
        findings = review.blocking_findings or review.findings
        details = "\n".join(f"- {finding}" for finding in findings)
        return (
            "\n".join(part for part in [review.summary, details] if part)
            or "Reviewer needs clarification"
        )

    @staticmethod
    def _task_from_snapshot(execution: WorkflowExecution) -> DevelopmentTask:
        if not execution.task_snapshot:
            raise WorkflowError("Cannot resume: the original task was not persisted")
        return DevelopmentTask(**execution.task_snapshot)

    @staticmethod
    def _task_with_responses(
        task: DevelopmentTask, interactions: list[tuple[str, str]]
    ) -> DevelopmentTask:
        clarification = "\n\n".join(
            f"Question/context:\n{prompt}\n\nResponse:\n{response}"
            for prompt, response in interactions
        )
        return DevelopmentTask(
            **{
                **asdict(task),
                "description": (
                    f"{task.description}\n\nHUMAN CLARIFICATION FOR THIS EXECUTION:\n"
                    f"{clarification}"
                ),
            }
        )

    @staticmethod
    def _validation_from_snapshot(snapshot: dict[str, object] | None) -> ValidationResult:
        if not snapshot:
            raise WorkflowError("Cannot resume review: persisted validation is missing")
        raw_checks = snapshot.get("checks", [])
        if not isinstance(raw_checks, list):
            raise WorkflowError("Cannot resume review: persisted validation is invalid")
        raw_error_message = snapshot.get("error_message")
        return ValidationResult(
            success=bool(snapshot.get("success")),
            checks=[ValidationCheck(**check) for check in raw_checks if isinstance(check, dict)],
            error_message=raw_error_message if isinstance(raw_error_message, str) else None,
        )

    def _finish(
        self,
        execution_id: str,
        status: WorkflowStatus,
        config: ProjectConfig | None,
    ) -> WorkflowExecution:
        if status is not WorkflowStatus.CANCELLED:
            try:
                self._raise_if_cancellation_requested(execution_id)
            except WorkflowCancelledError:
                status = WorkflowStatus.CANCELLED
        self._repository.finish(execution_id, status)
        execution = self._repository.get_execution(execution_id)
        status = execution.status
        self._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=execution.current_stage,
            status=status,
        )
        self._emit(
            execution_id,
            WorkflowEventKind.WORKFLOW_FINISHED,
            stage=execution.current_stage,
            status=status,
        )
        if config is not None:
            self._notification_service.notify_outcome(config.notifications, execution)
        return execution

    def _step(self, execution_id: str, stage: WorkflowStage) -> AbstractContextManager[None]:
        return self._stage_events._step(execution_id, stage)

    def _raise_if_cancellation_requested(self, execution_id: str) -> None:
        if self._repository.cancellation_requested(execution_id):
            raise WorkflowCancelledError("Cancellation requested by user")

    def _diagnostic_agent(
        self,
        provider: AgentProvider,
        config: ProjectConfig,
        role: AgentRole,
        execution_id: str,
    ) -> AgentProvider:
        configured = getattr(config.agents, role.value).provider
        provider_settings = config.providers[configured].model_dump()
        fallback_preset_name = getattr(config.agents, role.value).fallback_preset
        primary_provider_name = configured
        primary_preset_name: str | None = None
        if fallback_preset_name is not None:
            for preset_name, preset in config.presets_for_role(role.value).items():
                if not configured.startswith("__tui_runtime_") and preset.provider != configured:
                    continue
                try:
                    candidate = config.with_agent_preset(role.value, preset_name)
                except ConfigurationError:
                    continue
                candidate_name = getattr(candidate.agents, role.value).provider
                if candidate.providers[candidate_name].model_dump() == provider_settings:
                    primary_provider_name = preset.provider
                    primary_preset_name = preset_name
                    break
        primary = self._admitted_agent(
            DiagnosticAgentProvider(
                provider,
                role=role,
                provider_config={
                    "name": primary_provider_name,
                    "preset": primary_preset_name,
                    **provider_settings,
                },
                record=lambda category, event_role, payload: self._record_diagnostic(
                    execution_id,
                    category,
                    payload,
                    stage=self._active_stage_of(execution_id),
                    role=event_role,
                ),
                cancellation_check=lambda: self._raise_if_cancellation_requested(execution_id),
            ),
            config=config,
            provider_name=configured,
            execution_id=execution_id,
        )
        if fallback_preset_name is None:
            return primary

        fallback_preset = config.presets_for_role(role.value)[fallback_preset_name]
        fallback_config = config.with_agent_preset(role.value, fallback_preset_name)
        fallback_configured = getattr(fallback_config.agents, role.value).provider
        fallback_settings = fallback_config.providers[fallback_configured].model_dump()
        primary_values = config.provider_runtime_values(configured)
        fallback_values = fallback_config.provider_runtime_values(fallback_configured)

        def create_fallback() -> AgentProvider:
            fallback_provider = self._client_factory.create(
                fallback_config, agent_roles={role}
            ).agent(role)
            # The fallback is charged to its own configured provider name; the
            # primary's slot is already released by the time this runs.
            return self._admitted_agent(
                DiagnosticAgentProvider(
                    fallback_provider,
                    role=role,
                    provider_config={
                        "name": fallback_preset.provider,
                        "preset": fallback_preset_name,
                        **fallback_settings,
                    },
                    record=lambda category, event_role, payload: self._record_diagnostic(
                        execution_id,
                        category,
                        payload,
                        stage=self._active_stage_of(execution_id),
                        role=event_role,
                    ),
                    cancellation_check=lambda: self._raise_if_cancellation_requested(execution_id),
                ),
                config=fallback_config,
                provider_name=fallback_configured,
                execution_id=execution_id,
            )

        def record_fallback(error: AgentProviderError) -> None:
            payload = {
                "role": role.value,
                "reason": error.category,
                "primary_provider": primary_provider_name,
                "primary_preset": primary_preset_name,
                "primary_model": primary_values.get("model"),
                "fallback_provider": fallback_preset.provider,
                "fallback_preset": fallback_preset_name,
                "fallback_model": fallback_values.get("model"),
            }
            logger.warning(
                "[workflow] %s agent capacity failure; using fallback %s -> %s/%s",
                role.value,
                primary_provider_name,
                fallback_preset.provider,
                fallback_preset_name,
            )
            self._emit(
                execution_id,
                WorkflowEventKind.AGENT_FALLBACK,
                stage=self._active_stage_of(execution_id),
                message=(
                    f"{role.value.title()} switched from {primary_provider_name} to "
                    f"{fallback_preset.provider}/{fallback_preset_name}"
                ),
                payload=payload,
            )

        return FallbackAgentProvider(
            primary, fallback_factory=create_fallback, on_fallback=record_fallback
        )

    def _admitted_agent(
        self,
        provider: AgentProvider,
        *,
        config: ProjectConfig,
        provider_name: str,
        execution_id: str,
    ) -> AgentProvider:
        """Bound one agent call by its provider's shared concurrency ceiling.

        The slot is acquired immediately before the call and released right
        after it, so a workflow never reserves a provider while it is doing
        Git, validation, or GitHub work. Role-local runtime variants are
        charged to the configured provider they were derived from.
        """

        concurrency_name = config.provider_concurrency_name(provider_name)
        return ConcurrencyLimitedAgentProvider(
            provider,
            provider_name=concurrency_name,
            coordinator=self._provider_coordinator,
            limit=config.provider_max_concurrency(provider_name),
            execution_id=execution_id,
            on_wait=lambda name: self._record_provider_wait(execution_id, name),
            on_admitted=lambda name: self._record_provider_admission(execution_id, name),
            cancellation_check=lambda: self._raise_if_cancellation_requested(execution_id),
        )

    def _record_provider_wait(self, execution_id: str, provider_name: str) -> None:
        """Persist that started work is blocked on shared provider capacity."""

        stage = self._active_stage_of(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.WAITING)
        self._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=stage,
            status=WorkflowStatus.WAITING,
            message=f"Waiting for {provider_name} capacity",
            payload={"kind": _PROVIDER_WAIT_DIAGNOSTIC_KIND, "provider": provider_name},
        )

    def _record_provider_admission(self, execution_id: str, provider_name: str) -> None:
        stage = self._active_stage_of(execution_id)
        self._repository.update_status(execution_id, WorkflowStatus.RUNNING)
        self._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            stage=stage,
            status=WorkflowStatus.RUNNING,
            message=f"Admitted to {provider_name}",
            payload={"kind": _PROVIDER_ADMITTED_DIAGNOSTIC_KIND, "provider": provider_name},
        )

    def _record_validation_check(self, execution_id: str, payload: dict[str, Any]) -> None:
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.VALIDATION,
            payload,
            stage=WorkflowStage.VALIDATE,
        )
        self._raise_if_cancellation_requested(execution_id)

    def _record_diagnostic(
        self,
        execution_id: str,
        category: DiagnosticCategory,
        payload: dict[str, Any],
        *,
        stage: WorkflowStage | None = None,
        role: AgentRole | None = None,
        notify: bool = True,
    ) -> None:
        self._stage_events._record_diagnostic(
            execution_id=execution_id,
            category=category,
            payload=payload,
            stage=stage,
            role=role,
            notify=notify,
        )

    def _emit(
        self,
        execution_id: str,
        kind: WorkflowEventKind,
        *,
        stage: WorkflowStage | None = None,
        status: WorkflowStatus | None = None,
        step_status: StepStatus | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._stage_events._emit(
            execution_id=execution_id,
            kind=kind,
            stage=stage,
            status=status,
            step_status=step_status,
            message=message,
            payload=payload,
        )

    def _notify_event(self, event: WorkflowEvent) -> None:
        self._stage_events._notify_event(
            event=event,
        )

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
        return self._corrections._validate_with_corrections(
            execution_id=execution_id,
            execution_clients=execution_clients,
            implementation_service=implementation_service,
            task=task,
            spec=spec,
            workspace_path=workspace_path,
            config=config,
        )

    @staticmethod
    def _failing_command_diagnostic(check: ValidationCheck) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {
            "command": check.command,
            "exit_code": check.exit_code,
            "stdout": check.stdout,
            "stderr": check.stderr,
        }
        if check.timed_out:
            diagnostic["timed_out"] = True
            diagnostic["timeout_seconds"] = check.timeout_seconds
        return diagnostic

    def _validate_polish_changes(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        workspace_path: Path,
        config: ProjectConfig,
    ) -> ValidationResult | None:
        return self._corrections._validate_polish_changes(
            execution_id=execution_id,
            execution_clients=execution_clients,
            workspace_path=workspace_path,
            config=config,
        )

    def _evaluate_optional_polish(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        implementation_service: ImplementationService,
        task: DevelopmentTask,
        spec: RepositorySpec,
        suggestions: list[str],
        workspace_path: Path,
        pull_request: PullRequest,
        working_branch: str,
        config: ProjectConfig,
        force: bool = False,
        before_push: Callable[[], None] | None = None,
    ) -> tuple[PolishOutcome, ValidationResult | None]:
        return self._corrections._evaluate_optional_polish(
            execution_id=execution_id,
            execution_clients=execution_clients,
            implementation_service=implementation_service,
            task=task,
            spec=spec,
            suggestions=suggestions,
            workspace_path=workspace_path,
            pull_request=pull_request,
            working_branch=working_branch,
            config=config,
            force=force,
            before_push=before_push,
        )

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

    def _record_skipped_polish(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        pull_request_number: int,
        implementation: ImplementationResult,
        fallback_reason: str | None = None,
    ) -> None:
        self._repository.record_polish_outcome(execution_id, PolishOutcome.SKIPPED)
        self._publish_polish_skip_comment(
            execution_id=execution_id,
            execution_clients=execution_clients,
            pull_request_number=pull_request_number,
            implementation=implementation,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _discard_polish_changes(execution_clients: WorkflowClients) -> None:
        execution_clients.git.discard_uncommitted_changes()

    def _publish_review(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        repository_slug: str,
        pull_request_number: int,
        review: CodeReview,
    ) -> None:
        event = {
            ReviewDecision.APPROVED: "APPROVE",
            ReviewDecision.CHANGES_REQUESTED: "REQUEST_CHANGES",
            ReviewDecision.NEEDS_HUMAN: "COMMENT",
        }[review.result]
        try:
            execution_clients.github.submit_pull_request_review(
                repository=repository_slug,
                pull_request_number=pull_request_number,
                event=event,
                body=format_pull_request_review(review),
            )
        except Exception as exc:
            message = f"GitHub review publication failed: {redact_text(str(exc))}"
            logger.warning("[github] %s", message)
            # Publication is an external side effect. Preserve the reviewer decision and let
            # the terminal workflow status reflect it even when GitHub is unavailable.
            self._repository.record_error(execution_id, message)

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

    @staticmethod
    def _format_polish_skip_comment(
        implementation: ImplementationResult, fallback_reason: str | None = None
    ) -> str:
        """Render a concise, safe explanation for an intentionally skipped polish pass."""
        lines = [
            "## Optional polish",
            "",
            "The non-blocking review suggestions were evaluated and intentionally skipped.",
            "",
        ]
        if implementation.polish_skip_reasons:
            lines.extend(f"- {reason}" for reason in implementation.polish_skip_reasons)
        else:
            reason = fallback_reason or "No small, safe, in-scope code change was warranted."
            lines.append(f"- {reason}")
        return "\n".join(lines)

    def _publish_polish_skip_comment(
        self,
        *,
        execution_id: str,
        execution_clients: WorkflowClients,
        pull_request_number: int,
        implementation: ImplementationResult,
        fallback_reason: str | None = None,
    ) -> None:
        execution = self._repository.get_execution(execution_id)
        if execution.polish_skip_comment_attempted:
            logger.info("[github] Optional polish skip comment was already attempted")
            return
        # Persist before the external side effect so a retry after a process interruption cannot
        # publish a second timeline comment. Publication itself remains deliberately non-fatal.
        self._repository.record_polish_skip_comment_attempt(execution_id)
        try:
            execution_clients.github.comment_pull_request(
                pr_number=pull_request_number,
                body=self._format_polish_skip_comment(implementation, fallback_reason),
            )
        except Exception as exc:
            # A skip explanation is audit context, not a new review decision. Keep the existing
            # approval intact if GitHub is temporarily unavailable.
            logger.warning(
                "[github] Optional polish skip comment publication failed: %s",
                redact_text(str(exc)),
            )

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
