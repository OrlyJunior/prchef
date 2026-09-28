from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.workflow_event import WorkflowEventKind, WorkflowEventListener
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStage, WorkflowStatus
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.providers.concurrency import ProviderConcurrencyCoordinator
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.exceptions import WorkflowError
from orchestrator.services.execution_dispatcher import ExecutionDispatcher, ExecutionHandle
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.notifications import NotificationService
from orchestrator.services.repository_context import resolve_repository_context_strategy
from orchestrator.services.task_provider import TaskProvider
from orchestrator.services.workflow_agent_runtime import WorkflowAgentRuntime
from orchestrator.services.workflow_correction_policy import CorrectionPolicy
from orchestrator.services.workflow_execution_lifecycle import ExecutionLifecycle
from orchestrator.services.workflow_stage_actions import StageActions
from orchestrator.services.workflow_stage_events import ExecutionRuntimes, StageEvents
from orchestrator.services.workflow_stage_sequencer import StageSequencer
from orchestrator.services.workflow_stage_values import StageValues

logger = logging.getLogger(__name__)


_MANUAL_CONTINUATION_DIAGNOSTIC_KIND = "MANUAL_CONTINUATION"
_QUOTA_EXHAUSTED_DIAGNOSTIC_KIND = "PROVIDER_QUOTA_EXHAUSTED"


