"""Process-scoped submission seam for workflow executions.

Every workflow execution — fresh runs, human-response resumes, and manual
continuations alike — is submitted here and returns an :class:`ExecutionHandle`
before it completes. Synchronous commands simply wait on the handle, while
batches and future scheduling features submit through the same seam instead
of adding their own concurrency handling.

Queueing is in-memory and single-process by design. The execution row for
queued work is already persisted as ``PENDING``, but automatic recovery of
queued work after a restart is deliberately out of scope here.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from orchestrator.models.workflow_execution import WorkflowExecution

ExecutionWork = Callable[[], WorkflowExecution]


class _Outcome:
    """A minimal, reusable completion slot for one submitted execution."""

    def __init__(self) -> None:
        self._finished = threading.Event()
        self._value: WorkflowExecution | None = None
        self._error: BaseException | None = None

    def set(self, value: WorkflowExecution) -> None:
        self._value = value
        self._finished.set()

    def fail(self, error: BaseException) -> None:
        self._error = error
        self._finished.set()

    def done(self) -> bool:
        return self._finished.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def get(self, timeout: float | None = None) -> WorkflowExecution:
        if not self._finished.wait(timeout):
            raise TimeoutError("The submitted execution did not finish in time")
        if self._error is not None:
            raise self._error
        if self._value is None:  # pragma: no cover - defensive
            raise RuntimeError("The submitted execution produced no result")
        return self._value


class ExecutionHandle:
    """A persisted execution ID plus polling/waiting access to its result."""

    def __init__(self, execution_id: str, outcome: _Outcome) -> None:
        self._execution_id = execution_id
        self._outcome = outcome

    @property
    def execution_id(self) -> str:
        """The persisted execution ID, available before the work completes."""

        return self._execution_id

    def done(self) -> bool:
        """Whether the execution finished; never blocks."""

        return self._outcome.done()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the execution finishes, returning whether it did."""

        return self._outcome.wait(timeout)

    def result(self, timeout: float | None = None) -> WorkflowExecution:
        """Block and return the finished execution, re-raising its failure."""

        return self._outcome.get(timeout)


@dataclass(eq=False, slots=True)
class _Job:
    execution_id: str
    run: ExecutionWork
    on_dropped: ExecutionWork | None
    ready: Callable[[], bool] | None = None
    outcome: _Outcome = field(default_factory=_Outcome)


class ExecutionDispatcher:
    """Run a bounded number of submitted executions on in-process workers.

    The bound only sizes the worker pool; the user-facing limits remain the
    per-provider ceilings. Workers are created lazily so a process that never
    submits work never starts a thread.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        thread_name_prefix: str = "orchestrator-execution",
    ) -> None:
        if max_workers < 1:
            raise ValueError("Execution dispatcher needs at least one worker")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._condition = threading.Condition()
        self._queue: deque[_Job] = deque()
        self._jobs: dict[str, _Job] = {}
        self._workers: list[threading.Thread] = []
        self._busy = 0
        self._shutdown = False

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def queued_count(self) -> int:
        with self._condition:
            return len(self._queue)

    def submit(
        self,
        execution_id: str,
        run: ExecutionWork,
        *,
        on_dropped: ExecutionWork | None = None,
        ready: Callable[[], bool] | None = None,
    ) -> ExecutionHandle:
        """Queue already-persisted work and return its handle immediately."""

        job = _Job(execution_id=execution_id, run=run, on_dropped=on_dropped, ready=ready)
        with self._condition:
            if self._shutdown:
                raise RuntimeError("The execution dispatcher is shut down")
            self._queue.append(job)
            self._jobs[execution_id] = job
            self._start_worker_if_needed()
            self._condition.notify()
        return ExecutionHandle(execution_id, job.outcome)

    def reevaluate(self) -> None:
        """Wake workers after admission predicates may have changed."""

        with self._condition:
            self._condition.notify_all()

    def cancel(self, execution_id: str) -> bool:
        """Remove still-queued work so it never starts a stage.

        Returns ``False`` when the execution is already running; cooperative
        cancellation at stage and provider boundaries handles that case.
        """

        with self._condition:
            job = self._jobs.get(execution_id)
            if job is None:
                return False
            try:
                self._queue.remove(job)
            except ValueError:
                return False
            self._jobs.pop(execution_id, None)
        if job.on_dropped is None:
            job.outcome.fail(
                RuntimeError(f"Queued execution {execution_id} was cancelled before it started")
            )
            return True
        # Outside the lock: finishing the execution persists a terminal status
        # and notifies observers.
        try:
            job.outcome.set(job.on_dropped())
        except BaseException as exc:  # noqa: BLE001 - surfaced through the handle
            job.outcome.fail(exc)
        return True

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Stop accepting work and let idle workers exit."""

        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
            workers = list(self._workers)
        if not wait:
            return
        for worker in workers:
            worker.join(timeout)

    def _start_worker_if_needed(self) -> None:
        available = len(self._workers) - self._busy
        if available >= len(self._queue) or len(self._workers) >= self._max_workers:
            return
        worker = threading.Thread(
            target=self._work,
            name=f"{self._thread_name_prefix}-{len(self._workers) + 1}",
            daemon=True,
        )
        self._workers.append(worker)
        worker.start()

    def _work(self) -> None:
        while True:
            with self._condition:
                while True:
                    if not self._queue and self._shutdown:
                        return
                    job = next(
                        (candidate for candidate in self._queue
                         if candidate.ready is None or candidate.ready()),
                        None,
                    )
                    if job is not None:
                        self._queue.remove(job)
                        break
                    if self._shutdown:
                        return
                    self._condition.wait()
                self._busy += 1
            try:
                job.outcome.set(job.run())
            except BaseException as exc:  # noqa: BLE001 - surfaced through the handle
                job.outcome.fail(exc)
            finally:
                with self._condition:
                    self._busy -= 1
                    if self._jobs.get(job.execution_id) is job:
                        self._jobs.pop(job.execution_id, None)
