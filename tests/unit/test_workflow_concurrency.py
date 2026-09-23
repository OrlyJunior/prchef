"""Bounded concurrent workflow execution through the shared submission seam.

These tests exercise the real ``WorkflowService`` against multi-execution
fakes. Threads are coordinated with events and barriers that the production
code itself signals, never with sleeps or timing assumptions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.config.project_config import ProjectConfig
from orchestrator.models import (
    AgentRole,
    CodeReview,
    DevelopmentTask,
    HumanInteraction,
    ImplementationResult,
    PolishOutcome,
    PullRequest,
    RepositorySpec,
    StepStatus,
    ValidationResult,
    WorkflowExecution,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.agent import AgentRequest, AgentResult
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.concurrency import ProviderConcurrencyCoordinator
from orchestrator.services.exceptions import WorkflowError
from orchestrator.services.execution_dispatcher import ExecutionDispatcher
from orchestrator.services.task_provider import TrelloTaskProvider
from orchestrator.services.workflow_service import WorkflowService
from tests.unit.fakes import (
    FakeCodexClient,
    FakeCommandClient,
    FakeConfigLoader,
    FakeGitClient,
    FakeGitHubClient,
    FakeJulesClient,
    FakeTrelloClient,
)

TIMEOUT = 15.0

# The default legacy configuration routes planner/reviewer to "jules" and the
# implementer to "codex", each with the conservative ceiling of one.
PLANNER_PROVIDER = "jules"
IMPLEMENTER_PROVIDER = "codex"


def _await(event: threading.Event, description: str) -> None:
    assert event.wait(TIMEOUT), f"timed out waiting for {description}"


class _Repository:
    """A multi-execution workflow repository fake.

    The shared unit fake tracks a single execution, which cannot show whether
    concurrent executions keep their persisted state apart.
    """

    _ACTIVE = frozenset(
        {WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING}
    )

    def __init__(self) -> None:
        self.executions: dict[str, WorkflowExecution] = {}
        self.steps: dict[int, dict[str, Any]] = {}
        self.stage_changes: dict[str, list[WorkflowStage]] = {}
        self.status_changes: dict[str, list[WorkflowStatus]] = {}
        self.diagnostics: dict[str, list[Any]] = {}
        self._lock = threading.RLock()
        self._next_step = 1
        self._next_execution = 1

    def create_execution(self, task_id: str, task_source: str = "trello") -> WorkflowExecution:
        with self._lock:
            execution_id = f"execution-{self._next_execution}"
            self._next_execution += 1
            execution = WorkflowExecution(
                id=execution_id,
                task_id=task_id,
                task_source=task_source,
                trello_card_id=task_id if task_source == "trello" else "",
            )
            self.executions[execution_id] = execution
            self.stage_changes[execution_id] = []
            self.status_changes[execution_id] = [WorkflowStatus.PENDING]
            self.diagnostics[execution_id] = []
            return execution

    def _execution(self, execution_id: str) -> WorkflowExecution:
        with self._lock:
            execution = self.executions[execution_id]
            execution.updated_at = datetime.now(UTC)
            return execution

    def update_task(self, execution_id: str, task: DevelopmentTask) -> None:
        execution = self._execution(execution_id)
        execution.task_id = task.id
        execution.task_source = task.source
        execution.task_url = task.url
        execution.task_snapshot = asdict(task)

    def update_project(
        self,
        execution_id: str,
        repository: str,
        base_branch: str,
        repository_ref: str = "",
    ) -> None:
        execution = self._execution(execution_id)
        execution.repository = repository
        execution.base_branch = base_branch
        execution.repository_ref = repository_ref

    def record_workspace(self, execution_id: str, workspace_path: str) -> None:
        self._execution(execution_id).workspace_path = workspace_path

    def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
        with self._lock:
            self._execution(execution_id).current_stage = stage
            self.stage_changes[execution_id].append(stage)

    def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
        with self._lock:
            self._execution(execution_id).status = status
            self.status_changes[execution_id].append(status)

    def request_cancellation(self, execution_id: str) -> bool:
        with self._lock:
            execution = self._execution(execution_id)
            if execution.status not in self._ACTIVE:
                return False
            if execution.cancellation_requested_at is None:
                execution.cancellation_requested_at = datetime.now(UTC)
            return True

    def cancellation_requested(self, execution_id: str) -> bool:
        with self._lock:
            execution = self._execution(execution_id)
            return (
                execution.status in self._ACTIVE
                and execution.cancellation_requested_at is not None
            )

    def record_spec(self, execution_id: str, spec: RepositorySpec) -> None:
        self._execution(execution_id).spec_snapshot = asdict(spec)

    def record_spec_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        execution = self._execution(execution_id)
        execution.spec_jules_session_id = session_id
        execution.spec_jules_session_url = session_url

    def record_branch(self, execution_id: str, branch: str) -> None:
        self._execution(execution_id).working_branch = branch

    def record_implementation(self, execution_id: str, result: ImplementationResult) -> None:
        self._execution(execution_id).implementation_snapshot = asdict(result)

    def record_validation(self, execution_id: str, result: ValidationResult) -> None:
        self._execution(execution_id).validation_snapshot = asdict(result)

    def record_commit(self, execution_id: str, commit_sha: str) -> None:
        self._execution(execution_id).commit_sha = commit_sha

    def record_pull_request(self, execution_id: str, pull_request: PullRequest) -> None:
        execution = self._execution(execution_id)
        execution.pr_url = pull_request.url
        execution.pull_request_snapshot = asdict(pull_request)

    def record_review(self, execution_id: str, review: CodeReview) -> None:
        self._execution(execution_id).review_snapshot = asdict(review)

    def record_review_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        execution = self._execution(execution_id)
        execution.review_jules_session_id = session_id
        execution.review_jules_session_url = session_url

    def record_polish_outcome(self, execution_id: str, outcome: PolishOutcome) -> None:
        self._execution(execution_id).polish_outcome = outcome

    def record_polish_skip_comment_attempt(self, execution_id: str) -> None:
        self._execution(execution_id).polish_skip_comment_attempted = True

    def record_error(self, execution_id: str, message: str) -> None:
        self._execution(execution_id).error_message = message

    def record_diagnostic(self, diagnostic: Any) -> Any:
        with self._lock:
            self.diagnostics.setdefault(diagnostic.execution_id, []).append(diagnostic)
        return diagnostic

    def list_diagnostics(self, execution_id: str) -> list[Any]:
        with self._lock:
            return list(self.diagnostics.get(execution_id, []))

    def request_human_input(
        self, execution_id: str, stage: WorkflowStage, prompt: str
    ) -> HumanInteraction:
        with self._lock:
            execution = self._execution(execution_id)
            interaction = HumanInteraction(
                id=len(execution.human_interactions) + 1,
                workflow_execution_id=execution_id,
                requesting_stage=stage,
                prompt=prompt,
                requested_at=datetime.now(UTC),
            )
            execution.human_interactions.append(interaction)
            return interaction

    def respond_to_human_input(self, execution_id: str, response: str) -> HumanInteraction:
        with self._lock:
            execution = self._execution(execution_id)
            if execution.status is not WorkflowStatus.NEEDS_HUMAN:
                raise ValueError(f"Execution {execution_id} is not awaiting human input")
            interaction = next(
                item
                for item in reversed(execution.human_interactions)
                if item.response is None
            )
            interaction.response = response.strip()
            interaction.responded_at = datetime.now(UTC)
            execution.status = WorkflowStatus.PENDING
            execution.error_message = None
            execution.finished_at = None
            self.status_changes[execution_id].append(WorkflowStatus.PENDING)
            return interaction

    def resume_execution(self, execution_id: str) -> None:
        execution = self._execution(execution_id)
        execution.status = WorkflowStatus.RUNNING
        execution.error_message = None
        execution.finished_at = None

    def queue_execution(self, execution_id: str) -> None:
        with self._lock:
            execution = self._execution(execution_id)
            execution.status = WorkflowStatus.PENDING
            execution.error_message = None
            execution.finished_at = None
            execution.cancellation_requested_at = None
            self.status_changes[execution_id].append(WorkflowStatus.PENDING)

    def finish(self, execution_id: str, status: WorkflowStatus) -> None:
        with self._lock:
            execution = self._execution(execution_id)
            execution.status = status
            if status in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
                execution.current_stage = WorkflowStage.COMPLETED
            execution.finished_at = datetime.now(UTC)
            self.status_changes[execution_id].append(status)

    def start_step(self, execution_id: str, stage: WorkflowStage) -> int:
        with self._lock:
            step_id = self._next_step
            self._next_step += 1
            self.steps[step_id] = {
                "execution_id": execution_id,
                "stage": stage,
                "status": StepStatus.RUNNING,
                "error": None,
            }
            return step_id

    def finish_step(
        self, step_id: int, status: StepStatus, error_message: str | None = None
    ) -> None:
        with self._lock:
            self.steps[step_id]["status"] = status
            self.steps[step_id]["error"] = error_message

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        return self._execution(execution_id)


class _AgentGate:
    """Observe, and optionally hold open, each raw agent call by call order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self._entered: dict[str, threading.Event] = {}
        self._blocks: dict[int, threading.Event] = {}

    def block_call(self, index: int) -> threading.Event:
        """Hold the ``index``-th agent call open until the returned event is set."""

        release = threading.Event()
        self._blocks[index] = release
        return release

    def entered_event(self, name: str) -> threading.Event:
        with self._lock:
            return self._entered.setdefault(name, threading.Event())

    def enter(self, role: str) -> None:
        with self._lock:
            index = len(self.calls)
            self.calls.append(role)
        self.entered_event(f"{role}:{index}").set()
        self.entered_event(role).set()
        release = self._blocks.get(index)
        if release is not None:
            assert release.wait(TIMEOUT), f"timed out holding agent call {index}"

    def call_count(self, role: str) -> int:
        with self._lock:
            return self.calls.count(role)