def _latest_failure_was_quota(diagnostics: Sequence[ExecutionDiagnostic]) -> bool:
    """Whether the newest quota/continuation marker is a quota exhaustion."""
    for diagnostic in reversed(diagnostics):
        if diagnostic.category is not DiagnosticCategory.WORKFLOW_EVENT:
            continue
        kind = diagnostic.payload.get("kind")
        if kind == _QUOTA_EXHAUSTED_DIAGNOSTIC_KIND:
            return True
        if kind == _MANUAL_CONTINUATION_DIAGNOSTIC_KIND:
            return False
    return False


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
        self._notification_service = notification_service or NotificationService()
        # A composing process supplies one dispatcher and one coordinator so
        # every execution competes for the same bounded capacity. Isolated
        # callers get private, strictly sequential ones.
        self._dispatcher = dispatcher or ExecutionDispatcher()
        self._provider_coordinator = provider_coordinator or ProviderConcurrencyCoordinator()
        self._runtimes = ExecutionRuntimes()
        self._stage_events = StageEvents(workflow_repository, self._runtimes, verbose=verbose)
        self._actions = StageActions(workflow_repository, self._stage_events)
        self._lifecycle = ExecutionLifecycle(
            workflow_repository,
            self._stage_events,
            self._notification_service,
        )
        self._agents = WorkflowAgentRuntime(
            workflow_repository, self._stage_events, client_factory, self._provider_coordinator
        )
        self._corrections = CorrectionPolicy(workflow_repository, self._stage_events, self._actions)
        self._sequencer = StageSequencer(
            workflow_repository,
            task_provider,
            config_loader,
            client_factory,
            self._stage_events,
            self._corrections,
            self._actions,
            self._lifecycle,
            self._agents,
            verbose=verbose,
            repository_index_clients=repository_index_clients,
            context_resolver=lambda *args, **kwargs: resolve_repository_context_strategy(
                *args, **kwargs
            ),
        )

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
        runtime = self._runtimes.peek(execution_id)
        self._stage_events._record_diagnostic(
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

    def submit_interrupted(
        self,
        execution_id: str,
        *,
        event_listener: WorkflowEventListener | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Queue the same execution through ordinary dispatcher admission."""
        execution = self._repository.get_execution(execution_id)
        if execution.status is not WorkflowStatus.INTERRUPTED:
            raise ValueError(f"Execution {execution_id} is not interrupted")
        if execution.resume_stage is None:
            raise WorkflowError("Interrupted execution has no restart checkpoint")
        self._repository.queue_interrupted(execution_id)
        self._runtimes.begin(execution_id, event_listener)
        self._stage_events._emit(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING
        )
        return self._submit(
            execution_id,
            lambda: self._sequencer._execute_interrupted(execution),
            admission_gate=admission_gate,
        )

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
        Queued work is in-memory; startup recovery offers its persisted row
        for explicit resubmission through the same dispatcher.

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
        self._runtimes.begin(execution.id, event_listener)
        self._stage_events._emit(
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
            lambda: self._sequencer._execute_workflow(
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
                result = work()
                if result.status is WorkflowStatus.INTERRUPTED:
                    self._stage_events._emit(
                        execution_id, WorkflowEventKind.STATUS_CHANGED,
                        stage=result.resume_stage or result.current_stage,
                        status=WorkflowStatus.INTERRUPTED,
                        message=result.error_message,
                    )
                return result
            finally:
                self._runtimes.end(execution_id)

        def drop_job() -> WorkflowExecution:
            try:
                return self._lifecycle._finish(execution_id, WorkflowStatus.CANCELLED, None)
            finally:
                self._runtimes.end(execution_id)

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
        self._runtimes.begin(execution_id, event_listener)
        # The repository atomically records the answer and queues it, so a
        # concurrent human-blocked cancellation cannot be undone here.
        self._stage_events._emit(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING
        )
        return self._submit(
            execution_id,
            lambda: self._sequencer._execute_response(execution, interaction, response),
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
            validation = StageValues._validation_from_snapshot(execution.validation_snapshot)
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
        task = StageValues._task_from_snapshot(execution)
        if stage is WorkflowStage.IMPLEMENT and not execution.spec_snapshot:
            raise WorkflowError("Cannot continue: the original repository spec was not persisted")
        config = self._config_loader.load(
            task, execution.repository_ref or execution.repository or None
        )
        provider_overrides, runtime_overrides = self._lifecycle._runtime_overrides_for_execution(
            execution_id
        )
        if provider_overrides or runtime_overrides:
            config = config.with_agent_runtime_overrides(provider_overrides, runtime_overrides)
        clients = self._client_factory.create(config)
        if stage is WorkflowStage.GENERATE_SPEC:
            # No workspace exists yet, so there is nothing to verify: planning
            # runs again and creates the workspace exactly as a fresh run would.
            self._runtimes.begin(execution_id, event_listener)
            self._stage_events._record_diagnostic(
                execution_id,
                DiagnosticCategory.WORKFLOW_EVENT,
                {"kind": _MANUAL_CONTINUATION_DIAGNOSTIC_KIND, "feedback": feedback},
                stage=WorkflowStage.GENERATE_SPEC,
            )
            if isinstance(self._repository, WorkflowRepository):
                self._repository.set_resume_mode(execution_id, "MANUAL_CONTINUATION")
                self._repository.set_resume_stage(execution_id, stage)
            self._queue_resumed(execution_id)
            return self._submit(
                execution_id,
                lambda: self._sequencer._execute_planning_retry(
                    execution, task, config, clients, feedback
                ),
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
        self._runtimes.begin(execution_id, event_listener)
        self._stage_events._record_diagnostic(
            execution_id,
            DiagnosticCategory.WORKFLOW_EVENT,
            {"kind": _MANUAL_CONTINUATION_DIAGNOSTIC_KIND, "feedback": feedback},
            stage=WorkflowStage.IMPLEMENT,
        )
        if isinstance(self._repository, WorkflowRepository):
            self._repository.set_resume_mode(execution_id, "MANUAL_CONTINUATION")
            self._repository.set_resume_stage(execution_id, stage)
        self._queue_resumed(execution_id)
        spec = RepositorySpec(**execution.spec_snapshot)
        return self._submit(
            execution_id,
            lambda: self._sequencer._execute_continuation(
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

    def _queue_resumed(self, execution_id: str) -> None:
        """Park a resumed or continued execution in the shared submission queue."""

        self._repository.queue_execution(execution_id)
        self._stage_events._emit(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING
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

    # Compatibility for callers that use these long-standing helper entry points.
    _branch_name = staticmethod(StageValues._branch_name)
    _pull_request_body = staticmethod(StageValues._pull_request_body)
    _validation_from_snapshot = staticmethod(StageValues._validation_from_snapshot)

    def _diagnostic_agent(
        self, provider: AgentProvider, config: ProjectConfig, role: AgentRole, execution_id: str
    ) -> AgentProvider:
        return self._agents._diagnostic_agent(provider, config, role, execution_id)

    def _publish_polish_skip_comment(self, **kwargs: Any) -> None:
        self._corrections._publish_polish_skip_comment(**kwargs)
