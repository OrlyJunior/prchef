from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    PolishOutcome,
    ReviewDecision,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import log_review_summary, log_spec_summary
from orchestrator.services.exceptions import WorkflowCancelledError, WorkflowError
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.repository_context import (
    RepositoryContextStrategy,
    resolve_repository_context_strategy,
)
from orchestrator.services.review_service import ReviewService
from orchestrator.services.spec_service import SpecService
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.workflow_agent_runtime import WorkflowAgentRuntime
from orchestrator.services.workflow_correction_policy import CorrectionPolicy
from orchestrator.services.workflow_execution_lifecycle import ExecutionLifecycle
from orchestrator.services.workflow_stage_actions import StageActions
from orchestrator.services.workflow_stage_events import StageEvents
from orchestrator.services.workflow_stage_values import StageValues
from orchestrator.workspace.manager import WorkspaceManager

logger = logging.getLogger("orchestrator.services.workflow_service")


class StageSequencer:
    """Sequence fresh and resumed workflow stages."""

    def __init__(
        self,
        repository: WorkflowRepository,
        task_provider: TaskProvider,
        config_loader: ProjectConfigLoader,
        client_factory: ClientFactory,
        events: StageEvents,
        corrections: CorrectionPolicy,
        actions: StageActions,
        lifecycle: ExecutionLifecycle,
        agents: WorkflowAgentRuntime,
        *,
        verbose: bool = False,
        repository_index_clients: Any | None = None,
        context_resolver: Callable[
            ..., RepositoryContextStrategy
        ] = resolve_repository_context_strategy,
    ) -> None:
        self._repository = repository
        self._task_provider = task_provider
        self._config_loader = config_loader
        self._client_factory = client_factory
        self._events = events
        self._corrections = corrections
        self._actions = actions
        self._lifecycle = lifecycle
        self._agents = agents
        self._verbose = verbose
        self._repository_index_clients = repository_index_clients
        self._context_resolver = context_resolver

    def _repository_context_strategy(self, config: ProjectConfig) -> RepositoryContextStrategy:
        """Resolve context for each fresh or resumed stage sequence."""
        return self._context_resolver(
            config.repository_context.strategy,
            settings=config.repository_context,
            repository_key=config.configured_repository_key,
            configured_root=config.path,
            client_manager=self._repository_index_clients,
        )

    def _context_truncation_callback(
        self, execution_id: str
    ) -> Callable[[Path, int, int], None]:
        def record(path: Path, actual_chars: int, cap_chars: int) -> None:
            runtime = self._events._runtimes.peek(execution_id)
            self._events._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {
                    "kind": "PROJECT_CONTEXT_TRUNCATED",
                    "severity": "warning",
                    "path": str(path),
                    "actual_chars": actual_chars,
                    "cap_chars": cap_chars,
                },
                stage=runtime.active_stage if runtime else None,
            )

        return record

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
            self._lifecycle._start_admitted(execution.id)
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution.id, WorkflowStatus.CANCELLED, None)
        workspace_path: Path | None = None
        config: ProjectConfig | None = None
        try:
            logger.info("[workflow] Loading %s task %s", self._task_provider.source, task_id)
            with self._events._step(execution.id, WorkflowStage.LOAD_TASK):
                # A CLI submission without an explicit repository reads the task
                # to establish its admission identity. Use that same snapshot or
                # error after dispatch, so a later provider edit cannot change
                # the repository being executed.
                if admission_error is not None:
                    raise admission_error
                task = admission_task or self._task_provider.get_task(task_id)
                self._repository.update_task(execution.id, task)

            with self._events._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._config_loader.load(task, repository_override)
                if provider_overrides or runtime_overrides:
                    config = config.with_agent_runtime_overrides(
                        provider_overrides,
                        runtime_overrides,
                    )
                    self._lifecycle._record_runtime_overrides(
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
                repository_context_strategy = self._repository_context_strategy(config)

            with self._events._step(execution.id, WorkflowStage.GENERATE_SPEC):
                spec = SpecService(
                    self._agents._diagnostic_agent(
                        clients.agent(AgentRole.PLANNER),
                        config,
                        AgentRole.PLANNER,
                        execution.id,
                    ),
                    repository_context_strategy,
                    self._context_truncation_callback(execution.id),
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
                return self._lifecycle._pause_for_human(
                    execution.id,
                    WorkflowStage.GENERATE_SPEC,
                    StageValues._planner_prompt(spec),
                    config,
                )

            working_branch = StageValues._branch_name(
                config.repository.branch_prefix, task.id, task.title, execution.id
            )
            workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
            with self._events._step(execution.id, WorkflowStage.PREPARE_BRANCH):
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
                clients.git.at_path(workspace_path).ensure_merge_base(config.repository.base_branch)
            execution_clients = clients.at_path(workspace_path)

            implementation_service = ImplementationService(
                self._agents._diagnostic_agent(
                    execution_clients.agent(AgentRole.IMPLEMENTER),
                    config,
                    AgentRole.IMPLEMENTER,
                    execution.id,
                ),
                execution_clients.git,
                self._context_truncation_callback(execution.id),
            )
            with self._events._step(execution.id, WorkflowStage.IMPLEMENT):
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

            validation = self._corrections._validate_with_corrections(
                execution_id=execution.id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )

            self._actions._commit_changes(
                execution.id, execution_clients, task, working_branch, config
            )

            with self._events._step(execution.id, WorkflowStage.PUSH):
                execution_clients.git.push(working_branch, config.repository.protected_branches)

            with self._events._step(execution.id, WorkflowStage.CREATE_PR):
                pull_request = execution_clients.github.create_pull_request(
                    title=task.title,
                    body=StageValues._pull_request_body(
                        task,
                        validation,
                        self._task_provider.pull_request_reference(task, repository_slug),
                    ),
                    base_branch=config.repository.base_branch,
                    head_branch=working_branch,
                )
                self._repository.record_pull_request(execution.id, pull_request)
                self._actions._assign_implementer(
                    execution_clients=execution_clients,
                    pull_request_number=pull_request.number,
                    workspace_path=workspace_path,
                )

            review_attempt = 0
            polish_attempted = execution.polish_outcome is not None
            while True:
                with self._events._step(execution.id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._agents._diagnostic_agent(
                            clients.agent(AgentRole.REVIEWER),
                            config,
                            AgentRole.REVIEWER,
                            execution.id,
                        ),
                        repository_context_strategy,
                        self._context_truncation_callback(execution.id),
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
                        self._repository.record_error(
                            execution.id, StageValues._review_prompt(review)
                        )
                    self._actions._publish_review(
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
                    polish_outcome, polish_validation = self._corrections._evaluate_optional_polish(
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

                if not self._corrections._review_retry_available(review, review_attempt, config):
                    break

                review_attempt += 1
                validation = self._corrections._correct_review_findings(
                    execution_id=execution.id,
                    execution_clients=execution_clients,
                    implementation_service=implementation_service,
                    task=task,
                    spec=spec,
                    review=review,
                    workspace_path=workspace_path,
                    working_branch=working_branch,
                    config=config,
                    attempt=review_attempt,
                    log_retry=True,
                )

            final_status = {
                ReviewDecision.APPROVED: WorkflowStatus.APPROVED,
                ReviewDecision.CHANGES_REQUESTED: WorkflowStatus.CHANGES_REQUESTED,
                ReviewDecision.NEEDS_HUMAN: WorkflowStatus.NEEDS_HUMAN,
            }[review.result]
            if final_status is WorkflowStatus.NEEDS_HUMAN:
                self._repository.request_human_input(
                    execution.id, WorkflowStage.REVIEW, StageValues._review_prompt(review)
                )
            return self._lifecycle._finish(execution.id, final_status, config)
        except WorkflowCancelledError:
            result = self._lifecycle._finish(execution.id, WorkflowStatus.CANCELLED, config)
            if workspace_path is not None:
                logger.info(
                    "[workspace] Workflow cancelled; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except ConfigurationError as exc:
            return self._lifecycle._pause_for_human(
                execution.id,
                self._repository.get_execution(execution.id).current_stage,
                str(exc),
                config,
            )
        except AgentNeedsHumanError as exc:
            stage = self._repository.get_execution(execution.id).current_stage
            result = self._lifecycle._pause_for_human(execution.id, stage, str(exc), config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow needs human input; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except Exception as exc:
            self._repository.record_error(execution.id, str(exc))
            self._lifecycle._record_quota_failure(execution.id, exc)
            result = self._lifecycle._finish(execution.id, WorkflowStatus.FAILED, config)
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
        with self._events._step(execution.id, WorkflowStage.GENERATE_SPEC):
            spec = SpecService(
                self._agents._diagnostic_agent(
                    clients.agent(AgentRole.PLANNER), config, AgentRole.PLANNER, execution.id
                ),
                repository_context_strategy,
                self._context_truncation_callback(execution.id),
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
            return self._lifecycle._pause_for_human(
                execution.id,
                WorkflowStage.GENERATE_SPEC,
                StageValues._planner_prompt(spec),
                config,
            )

        working_branch = StageValues._branch_name(
            config.repository.branch_prefix, task.id, task.title, execution.id
        )
        workspace_manager = WorkspaceManager(clients.git, config.path, config.workspace)
        with self._events._step(execution.id, WorkflowStage.PREPARE_BRANCH):
            workspace_path = workspace_manager.workspace_path(execution.id)
            self._repository.record_workspace(execution.id, str(workspace_path))
            workspace_path = workspace_manager.create(
                execution.id,
                config.repository.base_branch,
                working_branch,
                config.repository.protected_branches,
            )
            self._repository.record_branch(execution.id, working_branch)
            clients.git.at_path(workspace_path).ensure_merge_base(config.repository.base_branch)
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
            raise WorkflowError(f"Cannot resume review: workspace is unavailable: {workspace_path}")
        if not execution.spec_snapshot or not execution.pull_request_snapshot:
            raise WorkflowError("Cannot resume review: persisted spec or pull request is missing")
        spec = RepositorySpec(**execution.spec_snapshot)
        pull_request = PullRequest(**execution.pull_request_snapshot)
        validation = StageValues._validation_from_snapshot(execution.validation_snapshot)
        execution_clients = clients.at_path(workspace_path)
        implementation_service = ImplementationService(
            self._agents._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution.id,
            ),
            execution_clients.git,
            self._context_truncation_callback(execution.id),
        )
        polish_attempted = execution.polish_outcome is not None
        while True:
            with self._events._step(execution.id, WorkflowStage.REVIEW):
                review = ReviewService(
                    self._agents._diagnostic_agent(
                        clients.agent(AgentRole.REVIEWER), config, AgentRole.REVIEWER, execution.id
                    ),
                    repository_context_strategy,
                    self._context_truncation_callback(execution.id),
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
                self._actions._publish_review(
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
                polish_outcome, polish_validation = self._corrections._evaluate_optional_polish(
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
                execution.id, WorkflowStage.REVIEW, StageValues._review_prompt(review)
            )
        return self._lifecycle._finish(execution.id, status, config)

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
            self._agents._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config,
                AgentRole.IMPLEMENTER,
                execution_id,
            ),
            execution_clients.git,
            self._context_truncation_callback(execution_id),
        )
        try:
            with self._events._step(execution_id, WorkflowStage.IMPLEMENT):
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
            validation = self._corrections._validate_with_corrections(
                execution_id=execution_id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task,
                spec=spec,
                workspace_path=workspace_path,
                config=config,
            )
            self._actions._commit_changes(
                execution_id, execution_clients, task, working_branch, config
            )
            with self._events._step(execution_id, WorkflowStage.PUSH):
                pull_request_snapshot = self._repository.get_execution(
                    execution_id
                ).pull_request_snapshot
                if pull_request_snapshot:
                    existing_pr = PullRequest(**pull_request_snapshot)
                    self._actions._assert_pull_request_open(
                        execution_clients, repository_slug, existing_pr.number
                    )
                execution_clients.git.push(working_branch, config.repository.protected_branches)
            if pull_request_snapshot:
                pull_request = PullRequest(**pull_request_snapshot)
            else:
                with self._events._step(execution_id, WorkflowStage.CREATE_PR):
                    pull_request = execution_clients.github.create_pull_request(
                        title=task.title,
                        body=StageValues._pull_request_body(
                            task,
                            validation,
                            self._task_provider.pull_request_reference(task, repository_slug),
                        ),
                        base_branch=config.repository.base_branch,
                        head_branch=working_branch,
                    )
                    self._repository.record_pull_request(execution_id, pull_request)
                    self._actions._assign_implementer(
                        execution_clients=execution_clients,
                        pull_request_number=pull_request.number,
                        workspace_path=workspace_path,
                    )
            polish_attempted = not force_polish and (
                self._repository.get_execution(execution_id).polish_outcome is not None
            )
            review_attempt = 0
            while True:
                with self._events._step(execution_id, WorkflowStage.REVIEW):
                    review = ReviewService(
                        self._agents._diagnostic_agent(
                            clients.agent(AgentRole.REVIEWER),
                            config,
                            AgentRole.REVIEWER,
                            execution_id,
                        ),
                        repository_context_strategy,
                        self._context_truncation_callback(execution_id),
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
                    self._actions._publish_review(
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
                    polish_outcome, polish_validation = self._corrections._evaluate_optional_polish(
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
                        before_push=lambda: self._actions._assert_pull_request_open(
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
                if self._corrections._review_retry_available(review, review_attempt, config):
                    review_attempt += 1
                    validation = self._corrections._correct_review_findings(
                        execution_id=execution_id,
                        execution_clients=execution_clients,
                        implementation_service=implementation_service,
                        task=task,
                        spec=spec,
                        review=review,
                        workspace_path=workspace_path,
                        working_branch=working_branch,
                        config=config,
                        attempt=review_attempt,
                        before_push=lambda: self._actions._assert_pull_request_open(
                            execution_clients, repository_slug, pull_request.number
                        ),
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
                    execution_id, WorkflowStage.REVIEW, StageValues._review_prompt(review)
                )
            return self._lifecycle._finish(execution_id, status, config)
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._lifecycle._pause_for_human(
                execution_id, WorkflowStage.IMPLEMENT, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            self._lifecycle._record_quota_failure(execution_id, exc)
            return self._lifecycle._finish(execution_id, WorkflowStatus.FAILED, config)

    def _execute_response(
        self,
        execution: WorkflowExecution,
        interaction: HumanInteraction,
        response: str,
    ) -> WorkflowExecution:
        execution_id = execution.id
        try:
            self._lifecycle._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, None)
        config: ProjectConfig | None = None
        try:
            task = StageValues._task_from_snapshot(execution)
            config = self._config_loader.load(
                task, execution.repository_ref or execution.repository or None
            )
            provider_overrides, runtime_overrides = (
                self._lifecycle._runtime_overrides_for_execution(execution_id)
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
            clarified_task = StageValues._task_with_responses(
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
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._lifecycle._pause_for_human(
                execution_id, interaction.requesting_stage, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            self._lifecycle._record_quota_failure(execution_id, exc)
            return self._lifecycle._finish(execution_id, WorkflowStatus.FAILED, config)

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
            self._lifecycle._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
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
            self._lifecycle._start_admitted(execution_id)
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        try:
            repository_slug = clients.git.github_repository()
            repository_context_strategy = self._repository_context_strategy(config)
            # Earlier answers still apply to the repeated planning attempt.
            clarified_task = StageValues._task_with_responses(
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
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._lifecycle._pause_for_human(
                execution_id, WorkflowStage.GENERATE_SPEC, str(exc), config
            )
        except Exception as exc:
            self._repository.record_error(execution_id, str(exc))
            self._lifecycle._record_quota_failure(execution_id, exc)
            return self._lifecycle._finish(execution_id, WorkflowStatus.FAILED, config)