class _GatedAgent:
    """Wrap a scripted agent client so the test can observe and gate it."""

    def __init__(self, inner: Any, role: str, gate: _AgentGate) -> None:
        self._inner = inner
        self._role = role
        self._gate = gate

    def execute(self, request: Any, *, on_started: Any = None) -> Any:
        self._gate.enter(self._role)
        return self._inner.execute(request, on_started=on_started)


class _ClientFactory:
    """Build clients for a workflow, optionally one independent set per execution.

    Sharing one mutable client set across *concurrent* executions would hide
    the very cross-contamination these tests exist to rule out; resuming or
    continuing a single execution, in contrast, must see the worktree state the
    original attempt left behind.
    """

    def __init__(self, gate: _AgentGate, *, shared: bool = False) -> None:
        self._gate = gate
        self._shared = shared
        self._cached: SimpleNamespace | None = None
        self.created: list[SimpleNamespace] = []

    def create(
        self, config: Any, *, agent_roles: set[AgentRole] | None = None
    ) -> SimpleNamespace:
        del agent_roles
        if self._shared and self._cached is not None:
            return self._cached
        jules = FakeJulesClient()
        codex = FakeCodexClient()
        git = FakeGitClient()
        github = FakeGitHubClient()
        commands = FakeCommandClient()
        provider_by_name = {"jules": jules, "codex": codex}
        role_names = {
            AgentRole.PLANNER: config.agents.planner.provider,
            AgentRole.IMPLEMENTER: config.agents.implementer.provider,
            AgentRole.REVIEWER: config.agents.reviewer.provider,
        }
        agents = {
            role: _GatedAgent(provider_by_name[role_names[role]], role.value, self._gate)
            for role in AgentRole
        }
        clients = SimpleNamespace(
            agent=lambda role: agents[role],
            git=git,
            github=github,
            commands=commands,
            jules=jules,
            codex=codex,
        )
        clients.at_path = lambda path: SimpleNamespace(
            agent=lambda role: agents[role],
            git=git.at_path(path),
            github=github.at_path(path),
            commands=commands,
        )
        self.created.append(clients)
        self._cached = clients
        return clients


