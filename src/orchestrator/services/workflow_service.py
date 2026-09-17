from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
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
    ) -> None:
        self._repository = workflow_repository
        self._task_provider = task_provider
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._verbose = verbose
        self._notification_service = notification_service or NotificationService()
        self._event_listener: WorkflowEventListener | None = None
        self._active_stage: WorkflowStage | None = None

    def cancel_execution(self, execution_id: str) -> bool:
        """Request cancellation without coupling the caller to a provider.

        Providers remain responsible for any interruption mechanism they can
        safely offer. The workflow itself observes this persisted request at
        stage boundaries, so a request made by another process is restart-safe.
        """
        accepted = self._repository.request_cancellation(execution_id)
        if accepted:
            self._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {
                    "kind": "CANCELLATION_REQUESTED",
                    "message": "Cancellation requested by user",
                },
                stage=self._active_stage,
                notify=False,
            )
        return accepted

    def run(
        self,
        task_id: str,
        repository_override: str | None = None,
        *,
        agent_providers: Mapping[AgentRole, str] | None = None,
        agent_runtime_options: Mapping[AgentRole, Mapping[str, object]] | None = None,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Run a workflow using the same persisted transitions used by the CLI.

        ``event_listener`` is an observational hook for interactive clients. It
        is called after the corresponding repository update and failures in the
        listener are isolated from workflow execution.
        """

        self._event_listener = event_listener
        execution = self._repository.create_execution(
            task_id, task_source=self._task_provider.source
        )
        self._emit(
            execution.id,
            WorkflowEventKind.EXECUTION_CREATED,
            stage=execution.current_stage,
            status=execution.status,
        )
        self._repository.update_status(execution.id, WorkflowStatus.RUNNING)
        self._emit(execution.id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING)
        provider_overrides = {
            role.value: provider for role, provider in (agent_providers or {}).items()
        }
        runtime_overrides = {
            role.value: dict(options)
            for role, options in (agent_runtime_options or {}).items()
        }
        workspace_path: Path | None = None
        config: ProjectConfig | None = None
        try:
            logger.info(
                "[workflow] Loading %s task %s", self._task_provider.source, task_id
            )
            with self._step(execution.id, WorkflowStage.LOAD_TASK):
                task = self._task_provider.get_task(task_id)
                self._repository.update_task(execution.id, task)

            with self._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._config_loader.load(task, repository_override)
                if provider_overrides or runtime_overrides:
                    config = config.with_agent_runtime_overrides(
                        provider_overrides,
                        runtime_overrides,
                    )
                    self._record_runtime_overrides(
                        execution.id, provider_overrides, runtime_overrides
                    )
                self._repository.update_project(
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
                clients = self._client_factory.create(config)
                repository_slug = clients.git.github_repository()
                repository_context_strategy = resolve_repository_context_strategy(
                    config.repository_context.strategy,
                    settings=config.repository_context,
                    repository_key=config.configured_repository_key,
                    configured_root=config.path,
                )

            with self._step(execution.id, WorkflowStage.GENERATE_SPEC):
                spec = SpecService(
                    self._diagnostic_agent(
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
                    lambda reference: self._repository.record_spec_jules_session(
                        execution.id, reference.external_id, reference.url
                    ),
                )
                self._repository.record_spec(execution.id, spec)
                if self._verbose:
                    log_spec_summary(logger, spec)
            if spec.has_blocking_ambiguity:
                return self._pause_for_human(
                    execution.id,
                    WorkflowStage.GENERATE_SPEC,
                    self._planner_prompt(spec),
                    config,
                )

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
                self._diagnostic_agent(
                    execution_clients.agent(AgentRole.IMPLEMENTER),
                    config,
                    AgentRole.IMPLEMENTER,
                    execution.id,
                ),
                execution_clients.git,
            )
            with self._step(execution.id, WorkflowStage.IMPLEMENT):
                implementation = implementation_service.implement(
                    task,
                    spec,
                    workspace_path,
                    instructions_repository_path=config.path,
                )
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
                self._assign_implementer(
                    execution_clients=execution_clients,
                    pull_request_number=pull_request.number,
                    workspace_path=workspace_path,
                )

            review_attempt = 0
            polish_attempted = execution.polish_outcome is not None
            while True:
                with self._step(execution.id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._diagnostic_agent(
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
                            self._repository.record_review_jules_session(
                                execution.id, reference.external_id, reference.url
                            )
                        ),
                    )
                    self._repository.record_review(execution.id, review)
                    if self._verbose:
                        log_review_summary(logger, review)
                    if review.result is ReviewDecision.NEEDS_HUMAN:
                        self._repository.record_error(execution.id, self._review_prompt(review))
                    self._publish_review(
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
                    polish_outcome, polish_validation = self._evaluate_optional_polish(
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
                with self._step(execution.id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_review_findings(
                        task,
                        spec,
                        review,
                        workspace_path,
                        instructions_repository_path=config.path,
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
            if final_status is WorkflowStatus.NEEDS_HUMAN:
                self._repository.request_human_input(
                    execution.id, WorkflowStage.REVIEW, self._review_prompt(review)
                )
            return self._finish(execution.id, final_status, config)
        except WorkflowCancelledError:
            result = self._finish(execution.id, WorkflowStatus.CANCELLED, config)
            if workspace_path is not None:
                logger.info(
                    "[workspace] Workflow cancelled; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except ConfigurationError as exc:
            return self._pause_for_human(
                execution.id,
                self._repository.get_execution(execution.id).current_stage,
                str(exc),
                config,
            )
        except AgentNeedsHumanError as exc:
            stage = self._repository.get_execution(execution.id).current_stage
            result = self._pause_for_human(execution.id, stage, str(exc), config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow needs human input; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except Exception as exc:
            self._repository.record_error(execution.id, str(exc))
            result = self._finish(execution.id, WorkflowStatus.FAILED, config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow failed; preserving workspace: %s", workspace_path
                )
            return result

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
        self._event_listener = event_listener
        execution = self._repository.get_execution(execution_id)
        if execution.status is not WorkflowStatus.NEEDS_HUMAN:
            raise ValueError(f"Execution {execution_id} is not awaiting human input")
        interaction = self._repository.respond_to_human_input(execution_id, response)
        self._repository.resume_execution(execution_id)
        self._emit(
            execution_id,
            WorkflowEventKind.STATUS_CHANGED,
            status=WorkflowStatus.RUNNING,
        )
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
                config = config.with_agent_runtime_overrides(
                    provider_overrides, runtime_overrides
                )
            clients = self._client_factory.create(config)
            repository_slug = clients.git.github_repository()
            repository_context_strategy = resolve_repository_context_strategy(
                config.repository_context.strategy,
                settings=config.repository_context,
                repository_key=config.configured_repository_key,
                configured_root=config.path,
            )
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
            return self._finish(execution_id, WorkflowStatus.FAILED, config)

    def continuation_feedback(self, execution_id: str) -> str:
        """Return the editable feedback shown before a terminal execution is continued."""
        execution = self._repository.get_execution(execution_id)
        self._assert_continuation_eligible(execution)
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

    def continue_execution(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: WorkflowEventListener | None = None,
    ) -> WorkflowExecution:
        """Manually continue an eligible terminal execution in its retained workspace."""
        if not feedback.strip():
            raise ValueError("Continuation feedback must not be empty")
        self._event_listener = event_listener
        execution = self._repository.get_execution(execution_id)
        self._assert_continuation_eligible(execution)
        task = self._task_from_snapshot(execution)
        if not execution.spec_snapshot:
            raise WorkflowError("Cannot continue: the original repository spec was not persisted")
        assert execution.workspace_path is not None and execution.working_branch is not None
        workspace_path = Path(execution.workspace_path)
        config = self._config_loader.load(
            task, execution.repository_ref or execution.repository or None
        )
        provider_overrides, runtime_overrides = self._runtime_overrides_for_execution(
            execution_id
        )
        if provider_overrides or runtime_overrides:
            config = config.with_agent_runtime_overrides(provider_overrides, runtime_overrides)
        clients = self._client_factory.create(config)
        execution_clients = clients.at_path(workspace_path)
        # Do this before changing status or contacting the implementer: a human
        # can safely correct a missing/moved worktree and try again.
        execution_clients.git.verify_worktree_branch(execution.working_branch, execution.commit_sha)
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            {"kind": "MANUAL_CONTINUATION", "feedback": feedback},
            stage=WorkflowStage.IMPLEMENT,
        )
        self._repository.resume_execution(execution_id)
        self._emit(execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING)
        repository_slug = clients.git.github_repository()
        repository_context_strategy = resolve_repository_context_strategy(
            config.repository_context.strategy,
            settings=config.repository_context,
            repository_key=config.configured_repository_key,
            configured_root=config.path,
        )
        return self._implement_and_review(
            execution_id,
            task,
            RepositorySpec(**execution.spec_snapshot),
            config,
            clients,
            repository_slug,
            workspace_path,
            execution.working_branch,
            repository_context_strategy,
            continuation_feedback=feedback,
            force_polish=True,
        )

    @staticmethod
    def _assert_continuation_eligible(execution: WorkflowExecution) -> None:
        eligible_failed = (
            execution.status is WorkflowStatus.FAILED
            and execution.workspace_path is not None
            and execution.working_branch is not None
        )
        if execution.status is WorkflowStatus.CHANGES_REQUESTED or eligible_failed:
            return
        raise WorkflowError(
            "Only CHANGES_REQUESTED executions, or FAILED executions with a retained "
            "workspace and branch, can be continued"
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
        """Repeat only planning, then continue through a newly safe workspace."""
        with self._step(execution.id, WorkflowStage.GENERATE_SPEC):
            spec = SpecService(
                self._diagnostic_agent(
                    clients.agent(AgentRole.PLANNER), config, AgentRole.PLANNER, execution.id
                ),
                repository_context_strategy,
            ).generate(
                task,
                config.path,
                repository_slug,
                config.repository.base_branch,
                lambda reference: self._repository.record_spec_jules_session(
                    execution.id, reference.external_id, reference.url
                ),
            )
            self._repository.record_spec(execution.id, spec)
        if spec.has_blocking_ambiguity:
            return self._pause_for_human(
                execution.id, WorkflowStage.GENERATE_SPEC, self._planner_prompt(spec), config
            )

        working_branch = self._branch_name(
            config.repository.branch_prefix, task.id, task.title, execution.id
        )
        workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
        with self._step(execution.id, WorkflowStage.PREPARE_BRANCH):
            workspace_path = workspace_manager.workspace_path(execution.id)
            self._repository.record_workspace(execution.id, str(workspace_path))
            workspace_path = workspace_manager.create(
                execution.id,
                config.repository.base_branch,
                working_branch,
                config.repository.protected_branches,
            )
            self._repository.record_branch(execution.id, working_branch)
        return self._implement_and_review(
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
            raise WorkflowError(
                f"Cannot resume review: workspace is unavailable: {workspace_path}"
            )
        if not execution.spec_snapshot or not execution.pull_request_snapshot:
            raise WorkflowError("Cannot resume review: persisted spec or pull request is missing")
        spec = RepositorySpec(**execution.spec_snapshot)
        pull_request = PullRequest(**execution.pull_request_snapshot)
        validation = self._validation_from_snapshot(execution.validation_snapshot)
        execution_clients = clients.at_path(workspace_path)
        implementation_service = ImplementationService(
            self._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution.id,
            ),
            execution_clients.git,
        )
        polish_attempted = execution.polish_outcome is not None
        while True:
            with self._step(execution.id, WorkflowStage.REVIEW):
                review = ReviewService(
                    self._diagnostic_agent(
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
                        self._repository.record_review_jules_session(
                            execution.id, reference.external_id, reference.url
                        )
                    ),
                )
                self._repository.record_review(execution.id, review)
                self._publish_review(
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
                polish_outcome, polish_validation = self._evaluate_optional_polish(
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
            self._repository.request_human_input(
                execution.id, WorkflowStage.REVIEW, self._review_prompt(review)
            )
        return self._finish(execution.id, status, config)

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
            self._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution_id,
            ),
            execution_clients.git,
        )
        try:
            with self._step(execution_id, WorkflowStage.IMPLEMENT):
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
                self._repository.record_implementation(execution_id, implementation)
                if not implementation.success:
                    raise WorkflowError(
                        implementation.error_message or "Implementation provider failed"
                    )
            validation = self._validate_with_corrections(
                execution_id=execution_id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )
            with self._step(execution_id, WorkflowStage.COMMIT):
                commit_sha = execution_clients.git.commit_all(
                    self._commit_message(task),
                    working_branch,
                    config.repository.protected_branches,
                )
                self._repository.record_commit(execution_id, commit_sha)
            with self._step(execution_id, WorkflowStage.PUSH):
                pull_request_snapshot = self._repository.get_execution(
                    execution_id
                ).pull_request_snapshot
                if pull_request_snapshot:
                    existing_pr = PullRequest(**pull_request_snapshot)
                    self._assert_pull_request_open(
                        execution_clients, repository_slug, existing_pr.number
                    )
                execution_clients.git.push(
                    working_branch, config.repository.protected_branches
                )
            if pull_request_snapshot:
                pull_request = PullRequest(**pull_request_snapshot)
            else:
                with self._step(execution_id, WorkflowStage.CREATE_PR):
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
                    self._repository.record_pull_request(execution_id, pull_request)
                    self._assign_implementer(
                        execution_clients=execution_clients,
                        pull_request_number=pull_request.number,
                        workspace_path=workspace_path,
                    )
            polish_attempted = not force_polish and (
                self._repository.get_execution(execution_id).polish_outcome is not None
            )
            review_attempt = 0
            while True:
                with self._step(execution_id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._diagnostic_agent(
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
                            self._repository.record_review_jules_session(
                                execution_id, reference.external_id, reference.url
                            )
                        ),
                    )
                    self._repository.record_review(execution_id, review)
                    self._publish_review(
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
                    polish_outcome, polish_validation = self._evaluate_optional_polish(
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
                        before_push=lambda: self._assert_pull_request_open(
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
                    with self._step(execution_id, WorkflowStage.IMPLEMENT):
                        implementation = implementation_service.correct_review_findings(
                            task,
                            spec,
                            review,
                            workspace_path,
                            instructions_repository_path=config.path,
                        )
                        self._repository.record_implementation(execution_id, implementation)
                        if not implementation.success:
                            raise WorkflowError(
                                implementation.error_message or "Review correction provider failed"
                            )
                    validation = self._validate_with_corrections(
                        execution_id=execution_id,
                        execution_clients=execution_clients,
                        implementation_service=implementation_service,
                        task=task,
                        spec=spec,
                        workspace_path=workspace_path,
                        config=config,
                    )
                    with self._step(execution_id, WorkflowStage.COMMIT):
                        commit_sha = execution_clients.git.commit_all(
                            self._commit_message(task),
                            working_branch,
                            config.repository.protected_branches,
                        )
                        self._repository.record_commit(execution_id, commit_sha)
                    with self._step(execution_id, WorkflowStage.PUSH):
                        self._assert_pull_request_open(
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
                self._repository.request_human_input(
                    execution_id, WorkflowStage.REVIEW, self._review_prompt(review)
                )
            return self._finish(execution_id, status, config)
        except WorkflowCancelledError:
            return self._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._pause_for_human(
                execution_id, WorkflowStage.IMPLEMENT, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            return self._finish(execution_id, WorkflowStatus.FAILED, config)

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
            checks=[
                ValidationCheck(**check) for check in raw_checks if isinstance(check, dict)
            ],
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

    @contextmanager
    def _step(self, execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        self._raise_if_cancellation_requested(execution_id)
        label = _STAGE_LABELS.get(stage, stage.value.casefold().replace("_", " "))
        previous_stage = self._active_stage
        self._active_stage = stage
        logger.info("[workflow] Entering %s", label)
        self._repository.update_stage(execution_id, stage)
        step_id = self._repository.start_step(execution_id, stage)
        self._emit(
            execution_id,
            WorkflowEventKind.STAGE_STARTED,
            stage=stage,
            step_status=StepStatus.RUNNING,
        )
        try:
            yield
            self._raise_if_cancellation_requested(execution_id)
        except WorkflowCancelledError as exc:
            self._repository.finish_step(step_id, StepStatus.CANCELLED, str(exc))
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.CANCELLED,
                message=str(exc),
            )
            logger.info("[workflow] %s cancelled", label)
            raise
        except Exception as exc:
            self._repository.finish_step(step_id, StepStatus.FAILED, str(exc))
            self._record_diagnostic(
                execution_id,
                DiagnosticCategory.ERROR,
                {
                    "operation": "workflow_stage",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                stage=stage,
            )
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FAILED,
                stage=stage,
                step_status=StepStatus.FAILED,
                message=redact_text(str(exc)),
            )
            logger.error("[workflow] %s failed: %s", label, redact_text(str(exc)))
            raise
        else:
            self._repository.finish_step(step_id, StepStatus.SUCCEEDED)
            self._emit(
                execution_id,
                WorkflowEventKind.STAGE_FINISHED,
                stage=stage,
                step_status=StepStatus.SUCCEEDED,
            )
            logger.info("[workflow] Completed %s", label)
        finally:
            self._active_stage = previous_stage

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
        primary = DiagnosticAgentProvider(
            provider,
            role=role,
            provider_config={
                "name": primary_provider_name,
                "preset": primary_preset_name,
                **provider_settings,
            },
            record=lambda category, event_role, payload: self._record_diagnostic(
                execution_id, category, payload, stage=self._active_stage, role=event_role
            ),
            cancellation_check=lambda: self._raise_if_cancellation_requested(execution_id),
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
            return DiagnosticAgentProvider(
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
                    stage=self._active_stage,
                    role=event_role,
                ),
                cancellation_check=lambda: self._raise_if_cancellation_requested(
                    execution_id
                ),
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
                stage=self._active_stage,
                message=(
                    f"{role.value.title()} switched from {primary_provider_name} to "
                    f"{fallback_preset.provider}/{fallback_preset_name}"
                ),
                payload=payload,
            )

        return FallbackAgentProvider(
            primary, fallback_factory=create_fallback, on_fallback=record_fallback
        )

    def _record_validation_check(
        self, execution_id: str, payload: dict[str, Any]
    ) -> None:
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
            self._repository.record_diagnostic(diagnostic)
        except AttributeError:
            # Lightweight repository fakes used by integrations predating the
            # diagnostics table retain their existing workflow behavior.
            pass
        except Exception:
            logger.warning("[workflow] Could not persist diagnostic event", exc_info=True)
        if self._verbose:
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
            self._notify_event(
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
        self._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            event_payload,
            stage=stage,
            notify=False,
        )
        sanitized_payload = redact_payload(payload) if payload is not None else None
        self._notify_event(
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
        listener = self._event_listener
        if listener is None:
            return
        try:
            listener(event)
        except Exception:
            # A presentation observer is never allowed to change workflow
            # semantics or make a successfully persisted transition fail.
            logger.warning("[workflow] Event listener failed")

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
                        on_check_complete=lambda payload: self._record_validation_check(
                            execution_id, payload
                        ),
                    )
                    self._repository.record_validation(execution_id, validation)
                    if not validation.success:
                        self._record_diagnostic(
                            execution_id,
                            DiagnosticCategory.ERROR,
                            {
                                "operation": "validation",
                                "message": validation.error_message,
                                "failing_commands": [
                                    {
                                        "command": check.command,
                                        "exit_code": check.exit_code,
                                        "stdout": check.stdout,
                                        "stderr": check.stderr,
                                    }
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
                with self._step(execution_id, WorkflowStage.IMPLEMENT):
                    implementation = implementation_service.correct_validation_failures(
                        task,
                        spec,
                        exc.validation,
                        workspace_path,
                        instructions_repository_path=config.path,
                    )
                    self._repository.record_implementation(execution_id, implementation)
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
            with self._step(execution_id, WorkflowStage.VALIDATE):
                validation = ValidationService(execution_clients.commands).validate(
                    workspace_path,
                    config.validation,
                    require_tests=config.safety.require_tests,
                    on_check_complete=lambda payload: self._record_validation_check(
                        execution_id, payload
                    ),
                )
                self._repository.record_validation(execution_id, validation)
                if not validation.success:
                    self._record_diagnostic(
                        execution_id,
                        DiagnosticCategory.ERROR,
                        {
                            "operation": "optional_polish_validation",
                            "message": validation.error_message,
                            "failing_commands": [
                                {"command": check.command, "exit_code": check.exit_code,
                                 "stdout": check.stdout, "stderr": check.stderr}
                                for check in validation.checks if not check.success
                            ],
                        },
                        stage=WorkflowStage.VALIDATE,
                    )
                    raise _ValidationFailed(validation)
        except _ValidationFailed:
            logger.warning(
                "[workflow] Optional polish failed validation; discarding polish changes"
            )
            self._discard_polish_changes(execution_clients)
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
        persisted = self._repository.get_execution(execution_id).polish_outcome
        if persisted is not None and not force:
            return persisted, None

        with self._step(execution_id, WorkflowStage.POLISH):
            implementation = implementation_service.run_polish_pass(
                task,
                spec,
                suggestions,
                workspace_path,
                pull_request,
                instructions_repository_path=config.path,
            )
            self._repository.record_implementation(execution_id, implementation)

        if not implementation.success:
            logger.warning("[workflow] Optional polish did not complete; retaining approved review")
            self._discard_polish_changes(execution_clients)
            self._record_skipped_polish(
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
            self._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
            )
            return PolishOutcome.SKIPPED, None

        validation = self._validate_polish_changes(
            execution_id=execution_id,
            execution_clients=execution_clients,
            workspace_path=workspace_path,
            config=config,
        )
        if validation is None:
            self._record_skipped_polish(
                execution_id=execution_id,
                execution_clients=execution_clients,
                pull_request_number=pull_request.number,
                implementation=implementation,
                fallback_reason=(
                    "The optional changes did not pass validation, so no changes were applied."
                ),
            )
            return PolishOutcome.SKIPPED, None

        with self._step(execution_id, WorkflowStage.COMMIT):
            commit_sha = execution_clients.git.commit_all(
                self._commit_message(task),
                working_branch,
                config.repository.protected_branches,
            )
            self._repository.record_commit(execution_id, commit_sha)
        with self._step(execution_id, WorkflowStage.PUSH):
            if before_push is not None:
                before_push()
            execution_clients.git.push(working_branch, config.repository.protected_branches)

        self._repository.record_polish_outcome(execution_id, PolishOutcome.APPLIED)
        return PolishOutcome.APPLIED, validation

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
