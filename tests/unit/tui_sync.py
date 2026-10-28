"""Deterministic synchronization for Textual TUI tests.

``Pilot.pause`` only drains Textual's message queue.  Screen actions in this
application also start thread workers, whose callbacks are posted back to that
queue after the worker has completed.  These helpers wait for the current
screen's workers and then drain those callbacks before inspecting UI state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.worker import Worker, WorkerCancelled

# This bounds state-based waits, rather than measuring UI performance. Shared
# CI runners can spend several seconds dispatching mount/layout and worker callbacks.
DEFAULT_TIMEOUT = 10.0


def _screen_workers(pilot: Pilot[Any], *, descendants: bool = False) -> list[Worker[Any]]:
    """Return workers owned by the screen currently under test.

    Textual keeps one manager at application level.  Filtering by the worker's
    node avoids coupling a screen test to unrelated work owned by another
    screen or the app itself.
    """

    screen = pilot.app.screen

    def belongs(worker: Worker[Any]) -> bool:
        node = worker.node
        while node is not None:
            if node is screen:
                return True
            if not descendants:
                return False
            node = node.parent
        return False

    return [worker for worker in pilot.app.workers if belongs(worker)]


def _was_superseded(
    worker: Worker[Any],
    workers: Sequence[Worker[Any]],
    refreshed_workers: Sequence[Worker[Any]] = (),
) -> bool:
    """Return whether a newer worker in the same exclusive group replaced ``worker``.

    Textual does not retain the ``exclusive`` flag on a ``Worker``. Its worker
    manager does, however, expose workers in creation order, and exclusive
    work cancels older workers with the same node and group before adding the
    replacement. Workers first observed in a refreshed manager snapshot are
    necessarily newer than the original snapshot, so append them after that
    original creation order. That lets the test helper accept only this
    specific expected cancellation, rather than treating every
    ``WorkerCancelled`` as settled.
    """

    observed_workers = [*workers]
    observed_workers.extend(
        refreshed_worker
        for refreshed_worker in refreshed_workers
        if not any(refreshed_worker is observed for observed in observed_workers)
    )
    try:
        worker_index = next(
            index
            for index, observed_worker in enumerate(observed_workers)
            if observed_worker is worker
        )
    except StopIteration:
        return False
    return any(
        replacement.node is worker.node
        and replacement.group == worker.group
        and not replacement.is_cancelled
        for replacement in observed_workers[worker_index + 1 :]
    )


async def _cancel_workers(workers: Iterable[Worker[Any]]) -> None:
    """Cancel timed-out workers and reap their Textual tasks.

    Thread workers cannot be forcibly stopped by Python, but ``cancel`` sets
    Textual's cooperative cancellation event and stops their Textual task.
    Reaping that task avoids leaving cancelled tasks or their exceptions behind
    after a failed test. Application worker functions must still honour the
    cancellation event before posting a late callback from long-running work.
    """

    workers = tuple(workers)
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)


async def _wait_for_workers(
    workers: Iterable[Worker[Any]],
    *,
    timeout: float,
    description: str,
    refresh_workers: Callable[[], Iterable[Worker[Any]]] | None = None,
) -> None:
    """Wait for workers, surfacing failures and cleaning up a timeout.

    ``refresh_workers`` supplies a live worker snapshot after one zero-time
    scheduling yield. Textual can cancel an exclusive worker before scheduling
    its replacement, so that replacement may not have existed in ``workers``.
    """

    workers = tuple(workers)
    deadline = asyncio.get_running_loop().time() + timeout
    wait_task = asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
    try:
        results = await asyncio.wait_for(wait_task, timeout)
    except TimeoutError as exc:
        await _cancel_workers(workers)
        raise AssertionError(f"Timed out waiting for {description} workers") from exc

    refreshed_workers: Sequence[Worker[Any]] = ()
    if refresh_workers is not None and asyncio.get_running_loop().time() < deadline:
        # Give Textual one event-loop turn to register an exclusive worker's
        # replacement, without adding a wall-clock delay or extending timeout.
        await asyncio.sleep(0)
        refreshed_workers = tuple(refresh_workers())

    for worker, result in zip(workers, results, strict=True):
        # Exclusive Textual workers are intentionally superseded by a newer
        # request. All other cancellations and failures are surfaced to the
        # test, because their callbacks may not have updated the asserted UI.
        if isinstance(result, WorkerCancelled) and _was_superseded(
            worker, workers, refreshed_workers
        ):
            continue
        if isinstance(result, BaseException):
            raise result


async def settle_tui(
    pilot: Pilot[Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    description: str = "current screen",
    descendants: bool = False,
) -> None:
    """Wait for a current screen's mount, workers, and queued callbacks to settle."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        # This drains mount, call_later, and layout messages. A second drain
        # after observing no worker closes the window where mount work queues a
        # worker after the first idle pass.
        await pilot.pause()
        workers = _screen_workers(pilot, descendants=descendants)
        if workers:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for {description} to settle")
            await _wait_for_workers(
                workers,
                timeout=remaining,
                description=description,
                refresh_workers=lambda: _screen_workers(pilot, descendants=descendants),
            )
            continue

        await pilot.pause()
        if not _screen_workers(pilot, descendants=descendants):
            return
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description} to settle")


async def wait_for_tui(
    pilot: Pilot[Any],
    predicate: Callable[[], bool],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    description: str = "expected TUI state",
    descendants: bool = False,
) -> None:
    """Wait until a concrete rendered, mounted, or selected state is true."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        # The deadline shares the remaining budget with worker waits; this
        # outer bound also interrupts an unresponsive Pilot.pause().
        async with asyncio.timeout(timeout):
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise AssertionError(f"Timed out waiting for {description}")
                await settle_tui(
                    pilot, timeout=remaining, description=description, descendants=descendants
                )
                try:
                    matched = predicate()
                except NoMatches:
                    matched = False
                if matched:
                    return
                await asyncio.sleep(0)
    except TimeoutError as exc:
        raise AssertionError(f"Timed out waiting for {description}") from exc