def _service(
    tmp_path: Path,
    *,
    gate: _AgentGate,
    dispatcher: ExecutionDispatcher,
    coordinator: ProviderConcurrencyCoordinator,
    repository: _Repository | None = None,
    shared_clients: bool = False,
) -> tuple[WorkflowService, _Repository]:
    workflow_repository = repository or _Repository()
    service = WorkflowService(
        workflow_repository,  # type: ignore[arg-type]
        TrelloTaskProvider(FakeTrelloClient()),
        FakeConfigLoader(tmp_path),
        _ClientFactory(gate, shared=shared_clients),  # type: ignore[arg-type]
        dispatcher=dispatcher,
        provider_coordinator=coordinator,
    )
    return service, workflow_repository


@pytest.fixture
def dispatchers() -> Iterator[list[ExecutionDispatcher]]:
    """Shut down every dispatcher a test creates, even when it fails."""

    created: list[ExecutionDispatcher] = []
    yield created
    for dispatcher in created:
        dispatcher.shutdown(timeout=TIMEOUT)


def _dispatcher(dispatchers: list[ExecutionDispatcher], workers: int) -> ExecutionDispatcher:
    dispatcher = ExecutionDispatcher(max_workers=workers)
    dispatchers.append(dispatcher)
    return dispatcher


def _statuses(events: list[WorkflowEvent]) -> list[WorkflowStatus]:
    return [
        event.status
        for event in events
        if event.kind is WorkflowEventKind.STATUS_CHANGED and event.status is not None
    ]


def _waiting_observer(waiting: threading.Event) -> Callable[[WorkflowEvent], None]:
    def observe(event: WorkflowEvent) -> None:
        if (
            event.kind is WorkflowEventKind.STATUS_CHANGED
            and event.status is WorkflowStatus.WAITING
        ):
            waiting.set()

    return observe


def test_submission_persists_the_execution_and_returns_its_id_before_completion(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    release = gate.block_call(0)
    service, repository = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )

    handle = service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")

    assert handle.execution_id == "execution-1"
    assert handle.done() is False
    # The row exists and is inspectable long before the workflow finishes.
    assert repository.get_execution(handle.execution_id).task_id == "card-1"

    release.set()
    assert handle.result(TIMEOUT).status is WorkflowStatus.APPROVED


