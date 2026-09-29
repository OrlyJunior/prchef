from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orchestrator.clients.command_client import assert_worktree_idle
from orchestrator.clients.exceptions import (
    CommandExecutionError,
    CommandProcessTreeUnconfirmedError,
    GitHubClientError,
)
from orchestrator.clients.factory import ClientFactory, WorkflowClients
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_interruption import WorkflowInterruptedError
from orchestrator.models.workflow_status import (
    PolishOutcome,
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import log_review_summary, log_spec_summary, summarize_text
from orchestrator.services.exceptions import WorkflowCancelledError, WorkflowError
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.repository_context import (
    RepositoryContextRequest,
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


class _DiagnosedRepositoryContext:
    """Report stale planning provenance before retrieval can synchronize metadata.

    Review indexes the feature worktree, whose commit is expected to differ from
    the fetched base.
    """

    def __init__(
        self,
        strategy: RepositoryContextStrategy,
        on_mismatch: Callable[[str], None],
        base_commit: str,
    ) -> None:
        self._strategy = strategy
        self._on_mismatch = on_mismatch
        self._base_commit = base_commit

    def render(self, request: RepositoryContextRequest) -> str:
        if request.role == "planner":
            inspect_commit = getattr(self._strategy, "inspect_indexed_commit", None)
            if callable(inspect_commit):
                indexed_commit = inspect_commit()
            else:
                inspect = getattr(self._strategy, "inspect_index", None)
                indexed_commit = inspect().last_indexed_commit if callable(inspect) else None
            if indexed_commit and indexed_commit != self._base_commit:
                self._on_mismatch(indexed_commit)
        return self._strategy.render(request)


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

    def _repository_context_strategy(
        self, config: ProjectConfig, execution_id: str | None = None
    ) -> RepositoryContextStrategy:
        """Resolve context for each fresh or resumed stage sequence."""

        def record(payload: dict[str, str]) -> None:
            if execution_id is None:
                return
            runtime = self._events._runtimes.peek(execution_id)
            self._events._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                payload,
                stage=runtime.active_stage if runtime else None,
            )

        def progress(message: str) -> None:
            record({"kind": "REPOSITORY_INDEX_PROGRESS", "message": message})

        strategy = self._context_resolver(
            config.repository_context.strategy,
            settings=config.repository_context,
            repository_key=config.configured_repository_key,
            configured_root=config.path,
            client_manager=self._repository_index_clients,
            on_progress=progress,
            on_diagnostic=record,
            cancellation_check=(
                (lambda: self._events._raise_if_cancellation_requested(execution_id))
                if execution_id is not None else None
            ),
        )
        if config.repository_context.strategy != "qdrant" or execution_id is None:
            return strategy
        base_commit = self._repository.get_execution(execution_id).base_commit
        if not base_commit:
            return strategy

        def record_mismatch(indexed_commit: str) -> None:
            runtime = self._events._runtimes.peek(execution_id)
            self._events._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {
                    "kind": "REPOSITORY_INDEX_BASE_MISMATCH",
                    "severity": "warning",
                    "indexed_commit": indexed_commit,
                    "base_commit": base_commit,
                },
                stage=runtime.active_stage if runtime else None,
            )

        return _DiagnosedRepositoryContext(strategy, record_mismatch, base_commit)

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

    def _pull_request_description(
        self,
        execution_id: str,
        clients: WorkflowClients,
        repository_slug: str,
        pull_request: PullRequest,
    ) -> str | None:
        try:
            return clients.github.pull_request_description(
                repository_slug, pull_request.number
            )
        except CommandProcessTreeUnconfirmedError:
            raise
        except (GitHubClientError, CommandExecutionError, OSError) as exc:
            runtime = self._events._runtimes.peek(execution_id)
            self._events._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {
                    "kind": "PULL_REQUEST_DESCRIPTION_UNAVAILABLE",
                    "severity": "warning",
                    "reason": summarize_text(str(exc), limit=400),
                },
                stage=runtime.active_stage if runtime else None,
            )
            return None

    def _execute_workflow(
        self,
        execution: WorkflowExecution,
        task_id: str,
        repository_override: str | None,
        provider_overrides: Mapping[str, str],
        runtime_overrides: Mapping[str, Mapping[str, object]],
        admission_task: DevelopmentTask | None = None,
        admission_error: Exception | None = None,
        recovering: bool = False,
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
                task = (
                    StageValues._task_from_snapshot(execution)
                    if recovering and execution.task_snapshot
                    else admission_task or self._task_provider.get_task(task_id)
                )
                self._repository.update_task(execution.id, task)

            with self._events._step(execution.id, WorkflowStage.LOAD_CONFIG):
                config = self._config_loader.load(task, repository_override)
                if provider_overrides or runtime_overrides:
                    config = config.with_agent_runtime_overrides(
                        provider_overrides,
                        runtime_overrides,
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
                base_commit = (
                    execution.base_commit
                    if recovering and execution.base_commit
                    else clients.git.fetch_base(config.repository.base_branch)
                )
                if base_commit != execution.base_commit:
                    self._repository.record_base_commit(execution.id, base_commit)
                repository_context_strategy = self._repository_context_strategy(
                    config, execution.id
                )

            with self._events._step(execution.id, WorkflowStage.GENERATE_SPEC):
                with clients.git.planning_snapshot(base_commit) as snapshot:
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
                        snapshot,
                        repository_slug,
                        config.repository.base_branch,
                        lambda reference: self._repository.record_spec_jules_session(
                            execution.id, reference.external_id, reference.url
                        ),
                        instructions_repository_path=config.path,
                        temporary_repository_context=True,
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
                logger.info("[workspace] Base commit: %s", base_commit)
                logger.info("[workspace] Branch: %s", working_branch)
                logger.info("[workspace] Path: %s", workspace_path)
                workspace_path = workspace_manager.create(
                    execution.id,
                    config.repository.base_branch,
                    working_branch,
                    config.repository.protected_branches,
                    base_commit,
                )
                self._repository.record_branch(execution.id, working_branch)
                clients.git.at_path(workspace_path).ensure_merge_base(base_commit)
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
                        diff=execution_clients.git.diff(base_commit),
                        pull_request_description=self._pull_request_description(
                            execution.id, execution_clients, repository_slug, pull_request
                        ),
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
        except WorkflowInterruptedError as exc:
            return self._repository.reinterrupt(execution.id, str(exc))
        except WorkflowCancelledError:
            result = self._lifecycle._finish(execution.id, WorkflowStatus.CANCELLED, config)
            if workspace_path is not None:
                logger.info(
                    "[workspace] Workflow cancelled; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except ConfigurationError as exc:
            if recovering:
                return self._repository.reinterrupt(execution.id, str(exc))
            return self._lifecycle._pause_for_human(
                execution.id,
                self._repository.get_execution(execution.id).current_stage,
                str(exc),
                config,
            )
        except AgentNeedsHumanError as exc:
            if recovering:
                return self._repository.reinterrupt(execution.id, str(exc))
            stage = self._repository.get_execution(execution.id).current_stage
            result = self._lifecycle._pause_for_human(execution.id, stage, str(exc), config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow needs human input; preserving workspace: %s",
                    workspace_path,
                )
            return result
        except Exception as exc:
            if recovering:
                return self._repository.reinterrupt(execution.id, str(exc))
            self._repository.record_error(execution.id, str(exc))
            self._lifecycle._record_quota_failure(execution.id, exc)
            result = self._lifecycle._finish(execution.id, WorkflowStatus.FAILED, config)
            if workspace_path is not None:
                logger.error(
                    "[workspace] Workflow failed; preserving workspace: %s", workspace_path
                )
            return result

    def _execute_interrupted(self, execution: WorkflowExecution) -> WorkflowExecution:
        """Restart at the persisted stage without creating a second execution."""
        stage = execution.resume_stage or execution.current_stage
        if execution.resume_mode == "HUMAN_RESPONSE":
            interaction = next(
                (item for item in reversed(execution.human_interactions) if item.response),
                None,
            )
            if interaction is None or not interaction.response:
                return self._repository.reinterrupt(
                    execution.id, "Persisted human answer is missing"
                )
            return self._execute_response(
                execution, interaction, interaction.response, recovering=True
            )
        try:
            if stage is WorkflowStage.LOAD_TASK:
                providers, options = self._lifecycle._runtime_overrides_for_execution(execution.id)
                return self._execute_workflow(
                    execution, execution.task_id, execution.repository_ref or None,
                    providers, options,
                    recovering=True,
                )
            self._lifecycle._start_admitted(execution.id)
            task = StageValues._task_from_snapshot(execution)
            answered = [(item.prompt, item.response) for item in execution.human_interactions
                        if item.response]
            if answered:
                task = StageValues._task_with_responses(task, answered)
            repository_ref = execution.repository_ref or execution.repository or None
            config = self._config_loader.load(task, repository_ref)
            providers, options = self._lifecycle._runtime_overrides_for_execution(execution.id)
            if providers or options:
                config = config.with_agent_runtime_overrides(providers, options)
            clients = self._client_factory.create(config)
            repository_slug = clients.git.github_repository()
            context = self._repository_context_strategy(config, execution.id)
            continuation_feedback: str | None = None
            if execution.resume_mode == "MANUAL_CONTINUATION":
                for diagnostic in reversed(self._repository.list_diagnostics(execution.id)):
                    if diagnostic.payload.get("kind") == "MANUAL_CONTINUATION":
                        value = diagnostic.payload.get("feedback")
                        if isinstance(value, str) and value.strip():
                            continuation_feedback = value
                        break
                if continuation_feedback is None:
                    raise WorkflowError("Persisted continuation feedback is missing")
            if stage is WorkflowStage.LOAD_CONFIG:
                with self._events._step(execution.id, WorkflowStage.LOAD_CONFIG):
                    self._repository.update_project(
                        execution.id, str(config.path), config.repository.base_branch,
                        repository_ref or str(config.path),
                    )
                    if not execution.base_commit:
                        base_commit = clients.git.fetch_base(config.repository.base_branch)
                        self._repository.record_base_commit(execution.id, base_commit)
                execution = self._repository.get_execution(execution.id)
                stage = WorkflowStage.GENERATE_SPEC
            if stage is WorkflowStage.GENERATE_SPEC:
                if not execution.base_commit:
                    raise WorkflowError("Cannot resume planning: fetched base commit is missing")
                if continuation_feedback is not None:
                    task = StageValues._task_with_responses(
                        task,
                        [("Continuation feedback for repeating planning:",
                          continuation_feedback)],
                    )
                return self._resume_planning(
                    execution, task, config, clients, repository_slug, context,
                    recovering=True,
                )
            if not execution.spec_snapshot:
                raise WorkflowError("Cannot resume: persisted repository spec is missing")
            spec = RepositorySpec(**execution.spec_snapshot)
            if stage is WorkflowStage.PREPARE_BRANCH and spec.has_blocking_ambiguity:
                return self._lifecycle._pause_for_human(
                    execution.id, WorkflowStage.GENERATE_SPEC,
                    StageValues._planner_prompt(spec), config,
                )
            if continuation_feedback is not None and stage is not WorkflowStage.IMPLEMENT:
                self._repository.set_resume_mode(execution.id, None)
                continuation_feedback = None
            branch = execution.working_branch or StageValues._branch_name(
                config.repository.branch_prefix, task.id, task.title, execution.id
            )
            manager = WorkspaceManager(clients.git, config.path, config.workspace)
            workspace_path = manager.workspace_path(execution.id)
            assert_worktree_idle(workspace_path)
            if (
                execution.workspace_path
                and Path(execution.workspace_path).resolve() != workspace_path
            ):
                raise WorkflowError("Cannot resume: persisted workspace path changed")
            if stage is WorkflowStage.PREPARE_BRANCH:
                with self._events._step(execution.id, WorkflowStage.PREPARE_BRANCH):
                    self._repository.record_workspace(execution.id, str(workspace_path))
                    if not workspace_path.exists():
                        if clients.git.branch_exists(branch):
                            if not execution.base_commit:
                                raise WorkflowError(
                                    "Cannot verify retained branch without base commit"
                                )
                            clients.git.attach_existing_worktree(
                                workspace_path, branch, execution.base_commit
                            )
                        else:
                            manager.create(
                                execution.id, config.repository.base_branch, branch,
                                config.repository.protected_branches, execution.base_commit,
                            )
                    clients.git.at_path(workspace_path).verify_worktree_branch(branch, None)
                    self._repository.record_branch(execution.id, branch)
                stage = WorkflowStage.IMPLEMENT
            else:
                if not execution.workspace_path or not execution.working_branch:
                    raise WorkflowError("Cannot resume: retained workspace or branch is missing")
                if not workspace_path.is_dir():
                    raise WorkflowError(
                        f"Cannot resume: workspace is unavailable: {workspace_path}"
                    )
                clients.git.at_path(workspace_path).verify_worktree_branch(branch, None)
            if execution.base_commit:
                clients.git.at_path(workspace_path).ensure_merge_base(execution.base_commit)
            if execution.resume_mode in {
                "VALIDATION_CORRECTION", "REVIEW_CORRECTION",
                "REVIEW_VALIDATION_CORRECTION",
            }:
                execution_clients = clients.at_path(workspace_path)
                implementer = ImplementationService(
                    self._agents._diagnostic_agent(
                        execution_clients.agent(AgentRole.IMPLEMENTER),
                        config, AgentRole.IMPLEMENTER, execution.id,
                    ),
                    execution_clients.git,
                    self._context_truncation_callback(execution.id),
                )
                if execution.resume_mode == "VALIDATION_CORRECTION":
                    if stage is WorkflowStage.IMPLEMENT:
                        failed = StageValues._validation_from_snapshot(
                            execution.validation_snapshot
                        )
                        with self._events._step(execution.id, WorkflowStage.IMPLEMENT):
                            result = implementer.correct_validation_failures(
                                task, spec, failed, workspace_path,
                                instructions_repository_path=config.path,
                            )
                            self._repository.record_implementation(execution.id, result)
                            if not result.success:
                                raise WorkflowError(
                                    result.error_message or "Validation correction failed"
                                )
                        stage = WorkflowStage.VALIDATE
                else:
                    if not execution.review_snapshot or not execution.pull_request_snapshot:
                        raise WorkflowError(
                            "Cannot resume review correction: review or PR is missing"
                        )
                    review = CodeReview(**{
                        **execution.review_snapshot,
                        "result": ReviewDecision(execution.review_snapshot["result"]),
                    })
                    pr = PullRequest(**execution.pull_request_snapshot)
                    self._corrections._correct_review_findings(
                        execution_id=execution.id,
                        execution_clients=execution_clients,
                        implementation_service=implementer,
                        task=task, spec=spec, review=review,
                        workspace_path=workspace_path, working_branch=branch,
                        config=config, attempt=execution.review_retry_attempt,
                        before_push=lambda: self._actions._assert_pull_request_open(
                            execution_clients, repository_slug, pr.number
                        ),
                        restart_stage=(
                            WorkflowStage.REVIEW if stage is WorkflowStage.CREATE_PR else stage
                        ), recovering=True,
                    )
                    stage = WorkflowStage.REVIEW
            if execution.resume_mode == "POLISH" or stage is WorkflowStage.POLISH:
                outcome = self._resume_optional_polish(
                    execution, stage, task, spec, config, clients,
                    repository_slug, workspace_path, branch,
                )
                if outcome is PolishOutcome.SKIPPED:
                    return self._lifecycle._finish(
                        execution.id, WorkflowStatus.APPROVED, config
                    )
                stage = WorkflowStage.REVIEW
            steps = self._repository.list_steps(execution.id)
            last_step = steps[-1] if steps else None
            interrupted_review = (
                stage is WorkflowStage.REVIEW
                and last_step is not None
                and last_step.stage is WorkflowStage.REVIEW
                and last_step.status is StepStatus.INTERRUPTED
            )
            if interrupted_review and last_step is not None and execution.review_snapshot:
                if execution.review_step_id is not None:
                    reuse_review = execution.review_step_id == last_step.id
                elif execution.review_publication_pending:
                    # Rows written before review provenance was added still
                    # carry the durable publication checkpoint.
                    reuse_review = True
                elif any(
                    item.stage is WorkflowStage.REVIEW
                    and item.status is StepStatus.SUCCEEDED
                    for item in steps[:-1]
                ):
                    reuse_review = False
                else:
                    raise WorkflowError(
                        "Cannot verify which review output belongs to the interrupted "
                        "review attempt; inspect the pull request before retrying"
                    )
            else:
                reuse_review = (
                    stage is WorkflowStage.REVIEW
                    and last_step is not None
                    and last_step.stage is WorkflowStage.REVIEW
                    and last_step.status is StepStatus.SUCCEEDED
                    and bool(execution.review_snapshot)
                    and not any(
                        item.response and item.requesting_stage is WorkflowStage.REVIEW
                        and item.responded_at and item.responded_at >= last_step.started_at
                        for item in execution.human_interactions
                    )
                )
            return self._implement_and_review(
                execution.id, task, spec, config, clients, repository_slug,
                workspace_path, branch, context, restart_stage=stage, recovering=True,
                reuse_review=reuse_review,
                continuation_feedback=continuation_feedback,
                force_polish=continuation_feedback is not None,
            )
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution.id, WorkflowStatus.CANCELLED, None)
        except Exception as exc:
            return self._repository.reinterrupt(execution.id, str(exc))

    def _resume_optional_polish(
        self, execution: WorkflowExecution, stage: WorkflowStage,
        task: DevelopmentTask, spec: RepositorySpec, config: ProjectConfig,
        clients: WorkflowClients, repository_slug: str, workspace_path: Path,
        branch: str,
    ) -> PolishOutcome:
        if not execution.review_snapshot or not execution.pull_request_snapshot:
            raise WorkflowError("Cannot resume polish: review or pull request is missing")
        review = CodeReview(**{
            **execution.review_snapshot,
            "result": ReviewDecision(execution.review_snapshot["result"]),
        })
        if review.result is not ReviewDecision.APPROVED:
            raise WorkflowError("Cannot resume polish: retained review was not approved")
        pull_request = PullRequest(**execution.pull_request_snapshot)
        execution_clients = clients.at_path(workspace_path)
        self._actions._assert_pull_request_open(
            execution_clients, repository_slug, pull_request.number
        )
        implementation_service = ImplementationService(
            self._agents._diagnostic_agent(
                execution_clients.agent(AgentRole.IMPLEMENTER),
                config, AgentRole.IMPLEMENTER, execution.id,
            ), execution_clients.git, self._context_truncation_callback(execution.id),
        )
        if execution.polish_outcome is not None:
            self._repository.set_resume_mode(execution.id, None)
            return execution.polish_outcome
        if stage is WorkflowStage.POLISH:
            outcome, _ = self._corrections._evaluate_optional_polish(
                execution_id=execution.id,
                execution_clients=execution_clients,
                implementation_service=implementation_service,
                task=task, spec=spec, suggestions=review.suggestions,
                workspace_path=workspace_path, pull_request=pull_request,
                working_branch=branch, config=config,
                before_push=lambda: self._actions._assert_pull_request_open(
                    execution_clients, repository_slug, pull_request.number
                ),
            )
            return outcome
        if stage is WorkflowStage.CREATE_PR:
            # Older checkpoints used the ordinary PUSH successor even inside
            # optional polish, which updates an existing PR.
            stage = WorkflowStage.REVIEW
        if stage not in {
            WorkflowStage.VALIDATE, WorkflowStage.COMMIT,
            WorkflowStage.PUSH, WorkflowStage.REVIEW,
        }:
            raise WorkflowError(f"Cannot resume polish from {stage.value}")
        if stage is WorkflowStage.REVIEW:
            if not StageValues._validation_from_snapshot(
                execution.validation_snapshot
            ).success:
                raise WorkflowError("Polish validation did not succeed")
            if (
                execution.commit_sha
                and execution_clients.git.remote_branch_commit(branch)
                == execution_clients.git.head_commit()
            ):
                self._repository.record_polish_outcome(execution.id, PolishOutcome.APPLIED)
                self._repository.set_resume_mode(execution.id, None)
                return PolishOutcome.APPLIED
            raise WorkflowError("Cannot verify polish push before resuming review")
        if not execution.implementation_snapshot:
            raise WorkflowError("Cannot resume polish: implementation output is missing")
        implementation = ImplementationResult(**execution.implementation_snapshot)
        if not implementation.success or not implementation.changed_files:
            self._corrections._record_skipped_polish(
                execution_id=execution.id, execution_clients=execution_clients,
                pull_request_number=pull_request.number, implementation=implementation,
            )
            self._repository.set_resume_mode(execution.id, None)
            return PolishOutcome.SKIPPED
        if stage is WorkflowStage.VALIDATE:
            validation = self._corrections._validate_polish_changes(
                execution_id=execution.id, execution_clients=execution_clients,
                workspace_path=workspace_path, config=config,
            )
            if validation is None:
                self._corrections._record_skipped_polish(
                    execution_id=execution.id, execution_clients=execution_clients,
                    pull_request_number=pull_request.number, implementation=implementation,
                )
                self._repository.set_resume_mode(execution.id, None)
                return PolishOutcome.SKIPPED
            stage = WorkflowStage.COMMIT
        if stage is WorkflowStage.COMMIT:
            if not StageValues._validation_from_snapshot(
                self._repository.get_execution(execution.id).validation_snapshot
            ).success:
                raise WorkflowError("Polish validation did not succeed")
            self._actions._commit_changes(
                execution.id, execution_clients, task, branch, config, reconcile=True
            )
            stage = WorkflowStage.PUSH
        if stage is WorkflowStage.PUSH:
            with self._events._step(execution.id, WorkflowStage.PUSH):
                if execution_clients.git.remote_branch_commit(branch) != \
                        execution_clients.git.head_commit():
                    execution_clients.git.push(
                        branch, config.repository.protected_branches
                    )
            self._repository.record_polish_outcome(execution.id, PolishOutcome.APPLIED)
            self._repository.set_resume_mode(execution.id, None)
            return PolishOutcome.APPLIED
        raise WorkflowError("Cannot resume optional polish")

    def _resume_planning(
        self,
        execution: WorkflowExecution,
        task: DevelopmentTask,
        config: ProjectConfig,
        clients: WorkflowClients,
        repository_slug: str,
        repository_context_strategy: RepositoryContextStrategy,
        *,
        recovering: bool = False,
    ) -> WorkflowExecution:
        """Repeat only planning, then continue through a newly safe workspace."""
        base_ref = execution.base_commit or config.repository.base_branch
        with self._events._step(execution.id, WorkflowStage.GENERATE_SPEC):
            with clients.git.planning_snapshot(base_ref) as snapshot:
                spec = SpecService(
                    self._agents._diagnostic_agent(
                        clients.agent(AgentRole.PLANNER), config, AgentRole.PLANNER, execution.id
                    ),
                    repository_context_strategy,
                    self._context_truncation_callback(execution.id),
                ).generate(
                    task,
                    snapshot,
                    repository_slug,
                    config.repository.base_branch,
                    lambda reference: self._repository.record_spec_jules_session(
                        execution.id, reference.external_id, reference.url
                    ),
                    instructions_repository_path=config.path,
                    temporary_repository_context=True,
                )
            self._repository.record_spec(execution.id, spec)
        if spec.has_blocking_ambiguity:
            return self._lifecycle._pause_for_human(
                execution.id,
                WorkflowStage.GENERATE_SPEC,
                StageValues._planner_prompt(spec),
                config,
            )

        if recovering and execution.resume_mode == "MANUAL_CONTINUATION":
            self._repository.set_resume_mode(execution.id, None)

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
                base_ref,
            )
            self._repository.record_branch(execution.id, working_branch)
            clients.git.at_path(workspace_path).ensure_merge_base(base_ref)
        if recovering:
            return self._implement_and_review(
                execution.id, task, spec, config, clients, repository_slug,
                workspace_path, working_branch, repository_context_strategy,
                recovering=True,
            )
        return self._implement_and_review(
            execution.id, task, spec, config, clients, repository_slug,
            workspace_path, working_branch, repository_context_strategy,
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
        assert_worktree_idle(workspace_path)
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
                    diff=execution_clients.git.diff(
                        execution.base_commit or config.repository.base_branch
                    ),
                    pull_request_description=self._pull_request_description(
                        execution.id, execution_clients, repository_slug, pull_request
                    ),
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
        restart_stage: WorkflowStage = WorkflowStage.IMPLEMENT,
        recovering: bool = False,
        reuse_review: bool = False,
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
            downstream = {
                WorkflowStage.IMPLEMENT: 0, WorkflowStage.VALIDATE: 1,
                WorkflowStage.COMMIT: 2, WorkflowStage.PUSH: 3,
                WorkflowStage.CREATE_PR: 4, WorkflowStage.REVIEW: 5,
            }
            if restart_stage not in downstream:
                raise WorkflowError(f"Cannot resume from {restart_stage.value}")
            offset = downstream[restart_stage]
            if offset <= 0:
                with self._events._step(execution_id, WorkflowStage.IMPLEMENT):
                    if continuation_feedback is None:
                        implementation = implementation_service.implement(
                            task, spec, workspace_path, instructions_repository_path=config.path
                        )
                    else:
                        implementation = implementation_service.continue_implementation(
                            task, spec, continuation_feedback, workspace_path,
                            instructions_repository_path=config.path,
                        )
                    self._repository.record_implementation(execution_id, implementation)
                    if not implementation.success:
                        raise WorkflowError(
                            implementation.error_message or "Implementation provider failed"
                        )
            if offset <= 1:
                validation = self._corrections._validate_with_corrections(
                    execution_id=execution_id,
                    execution_clients=execution_clients,
                    implementation_service=implementation_service,
                    task=task, spec=spec, workspace_path=workspace_path, config=config,
                    initial_attempt=(
                        self._repository.get_execution(execution_id).validation_retry_attempt
                        if recovering else 0
                    ),
                )
            else:
                validation = StageValues._validation_from_snapshot(
                    self._repository.get_execution(execution_id).validation_snapshot
                )
            if offset <= 2:
                self._actions._commit_changes(
                    execution_id, execution_clients, task, working_branch, config,
                    reconcile=recovering,
                )
            if offset <= 3:
                with self._events._step(execution_id, WorkflowStage.PUSH):
                    pull_request_snapshot = self._repository.get_execution(
                        execution_id
                    ).pull_request_snapshot
                    if pull_request_snapshot:
                        existing_pr = PullRequest(**pull_request_snapshot)
                        self._actions._assert_pull_request_open(
                            execution_clients, repository_slug, existing_pr.number
                        )
                    if recovering:
                        remote = execution_clients.git.remote_branch_commit(working_branch)
                        local = execution_clients.git.head_commit()
                        if remote != local:
                            execution_clients.git.push(
                                working_branch, config.repository.protected_branches
                            )
                    else:
                        execution_clients.git.push(
                            working_branch, config.repository.protected_branches
                        )
            else:
                pull_request_snapshot = self._repository.get_execution(
                    execution_id
                ).pull_request_snapshot
            if recovering and not pull_request_snapshot:
                found = execution_clients.github.find_pull_request(
                    working_branch, config.repository.base_branch
                )
                if found is not None:
                    self._repository.record_pull_request(execution_id, found)
                    pull_request_snapshot = self._repository.get_execution(
                        execution_id
                    ).pull_request_snapshot
                elif offset > 4:
                    raise WorkflowError(
                        "Persisted pull request is missing at the review checkpoint"
                    )
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
            review_attempt = (
                self._repository.get_execution(execution_id).review_retry_attempt
                if recovering else 0
            )
            while True:
                with self._events._step(execution_id, WorkflowStage.REVIEW):
                    pending = self._repository.get_execution(execution_id)
                    if recovering and (pending.review_publication_pending or reuse_review):
                        if not pending.review_snapshot:
                            raise WorkflowError("Persisted reviewer output is missing")
                        review = CodeReview(**{
                            **pending.review_snapshot,
                            "result": ReviewDecision(pending.review_snapshot["result"]),
                        })
                    else:
                        review = ReviewService(
                            self._agents._diagnostic_agent(
                                clients.agent(AgentRole.REVIEWER),
                                config, AgentRole.REVIEWER, execution_id,
                            ),
                            repository_context_strategy,
                            self._context_truncation_callback(execution_id),
                        ).review(
                            task=task, spec=spec, pull_request=pull_request,
                            diff=execution_clients.git.diff(
                                pending.base_commit or config.repository.base_branch
                            ),
                            pull_request_description=self._pull_request_description(
                                execution_id, execution_clients, repository_slug, pull_request
                            ),
                            validation=validation, repository_path=workspace_path,
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
                    if pending.review_publication_pending or not reuse_review:
                        self._actions._publish_review(
                            execution_id=execution_id,
                            execution_clients=execution_clients,
                            repository_slug=repository_slug,
                            pull_request_number=pull_request.number,
                            review=review,
                            reconcile=recovering and pending.review_publication_pending,
                        )
                    reuse_review = False
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
        except WorkflowInterruptedError as exc:
            return self._repository.reinterrupt(execution_id, str(exc))
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._lifecycle._pause_for_human(
                execution_id, WorkflowStage.IMPLEMENT, str(exc), config
            )
        except Exception as exc:
            if recovering:
                return self._repository.reinterrupt(execution_id, str(exc))
            self._repository.record_error(execution_id, str(exc))
            self._lifecycle._record_quota_failure(execution_id, exc)
            return self._lifecycle._finish(execution_id, WorkflowStatus.FAILED, config)

    def _execute_response(
        self,
        execution: WorkflowExecution,
        interaction: HumanInteraction,
        response: str,
        *,
        recovering: bool = False,
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
            repository_context_strategy = self._repository_context_strategy(config, execution.id)
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
            clear_mode = getattr(self._repository, "set_resume_mode", None)
            if callable(clear_mode):
                clear_mode(execution_id, None)
            if interaction.requesting_stage is WorkflowStage.GENERATE_SPEC:
                return self._resume_planning(
                    execution,
                    clarified_task,
                    config,
                    clients,
                    repository_slug,
                    repository_context_strategy,
                    recovering=recovering,
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
        except WorkflowInterruptedError as exc:
            return self._repository.reinterrupt(execution_id, str(exc))
        except WorkflowCancelledError:
            return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, config)
        except AgentNeedsHumanError as exc:
            return self._lifecycle._pause_for_human(
                execution_id, interaction.requesting_stage, str(exc), config
            )
        except Exception as exc:
            if recovering:
                return self._repository.reinterrupt(execution_id, str(exc))
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
        repository_context_strategy = self._repository_context_strategy(config, execution_id)
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
            repository_context_strategy = self._repository_context_strategy(config, execution_id)
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
        except WorkflowInterruptedError as exc:
            return self._repository.reinterrupt(execution_id, str(exc))
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
