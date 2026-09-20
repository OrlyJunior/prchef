"""Provider admission and execution submission behaviour.

Every test here coordinates threads through explicit events, barriers, or
state the production code itself signals (``on_wait``/``on_admitted``). No test
sleeps or assumes an ordering that timing alone would produce.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pytest

from orchestrator.models.agent import AgentRequest, AgentResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.providers.concurrency import (
    ConcurrencyLimitedAgentProvider,
    ProviderConcurrencyCoordinator,
    ProviderSlotCancelled,
)
from orchestrator.services.execution_dispatcher import ExecutionDispatcher

# Every blocking wait in this module is bounded so a regression fails the suite
# instead of hanging it.
TIMEOUT = 10.0


def _await(event: threading.Event) -> None:
    assert event.wait(TIMEOUT), "timed out waiting for the coordinated state"


@pytest.fixture
def threads() -> Iterator[list[threading.Thread]]:
    """Join every thread a test starts, even when an assertion fails."""

    started: list[threading.Thread] = []
    yield started
    for thread in started:
        thread.join(TIMEOUT)
        assert not thread.is_alive(), "a worker thread did not finish"


def _spawn(threads: list[threading.Thread], target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    threads.append(thread)
    thread.start()
    return thread


class _RecordingProvider:
    """An agent provider that blocks until the test lets it finish."""

    def __init__(self, release: threading.Event | None = None) -> None:
        self._release = release
        self.entered = threading.Event()
        self.calls = 0
        self.error: Exception | None = None

    def execute(self, request: AgentRequest, *, on_started: object = None) -> AgentResult:
        del on_started
        self.calls += 1
        self.entered.set()
        if self._release is not None:
            assert self._release.wait(TIMEOUT)
        if self.error is not None:
            raise self.error
        return AgentResult(output=request.title)


def _request(title: str = "work") -> AgentRequest:
    return AgentRequest(instructions="do the thing", title=title)


def test_default_ceiling_is_one_and_serializes_a_provider(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator()
    release_first = threading.Event()
    first_admitted = threading.Event()
    second_waiting = threading.Event()
    second_admitted = threading.Event()

    assert coordinator.limit_for("ollama") == 1

    def first() -> None:
        with coordinator.slot("ollama"):
            first_admitted.set()
            _await(release_first)

    def second() -> None:
        with coordinator.slot("ollama", on_wait=second_waiting.set):
            second_admitted.set()

    _spawn(threads, first)
    _await(first_admitted)
    _spawn(threads, second)

    # The second call is genuinely blocked, not merely slower to start.
    _await(second_waiting)
    assert not second_admitted.is_set()
    assert coordinator.active_count("ollama") == 1
    assert coordinator.queued_count("ollama") == 1

    release_first.set()
    _await(second_admitted)
    assert coordinator.active_count("ollama") == 0


def test_provider_never_exceeds_its_configured_ceiling(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 2})
    # A barrier of exactly the ceiling proves two calls really do overlap; a
    # third waiter proves the ceiling is not exceeded.
    overlap = threading.Barrier(2, timeout=TIMEOUT)
    release = threading.Event()
    third_waiting = threading.Event()
    third_admitted = threading.Event()

    def concurrent() -> None:
        with coordinator.slot("codex"):
            overlap.wait()
            _await(release)

    _spawn(threads, concurrent)
    _spawn(threads, concurrent)

    def third() -> None:
        with coordinator.slot("codex", on_wait=third_waiting.set):
            third_admitted.set()

    _spawn(threads, third)
    _await(third_waiting)

    assert coordinator.active_count("codex") == 2
    assert not third_admitted.is_set()

    release.set()
    _await(third_admitted)


def test_separate_provider_names_admit_independently(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"ollama": 1, "claude": 1})
    # The test thread is the third party, so it only inspects the coordinator
    # once both calls are provably inside their slots at the same time.
    both_running = threading.Barrier(3, timeout=TIMEOUT)
    release = threading.Event()

    def hold(provider: str) -> Callable[[], None]:
        def run() -> None:
            with coordinator.slot(provider):
                # Only reached by both threads if the two names do not contend.
                both_running.wait()
                _await(release)

        return run

    _spawn(threads, hold("ollama"))
    _spawn(threads, hold("claude"))
    both_running.wait()

    assert coordinator.active_count("ollama") == 1
    assert coordinator.active_count("claude") == 1
    release.set()


def test_queued_calls_are_admitted_in_fifo_order(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    holder_admitted = threading.Event()
    release_holder = threading.Event()
    admissions: list[str] = []
    admission_lock = threading.Lock()
    finished = threading.Semaphore(0)

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)

    def queued(name: str, waiting: threading.Event) -> Callable[[], None]:
        def run() -> None:
            with coordinator.slot("codex", on_wait=waiting.set):
                with admission_lock:
                    admissions.append(name)
            finished.release()

        return run

    _spawn(threads, holder)
    _await(holder_admitted)

    # Enqueue one at a time, each confirmed queued before the next is started,
    # so the expected FIFO order is established rather than assumed.
    order = ["first", "second", "third"]
    for name in order:
        waiting = threading.Event()
        _spawn(threads, queued(name, waiting))
        _await(waiting)

    assert coordinator.queued_count("codex") == 3
    release_holder.set()
    for _ in order:
        assert finished.acquire(timeout=TIMEOUT)

    assert admissions == order


def test_permits_are_released_after_success_and_after_failure() -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})

    with coordinator.slot("codex"):
        assert coordinator.active_count("codex") == 1
    assert coordinator.active_count("codex") == 0

    with pytest.raises(RuntimeError, match="provider exploded"):
        with coordinator.slot("codex"):
            raise RuntimeError("provider exploded")
    assert coordinator.active_count("codex") == 0

    # The ceiling is still usable, so nothing leaked.
    with coordinator.slot("codex"):
        assert coordinator.active_count("codex") == 1


def test_wait_hook_failure_abandons_its_fifo_ticket(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    holder_admitted = threading.Event()
    release_holder = threading.Event()
    holder_finished = threading.Event()
    waiting = threading.Event()
    hook_failed = threading.Event()

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)
        holder_finished.set()

    def raise_while_waiting() -> None:
        waiting.set()
        raise RuntimeError("wait hook exploded")

    def queued() -> None:
        try:
            with coordinator.slot("codex", on_wait=raise_while_waiting):
                raise AssertionError("a failed wait hook must not be admitted")
        except RuntimeError as exc:
            assert str(exc) == "wait hook exploded"
            hook_failed.set()

    _spawn(threads, holder)
    _await(holder_admitted)
    _spawn(threads, queued)
    _await(waiting)
    _await(hook_failed)

    assert coordinator.queued_count("codex") == 0
    release_holder.set()
    _await(holder_finished)
    with coordinator.slot("codex"):
        assert coordinator.active_count("codex") == 1


def test_admission_hook_failure_releases_its_provider_permit(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    holder_admitted = threading.Event()
    release_holder = threading.Event()
    holder_finished = threading.Event()
    waiting = threading.Event()
    hook_failed = threading.Event()

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)
        holder_finished.set()

    def raise_after_admission() -> None:
        raise RuntimeError("admission hook exploded")

    def queued() -> None:
        try:
            with coordinator.slot(
                "codex", on_wait=waiting.set, on_admitted=raise_after_admission
            ):
                raise AssertionError("a failed admission hook must not enter the slot")
        except RuntimeError as exc:
            assert str(exc) == "admission hook exploded"
            hook_failed.set()

    _spawn(threads, holder)
    _await(holder_admitted)
    _spawn(threads, queued)
    _await(waiting)
    release_holder.set()
    _await(holder_finished)
    _await(hook_failed)

    assert coordinator.active_count("codex") == 0
    with coordinator.slot("codex"):
        assert coordinator.active_count("codex") == 1


def test_first_registration_fixes_the_ceiling_for_the_process() -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 2})

    assert coordinator.register("codex", 9) == 2
    assert coordinator.limit_for("codex") == 2
    assert coordinator.register("gemini", 3) == 3
    assert coordinator.limit_for("gemini") == 3


def test_total_capacity_sizes_worker_pools_from_configured_ceilings() -> None:
    assert ProviderConcurrencyCoordinator({"a": 2, "b": 3}).total_capacity() == 5
    # An empty configuration still supports one worker.
    assert ProviderConcurrencyCoordinator().total_capacity() == 1


def test_cancelling_a_queued_call_raises_and_leaves_no_permit(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    holder_admitted = threading.Event()
    release_holder = threading.Event()
    waiting = threading.Event()
    cancelled = threading.Event()
    admitted = threading.Event()

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)

    def queued() -> None:
        try:
            with coordinator.slot("codex", execution_id="exec-1", on_wait=waiting.set):
                admitted.set()
        except ProviderSlotCancelled:
            cancelled.set()

    _spawn(threads, holder)
    _await(holder_admitted)
    _spawn(threads, queued)
    _await(waiting)

    assert coordinator.cancel("exec-1") is True
    _await(cancelled)
    assert not admitted.is_set()
    assert coordinator.queued_count("codex") == 0

    release_holder.set()


def test_cancelling_an_unknown_or_admitted_execution_reports_nothing_woken() -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})

    assert coordinator.cancel("missing") is False
    with coordinator.slot("codex", execution_id="exec-1"):
        # An admitted call is not interrupted here; the workflow's own
        # cooperative check stops it at the next boundary.
        assert coordinator.cancel("exec-1") is False


def test_repeated_cancellation_of_queued_work_is_idempotent(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    holder_admitted = threading.Event()
    release_holder = threading.Event()
    waiting = threading.Event()
    cancelled = threading.Event()

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)

    def queued() -> None:
        try:
            with coordinator.slot("codex", execution_id="exec-1", on_wait=waiting.set):
                pass
        except ProviderSlotCancelled:
            cancelled.set()

    _spawn(threads, holder)
    _await(holder_admitted)
    _spawn(threads, queued)
    _await(waiting)

    assert coordinator.cancel("exec-1") is True
    # A second request must not raise or corrupt the queue bookkeeping.
    coordinator.cancel("exec-1")
    _await(cancelled)
    assert coordinator.cancel("exec-1") is False

    release_holder.set()


def test_invalid_default_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ProviderConcurrencyCoordinator(default_limit=0)


def test_limited_provider_holds_a_permit_only_for_the_agent_call(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    release = threading.Event()
    inner = _RecordingProvider(release)
    waits: list[str] = []
    admissions: list[str] = []
    limited = ConcurrencyLimitedAgentProvider(
        inner,
        provider_name="codex",
        coordinator=coordinator,
        limit=1,
        execution_id="exec-1",
        on_wait=waits.append,
        on_admitted=admissions.append,
    )

    _spawn(threads, lambda: limited.execute(_request()))
    _await(inner.entered)

    assert coordinator.active_count("codex") == 1
    # Admitted immediately, so no wait was reported.
    assert waits == []
    assert admissions == []

    release.set()


def test_limited_provider_reports_wait_and_admission_around_a_busy_provider(threads) -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    release_holder = threading.Event()
    holder_admitted = threading.Event()
    waits: list[str] = []
    admissions: list[str] = []
    second_done = threading.Event()
    waiting = threading.Event()

    def holder() -> None:
        with coordinator.slot("codex"):
            holder_admitted.set()
            _await(release_holder)

    limited = ConcurrencyLimitedAgentProvider(
        _RecordingProvider(),
        provider_name="codex",
        coordinator=coordinator,
        execution_id="exec-2",
        on_wait=lambda name: (waits.append(name), waiting.set()),
        on_admitted=admissions.append,
    )

    _spawn(threads, holder)
    _await(holder_admitted)
    _spawn(threads, lambda: (limited.execute(_request()), second_done.set()))
    _await(waiting)

    assert waits == ["codex"]
    assert admissions == []

    release_holder.set()
    _await(second_done)
    assert admissions == ["codex"]


def test_limited_provider_releases_the_permit_when_the_agent_raises() -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    inner = _RecordingProvider()
    inner.error = RuntimeError("provider exploded")
    limited = ConcurrencyLimitedAgentProvider(
        inner, provider_name="codex", coordinator=coordinator
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        limited.execute(_request())

    assert coordinator.active_count("codex") == 0


def test_limited_provider_checks_cancellation_before_acquiring() -> None:
    coordinator = ProviderConcurrencyCoordinator({"codex": 1})
    inner = _RecordingProvider()

    def refuse() -> None:
        raise RuntimeError("cancelled")

    limited = ConcurrencyLimitedAgentProvider(
        inner,
        provider_name="codex",
        coordinator=coordinator,
        cancellation_check=refuse,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        limited.execute(_request())

    assert inner.calls == 0
    assert coordinator.active_count("codex") == 0


def test_limited_provider_reports_its_concurrency_identity() -> None:
    coordinator = ProviderConcurrencyCoordinator()
    limited = ConcurrencyLimitedAgentProvider(
        _RecordingProvider(), provider_name="codex", coordinator=coordinator
    )

    assert limited.provider_name == "codex"


def _execution(execution_id: str) -> WorkflowExecution:
    return WorkflowExecution(id=execution_id)


def test_dispatcher_returns_a_handle_before_the_work_completes() -> None:
    dispatcher = ExecutionDispatcher()
    release = threading.Event()
    started = threading.Event()

    def work() -> WorkflowExecution:
        started.set()
        _await(release)
        return _execution("exec-1")

    try:
        handle = dispatcher.submit("exec-1", work)
        _await(started)

        assert handle.execution_id == "exec-1"
        assert handle.done() is False

        release.set()
        assert handle.wait(TIMEOUT) is True
        assert handle.done() is True
        assert handle.result(TIMEOUT).id == "exec-1"
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_bounds_workers_and_keeps_excess_work_queued() -> None:
    dispatcher = ExecutionDispatcher(max_workers=1)
    release = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def blocking() -> WorkflowExecution:
        first_started.set()
        _await(release)
        return _execution("exec-1")

    def queued() -> WorkflowExecution:
        second_started.set()
        return _execution("exec-2")

    try:
        assert dispatcher.max_workers == 1
        first = dispatcher.submit("exec-1", blocking)
        _await(first_started)
        second = dispatcher.submit("exec-2", queued)

        # The bound is real: the second job has not started at all.
        assert dispatcher.queued_count() == 1
        assert not second_started.is_set()
        assert second.done() is False

        release.set()
        assert first.result(TIMEOUT).id == "exec-1"
        assert second.result(TIMEOUT).id == "exec-2"
        assert dispatcher.queued_count() == 0
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_runs_submissions_concurrently_up_to_its_bound() -> None:
    dispatcher = ExecutionDispatcher(max_workers=2)
    overlap = threading.Barrier(2, timeout=TIMEOUT)

    def work(execution_id: str) -> Callable[[], WorkflowExecution]:
        def run() -> WorkflowExecution:
            overlap.wait()
            return _execution(execution_id)

        return run

    try:
        first = dispatcher.submit("exec-1", work("exec-1"))
        second = dispatcher.submit("exec-2", work("exec-2"))

        # The barrier only clears if both jobs really run at the same time.
        assert first.result(TIMEOUT).id == "exec-1"
        assert second.result(TIMEOUT).id == "exec-2"
    finally:
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_surfaces_work_failures_through_the_handle() -> None:
    dispatcher = ExecutionDispatcher()

    def failing() -> WorkflowExecution:
        raise RuntimeError("workflow exploded")

    try:
        handle = dispatcher.submit("exec-1", failing)
        with pytest.raises(RuntimeError, match="workflow exploded"):
            handle.result(TIMEOUT)
        assert handle.done() is True
    finally:
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_cancels_queued_work_through_its_drop_hook() -> None:
    dispatcher = ExecutionDispatcher(max_workers=1)
    release = threading.Event()
    first_started = threading.Event()
    ran = threading.Event()

    def blocking() -> WorkflowExecution:
        first_started.set()
        _await(release)
        return _execution("exec-1")

    def never_runs() -> WorkflowExecution:
        ran.set()
        return _execution("exec-2")

    try:
        first = dispatcher.submit("exec-1", blocking)
        _await(first_started)
        second = dispatcher.submit(
            "exec-2", never_runs, on_dropped=lambda: _execution("exec-2-cancelled")
        )

        assert dispatcher.cancel("exec-2") is True
        assert second.result(TIMEOUT).id == "exec-2-cancelled"
        assert not ran.is_set()

        release.set()
        assert first.result(TIMEOUT).id == "exec-1"
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_cancelling_queued_work_without_a_drop_hook_fails_the_handle() -> None:
    dispatcher = ExecutionDispatcher(max_workers=1)
    release = threading.Event()
    first_started = threading.Event()

    def blocking() -> WorkflowExecution:
        first_started.set()
        _await(release)
        return _execution("exec-1")

    try:
        dispatcher.submit("exec-1", blocking)
        _await(first_started)
        second = dispatcher.submit("exec-2", lambda: _execution("exec-2"))

        assert dispatcher.cancel("exec-2") is True
        with pytest.raises(RuntimeError, match="cancelled before it started"):
            second.result(TIMEOUT)
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_cannot_cancel_running_or_unknown_work() -> None:
    dispatcher = ExecutionDispatcher(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    def blocking() -> WorkflowExecution:
        started.set()
        _await(release)
        return _execution("exec-1")

    try:
        handle = dispatcher.submit("exec-1", blocking)
        _await(started)

        # Already running: cooperative cancellation owns this case.
        assert dispatcher.cancel("exec-1") is False
        assert dispatcher.cancel("missing") is False

        release.set()
        assert handle.result(TIMEOUT).id == "exec-1"
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)


def test_dispatcher_rejects_submissions_after_shutdown() -> None:
    dispatcher = ExecutionDispatcher()
    dispatcher.shutdown(timeout=TIMEOUT)

    with pytest.raises(RuntimeError, match="shut down"):
        dispatcher.submit("exec-1", lambda: _execution("exec-1"))


def test_dispatcher_requires_at_least_one_worker() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        ExecutionDispatcher(max_workers=0)


def test_handle_result_times_out_without_blocking_forever() -> None:
    dispatcher = ExecutionDispatcher()
    release = threading.Event()
    started = threading.Event()

    def blocking() -> WorkflowExecution:
        started.set()
        _await(release)
        return _execution("exec-1")

    try:
        handle = dispatcher.submit("exec-1", blocking)
        _await(started)

        assert handle.wait(0.0) is False
        with pytest.raises(TimeoutError):
            handle.result(0.0)
    finally:
        release.set()
        dispatcher.shutdown(timeout=TIMEOUT)