def test_fresh_submission_gate_keeps_execution_pending_until_reevaluated(
    tmp_path: Path, dispatchers
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    ready = False
    handle = service.submit_execution("card-1", admission_gate=lambda: ready)
    assert repository.get_execution(handle.execution_id).status is WorkflowStatus.PENDING
    assert not handle.done()
    ready = True
    service._dispatcher.reevaluate()
    assert handle.result(TIMEOUT).status is WorkflowStatus.APPROVED


@pytest.mark.parametrize("category", ["quota", "rate_limit", "capacity", "unknown", None])
def test_provider_quota_failure_emits_named_diagnostic_only_for_quota(
    tmp_path: Path, dispatchers, category: str | None
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    factory = service._client_factory
    original_create = factory.create

    class FailingAgent:
        def execute(self, request: Any, *, on_started: Any = None) -> AgentResult:
            del request, on_started
            if category is None:
                raise RuntimeError("ordinary failure")
            raise AgentProviderError("provider failed", category=category)

    def failing_create(config: Any, *, agent_roles: Any = None) -> Any:
        clients = original_create(config, agent_roles=agent_roles)
        original_agent = clients.agent
        clients.agent = lambda role: (  # type: ignore[method-assign]
            FailingAgent() if role is AgentRole.PLANNER else original_agent(role)
        )
        return clients

    factory.create = failing_create  # type: ignore[method-assign]
    events: list[WorkflowEvent] = []
    result = service.submit_execution("card-1", event_listener=events.append).result(TIMEOUT)
    assert result.status is WorkflowStatus.FAILED
    quota_events = [
        event for event in events
        if event.kind is WorkflowEventKind.DIAGNOSTIC
        and (event.payload or {}).get("kind") == "PROVIDER_QUOTA_EXHAUSTED"
    ]
    assert len(quota_events) == (1 if category == "quota" else 0)
    if quota_events:
        assert quota_events[0].payload == {
            "kind": "PROVIDER_QUOTA_EXHAUSTED",
            "provider": PLANNER_PROVIDER,
            "role": "planner",
        }
        assert events.index(quota_events[0]) < next(
            index for index, event in enumerate(events)
            if event.kind is WorkflowEventKind.WORKFLOW_FINISHED
        )
        assert any(
            diagnostic.payload.get("kind") == "PROVIDER_QUOTA_EXHAUSTED"
            for diagnostic in repository.list_diagnostics(result.id)
        )


def _fail_planner_while(service: WorkflowService, failing: Callable[[], Exception | None]) -> None:
    """Make the planner raise ``failing()`` whenever it returns an exception."""

    factory = service._client_factory
    original_create = factory.create

    class MaybeFailingPlanner:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate

        def execute(self, request: Any, *, on_started: Any = None) -> AgentResult:
            error = failing()
            if error is not None:
                raise error
            return self._delegate.execute(request, on_started=on_started)

    def create(config: Any, *, agent_roles: Any = None) -> Any:
        clients = original_create(config, agent_roles=agent_roles)
        original_agent = clients.agent
        clients.agent = lambda role: (  # type: ignore[method-assign]
            MaybeFailingPlanner(original_agent(role))
            if role is AgentRole.PLANNER
            else original_agent(role)
        )
        return clients

    factory.create = create  # type: ignore[method-assign]


def test_planning_quota_failure_is_continued_by_repeating_planning(
    tmp_path: Path, dispatchers
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    quota_exhausted = True
    _fail_planner_while(
        service,
        lambda: AgentProviderError("quota", category="quota") if quota_exhausted else None,
    )

    failed = service.submit_execution("card-1").result(TIMEOUT)

    assert failed.status is WorkflowStatus.FAILED
    assert failed.workspace_path is None and failed.working_branch is None
    assert (
        WorkflowService.continuation_stage(failed, lambda: repository.list_diagnostics(failed.id))
        is WorkflowStage.GENERATE_SPEC
    )
    assert "quota was exhausted" in service.continuation_feedback(failed.id)

    quota_exhausted = False
    ready = False
    handle = service.submit_continuation(
        failed.id, "Quota reset; plan again.", admission_gate=lambda: ready
    )
    assert repository.get_execution(failed.id).status is WorkflowStatus.PENDING
    ready = True
    service._dispatcher.reevaluate()
    continued = handle.result(TIMEOUT)

    assert continued.id == failed.id
    assert continued.status is WorkflowStatus.APPROVED
    assert continued.workspace_path is not None
    assert continued.working_branch is not None
    assert continued.spec_snapshot


@pytest.mark.parametrize("retried_quota", [False, True])
def test_ordinary_planning_failure_is_not_continuable(
    tmp_path: Path, dispatchers, retried_quota: bool
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    failures: list[Exception] = [RuntimeError("ordinary failure")]
    if retried_quota:
        # A quota failure that was already continued must not make the
        # ordinary failure of the repeated planning attempt eligible again.
        failures.insert(0, AgentProviderError("quota", category="quota"))
    _fail_planner_while(service, lambda: failures.pop(0) if failures else None)

    failed = service.submit_execution("card-1").result(TIMEOUT)
    if retried_quota:
        failed = service.submit_continuation(failed.id, "Plan again.").result(TIMEOUT)

    assert failed.status is WorkflowStatus.FAILED
    assert failed.workspace_path is None
    assert (
        WorkflowService.continuation_stage(failed, lambda: repository.list_diagnostics(failed.id))
        is None
    )
    with pytest.raises(WorkflowError, match="can be continued"):
        service.submit_continuation(failed.id, "Try again.")


def test_excess_submissions_stay_pending_rather_than_failing(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    release = gate.block_call(0)
    service, repository = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )

    first = service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")
    second = service.submit_execution("card-2")

    # The dispatcher is bounded, so the second submission is persisted and
    # queued rather than rejected or started.
    assert second.execution_id == "execution-2"
    assert repository.get_execution(second.execution_id).status is WorkflowStatus.PENDING
    assert repository.stage_changes["execution-2"] == []
    assert second.done() is False

    release.set()
    assert first.result(TIMEOUT).status is WorkflowStatus.APPROVED
    assert second.result(TIMEOUT).status is WorkflowStatus.APPROVED


def test_run_wraps_the_same_handle_and_preserves_synchronous_behaviour(
    tmp_path: Path, dispatchers
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )

    execution = service.run("card-1")

    assert execution.status is WorkflowStatus.APPROVED
    assert execution.pr_url == "https://github.example/pr/42"
    assert repository.get_execution(execution.id).status is WorkflowStatus.APPROVED


def test_lifecycle_moves_pending_to_running_and_then_to_a_terminal_status(
    tmp_path: Path, dispatchers
) -> None:
    events: list[WorkflowEvent] = []
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )

    execution = service.run("card-1", event_listener=events.append)

    assert repository.status_changes[execution.id] == [
        WorkflowStatus.PENDING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.APPROVED,
    ]
    assert _statuses(events) == [WorkflowStatus.RUNNING, WorkflowStatus.APPROVED]
    # Status events are persisted, not only streamed to the observer.
    persisted = [
        diagnostic.payload
        for diagnostic in repository.list_diagnostics(execution.id)
        if diagnostic.payload.get("kind") == WorkflowEventKind.STATUS_CHANGED.value
    ]
    assert [payload["status"] for payload in persisted] == ["RUNNING", "APPROVED"]


def test_provider_capacity_moves_a_started_execution_through_waiting(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    release_first_planner = gate.block_call(0)
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 1, IMPLEMENTER_PROVIDER: 2})
    dispatcher = _dispatcher(dispatchers, 2)
    repository = _Repository()
    first_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    second_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    waiting = threading.Event()
    admitted = threading.Event()
    second_events: list[WorkflowEvent] = []

    def observe(event: WorkflowEvent) -> None:
        second_events.append(event)
        if event.kind is not WorkflowEventKind.STATUS_CHANGED:
            return
        if event.status is WorkflowStatus.WAITING:
            waiting.set()
        elif event.status is WorkflowStatus.RUNNING and waiting.is_set():
            admitted.set()

    first = first_service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")
    second = second_service.submit_execution("card-2", event_listener=observe)

    _await(waiting, "the second execution to report WAITING")
    assert repository.get_execution(second.execution_id).status is WorkflowStatus.WAITING
    waiting_event = next(
        event
        for event in second_events
        if event.kind is WorkflowEventKind.STATUS_CHANGED
        and event.status is WorkflowStatus.WAITING
    )
    # The sanitized event identifies the provider the run is queued behind.
    assert waiting_event.payload is not None
    assert waiting_event.payload["provider"] == PLANNER_PROVIDER
    assert PLANNER_PROVIDER in (waiting_event.message or "")

    release_first_planner.set()
    _await(admitted, "the second execution to be admitted")
    assert first.result(TIMEOUT).status is WorkflowStatus.APPROVED
    assert second.result(TIMEOUT).status is WorkflowStatus.APPROVED

    statuses = repository.status_changes[second.execution_id]
    assert WorkflowStatus.WAITING in statuses
    assert statuses.index(WorkflowStatus.RUNNING) < statuses.index(WorkflowStatus.WAITING)
    assert statuses[-1] is WorkflowStatus.APPROVED


def test_a_busy_provider_does_not_block_a_different_provider(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 1, IMPLEMENTER_PROVIDER: 1})
    service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=coordinator,
    )
    implementer_held = threading.Event()
    release_implementer = threading.Event()

    def hold_implementer() -> None:
        with coordinator.slot(IMPLEMENTER_PROVIDER):
            implementer_held.set()
            assert release_implementer.wait(TIMEOUT)

    holder = threading.Thread(target=hold_implementer, daemon=True)
    holder.start()
    _await(implementer_held, "the external implementer slot holder")

    handle = service.submit_execution("card-1")

    # Planning uses a different configured provider, so it proceeds while the
    # implementer's only slot is still taken by someone else.
    _await(gate.entered_event("planner"), "the planner call")
    assert coordinator.active_count(IMPLEMENTER_PROVIDER) == 1

    release_implementer.set()
    holder.join(TIMEOUT)
    assert not holder.is_alive()
    assert handle.result(TIMEOUT).status is WorkflowStatus.APPROVED


def test_concurrent_executions_keep_their_listeners_stages_and_workspaces_apart(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 2, IMPLEMENTER_PROVIDER: 2})
    dispatcher = _dispatcher(dispatchers, 2)
    repository = _Repository()
    first_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    second_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    first_events: list[WorkflowEvent] = []
    second_events: list[WorkflowEvent] = []

    # Hold both executions inside their first planner call at the same time, so
    # they are provably in flight together rather than merely interleaved.
    release_first = gate.block_call(0)
    release_second = gate.block_call(1)

    first = first_service.submit_execution("card-1", event_listener=first_events.append)
    _await(gate.entered_event("planner:0"), "the first execution's planner call")
    second = second_service.submit_execution("card-2", event_listener=second_events.append)
    _await(gate.entered_event("planner:1"), "the second execution's planner call")

    assert coordinator.active_count(PLANNER_PROVIDER) == 2
    release_first.set()
    release_second.set()

    first_execution = first.result(TIMEOUT)
    second_execution = second.result(TIMEOUT)

    assert first_execution.id != second_execution.id
    assert first_execution.status is WorkflowStatus.APPROVED
    assert second_execution.status is WorkflowStatus.APPROVED

    # Each listener only ever saw its own execution.
    assert first_events and second_events
    assert {event.execution_id for event in first_events} == {first_execution.id}
    assert {event.execution_id for event in second_events} == {second_execution.id}

    # Branches and worktrees stay distinct and carry their own execution IDs.
    assert first_execution.working_branch != second_execution.working_branch
    assert first_execution.id in str(first_execution.working_branch)
    assert second_execution.id in str(second_execution.working_branch)
    assert first_execution.workspace_path != second_execution.workspace_path
    assert first_execution.id in str(first_execution.workspace_path)
    assert second_execution.id in str(second_execution.workspace_path)

    # Every persisted diagnostic and step is attributed to one execution.
    for execution in (first_execution, second_execution):
        diagnostics = repository.list_diagnostics(execution.id)
        assert diagnostics
        assert {diagnostic.execution_id for diagnostic in diagnostics} == {execution.id}
    assert {step["execution_id"] for step in repository.steps.values()} == {
        first_execution.id,
        second_execution.id,
    }
    assert WorkflowStage.REVIEW in repository.stage_changes[first_execution.id]
    assert WorkflowStage.REVIEW in repository.stage_changes[second_execution.id]

    # Worktree-bound clients were constructed per execution.
    first_clients = first_service._client_factory.created
    second_clients = second_service._client_factory.created
    assert len(first_clients) == len(second_clients) == 1
    assert first_clients[0].git is not second_clients[0].git


def test_a_queued_pending_execution_is_cancelled_without_running_a_stage(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    release = gate.block_call(0)
    service, repository = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )

    first = service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")
    second = service.submit_execution("card-2")

    assert service.cancel_execution(second.execution_id) is True

    cancelled = second.result(TIMEOUT)
    assert cancelled.status is WorkflowStatus.CANCELLED
    assert cancelled.error_message is None
    # It never entered a stage or reached a provider.
    assert repository.stage_changes[second.execution_id] == []
    assert gate.call_count("planner") == 1

    release.set()
    assert first.result(TIMEOUT).status is WorkflowStatus.APPROVED


def test_an_execution_waiting_for_provider_capacity_is_cancellable(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    release_first_planner = gate.block_call(0)
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 1, IMPLEMENTER_PROVIDER: 2})
    dispatcher = _dispatcher(dispatchers, 2)
    repository = _Repository()
    first_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    second_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    waiting = threading.Event()

    first = first_service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")
    second = second_service.submit_execution(
        "card-2", event_listener=_waiting_observer(waiting)
    )
    _await(waiting, "the second execution to report WAITING")

    assert second_service.cancel_execution(second.execution_id) is True

    cancelled = second.result(TIMEOUT)
    assert cancelled.status is WorkflowStatus.CANCELLED
    # Only the first execution's planner call ever ran.
    assert gate.call_count("planner") == 1
    assert repository.stage_changes[second.execution_id] == [
        WorkflowStage.LOAD_TASK,
        WorkflowStage.LOAD_CONFIG,
        WorkflowStage.GENERATE_SPEC,
    ]

    release_first_planner.set()
    assert first.result(TIMEOUT).status is WorkflowStatus.APPROVED
    # No permit leaked: the coordinator is idle once both executions are done.
    assert coordinator.active_count(PLANNER_PROVIDER) == 0
    assert coordinator.queued_count(PLANNER_PROVIDER) == 0


def test_cancellation_requested_before_admission_stops_the_agent_call(
    tmp_path: Path, dispatchers
) -> None:
    """Cancellation and admission may resolve in either order; both end CANCELLED."""

    gate = _AgentGate()
    release_first_planner = gate.block_call(0)
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 1, IMPLEMENTER_PROVIDER: 2})
    dispatcher = _dispatcher(dispatchers, 2)
    repository = _Repository()
    first_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    second_service, _ = _service(
        tmp_path,
        gate=gate,
        dispatcher=dispatcher,
        coordinator=coordinator,
        repository=repository,
    )
    waiting = threading.Event()

    first = first_service.submit_execution("card-1")
    _await(gate.entered_event("planner"), "the first planner call")
    second = second_service.submit_execution(
        "card-2", event_listener=_waiting_observer(waiting)
    )
    _await(waiting, "the second execution to report WAITING")

    # Persist the request without waking the limiter, then free the slot. The
    # waiter is admitted normally and must still refuse to call the provider.
    assert repository.request_cancellation(second.execution_id) is True
    release_first_planner.set()

    assert second.result(TIMEOUT).status is WorkflowStatus.CANCELLED
    assert gate.call_count("planner") == 1
    assert first.result(TIMEOUT).status is WorkflowStatus.APPROVED
    assert coordinator.active_count(PLANNER_PROVIDER) == 0
    assert coordinator.queued_count(PLANNER_PROVIDER) == 0

    # A repeated request against terminal work is refused rather than retried.
    assert second_service.cancel_execution(second.execution_id) is False


def test_cancelling_a_running_execution_stops_before_the_next_stage(
    tmp_path: Path, dispatchers
) -> None:
    service, repository = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    original = repository.record_implementation

    def cancel_after_implementation(execution_id: str, result: Any) -> None:
        original(execution_id, result)
        assert service.cancel_execution(execution_id) is True

    repository.record_implementation = cancel_after_implementation  # type: ignore[method-assign]

    execution = service.run("card-1")

    assert execution.status is WorkflowStatus.CANCELLED
    assert WorkflowStage.VALIDATE not in repository.stage_changes[execution.id]
    assert WorkflowStage.COMMIT not in repository.stage_changes[execution.id]
    # The workspace is preserved for inspection rather than discarded.
    assert execution.workspace_path is not None


def _paused_planner_execution(
    tmp_path: Path, dispatchers
) -> tuple[WorkflowService, _Repository, _AgentGate, WorkflowExecution]:
    """Run one workflow to a planner NEEDS_HUMAN pause and return its state."""

    gate = _AgentGate()
    service, repository = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    factory = service._client_factory
    plain_create = factory.create

    def ambiguous_create(config: Any, *, agent_roles: Any = None) -> Any:
        clients = plain_create(config, agent_roles=agent_roles)
        clients.jules.ambiguous = True
        return clients

    factory.create = ambiguous_create  # type: ignore[method-assign]
    paused = service.run("card-1")
    assert paused.status is WorkflowStatus.NEEDS_HUMAN

    factory.create = plain_create  # type: ignore[method-assign]
    return service, repository, gate, paused


def test_human_response_resume_uses_the_shared_submission_seam(
    tmp_path: Path, dispatchers
) -> None:
    service, repository, gate, paused = _paused_planner_execution(tmp_path, dispatchers)
    planner_calls_before = gate.call_count("planner")

    ready = False
    handle = service.submit_response(
        paused.id, "Use a 30 day retention period.", admission_gate=lambda: ready
    )

    # The resume keeps the original execution ID and returns a handle.
    assert handle.execution_id == paused.id
    assert repository.get_execution(paused.id).status is WorkflowStatus.PENDING
    ready = True
    service._dispatcher.reevaluate()
    resumed = handle.result(TIMEOUT)

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED
    assert gate.call_count("planner") > planner_calls_before
    # It was re-queued as PENDING before a worker admitted it.
    assert repository.status_changes[paused.id][-3:] == [
        WorkflowStatus.PENDING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.APPROVED,
    ]


def test_respond_and_resume_waits_on_the_same_handle(tmp_path: Path, dispatchers) -> None:
    service, _, _, paused = _paused_planner_execution(tmp_path, dispatchers)

    resumed = service.respond_and_resume(paused.id, "Use a 30 day retention period.")

    assert resumed.id == paused.id
    assert resumed.status is WorkflowStatus.APPROVED


def test_resume_respects_the_shared_provider_ceiling(tmp_path: Path, dispatchers) -> None:
    service, repository, _, paused = _paused_planner_execution(tmp_path, dispatchers)
    coordinator = service._provider_coordinator
    holder_admitted = threading.Event()
    release_holder = threading.Event()

    def hold_planner() -> None:
        with coordinator.slot(PLANNER_PROVIDER):
            holder_admitted.set()
            assert release_holder.wait(TIMEOUT)

    holder = threading.Thread(target=hold_planner, daemon=True)
    holder.start()
    _await(holder_admitted, "the external planner slot holder")

    waiting = threading.Event()
    handle = service.submit_response(
        paused.id,
        "Use a 30 day retention period.",
        event_listener=_waiting_observer(waiting),
    )
    _await(waiting, "the resume to report WAITING")

    # A resume competes for the same bounded capacity as a fresh run.
    assert repository.get_execution(paused.id).status is WorkflowStatus.WAITING

    release_holder.set()
    holder.join(TIMEOUT)
    assert not holder.is_alive()
    assert handle.result(TIMEOUT).status is WorkflowStatus.APPROVED


def test_manual_continuation_uses_the_shared_submission_seam(
    tmp_path: Path, dispatchers
) -> None:
    gate = _AgentGate()
    service, repository = _service(
        tmp_path,
        gate=gate,
        dispatcher=_dispatcher(dispatchers, 1),
        coordinator=ProviderConcurrencyCoordinator(),
        # A continuation reuses the retained worktree the first attempt left.
        shared_clients=True,
    )
    factory = service._client_factory
    plain_create = factory.create

    def changes_requested_create(config: Any, *, agent_roles: Any = None) -> Any:
        clients = plain_create(config, agent_roles=agent_roles)
        clients.jules.changes_requested = True
        return clients

    factory.create = changes_requested_create  # type: ignore[method-assign]
    terminal = service.run("card-1")
    assert terminal.status is WorkflowStatus.CHANGES_REQUESTED
    factory.create = plain_create  # type: ignore[method-assign]
    service._client_factory.created[0].jules.changes_requested = False

    ready = False
    handle = service.submit_continuation(
        terminal.id, "Also cover the boundary case.", admission_gate=lambda: ready
    )

    assert handle.execution_id == terminal.id
    assert repository.get_execution(terminal.id).status is WorkflowStatus.PENDING
    ready = True
    service._dispatcher.reevaluate()
    continued = handle.result(TIMEOUT)

    assert continued.id == terminal.id
    assert continued.status is WorkflowStatus.APPROVED
    assert repository.status_changes[terminal.id][-3:] == [
        WorkflowStatus.PENDING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.APPROVED,
    ]


def test_every_entry_point_shares_one_dispatcher_and_coordinator(
    tmp_path: Path, dispatchers
) -> None:
    dispatcher = _dispatcher(dispatchers, 2)
    coordinator = ProviderConcurrencyCoordinator({PLANNER_PROVIDER: 1})
    service, _ = _service(
        tmp_path, gate=_AgentGate(), dispatcher=dispatcher, coordinator=coordinator
    )

    assert service._dispatcher is dispatcher
    assert service._provider_coordinator is coordinator


def test_an_isolated_service_defaults_to_strictly_sequential_capacity(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        gate=_AgentGate(),
        dispatcher=ExecutionDispatcher(),
        coordinator=ProviderConcurrencyCoordinator(),
    )
    try:
        assert service._provider_coordinator.limit_for("anything") == 1
        assert service._dispatcher.max_workers == 1
    finally:
        service._dispatcher.shutdown(timeout=TIMEOUT)


def _fallback_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "primary": {"type": "codex-cli", "command": ["codex"]},
                "backup": {"type": "codex-cli", "command": ["codex"]},
            },
            "agents": {
                "planner": {"provider": "primary", "fallback_preset": "backup-preset"},
                "implementer": {"provider": "primary"},
                "reviewer": {"provider": "primary"},
            },
            "agent_presets": {"planner": {"backup-preset": {"provider": "backup"}}},
        }
    )


class _DiagnosticsOnlyRepository:
    """The minimum repository surface a bare ``_diagnostic_agent`` call needs."""

    def record_diagnostic(self, diagnostic: Any) -> None:
        return None

    def cancellation_requested(self, execution_id: str) -> bool:
        del execution_id
        return False

    def update_status(self, execution_id: str, status: Any) -> None:
        return None


def _fallback_provider(
    tmp_path: Path,
    coordinator: ProviderConcurrencyCoordinator,
    *,
    on_primary: Callable[[], None],
    on_fallback: Callable[[], None],
) -> Any:
    """Build the planner provider chain for a quota-failing primary."""

    config = _fallback_config(tmp_path)

    class _Backup:
        def execute(self, request: Any, *, on_started: Any = None) -> AgentResult:
            del request, on_started
            on_fallback()
            return AgentResult(output="fallback")

    class _Factory:
        def create(self, config: Any, *, agent_roles: Any = None) -> Any:
            del config, agent_roles
            return SimpleNamespace(agent=lambda role: _Backup())

    class _Primary:
        def execute(self, request: Any, *, on_started: Any = None) -> AgentResult:
            del request, on_started
            on_primary()
            raise AgentProviderError("primary quota exhausted", category="quota")

    service = WorkflowService(
        _DiagnosticsOnlyRepository(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _Factory(),  # type: ignore[arg-type]
        provider_coordinator=coordinator,
    )
    return service._diagnostic_agent(_Primary(), config, AgentRole.PLANNER, "execution-1")


def test_fallback_releases_the_primary_slot_before_taking_the_fallback_slot(
    tmp_path: Path,
) -> None:
    coordinator = ProviderConcurrencyCoordinator({"primary": 1, "backup": 1})
    during_primary: list[tuple[int, int]] = []
    during_fallback: list[tuple[int, int]] = []

    def counts() -> tuple[int, int]:
        return coordinator.active_count("primary"), coordinator.active_count("backup")

    provider = _fallback_provider(
        tmp_path,
        coordinator,
        on_primary=lambda: during_primary.append(counts()),
        on_fallback=lambda: during_fallback.append(counts()),
    )

    result = provider.execute(AgentRequest(instructions="plan", title="Plan"))

    assert result.output == "fallback"
    # The primary held only its own slot...
    assert during_primary == [(1, 0)]
    # ...and it was released before the fallback acquired its provider's slot.
    assert during_fallback == [(0, 1)]
    assert coordinator.active_count("primary") == 0
    assert coordinator.active_count("backup") == 0


def test_fallback_concurrency_is_charged_to_its_own_configured_provider(
    tmp_path: Path,
) -> None:
    coordinator = ProviderConcurrencyCoordinator({"primary": 1, "backup": 1})
    provider = _fallback_provider(
        tmp_path, coordinator, on_primary=lambda: None, on_fallback=lambda: None
    )

    provider.execute(AgentRequest(instructions="plan", title="Plan"))

    # The preset-derived runtime variant registered no capacity of its own: the
    # summed ceilings still cover exactly the two configured providers.
    assert coordinator.total_capacity() == 2
    assert coordinator.limit_for("backup") == 1
    assert coordinator.active_count("backup") == 0

    # The synthetic name a preset materializes resolves back to "backup".
    fallback_config = _fallback_config(tmp_path).with_agent_preset("planner", "backup-preset")
    runtime_name = fallback_config.agents.planner.provider
    assert runtime_name.startswith("__tui_runtime_")
    assert fallback_config.provider_concurrency_name(runtime_name) == "backup"
