"""Deterministic synchronization for Textual TUI tests.

``Pilot.pause`` only drains Textual's message queue.  Screen actions in this
application also start thread workers, whose callbacks are posted back to that
queue after the worker has completed.  These helpers wait for the current
screen's workers and then drain those callbacks before inspecting UI state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.worker import Worker, WorkerCancelled

DEFAULT_TIMEOUT = 2.0


def _consume_late_result(task: asyncio.Future[list[Any]]) -> None:
    """Retrieve a timed-out wait task's result without raising from its callback."""

    try:
        task.result()
    except BaseException:
        pass


def _screen_workers(pilot: Pilot[Any]) -> list[Worker[Any]]:
    """Return workers owned by the screen currently under test.

    Textual keeps one manager at application level.  Filtering by the worker's
    node avoids coupling a screen test to unrelated work owned by another
    screen or the app itself.
    """

    screen = pilot.app.screen
    return [worker for worker in pilot.app.workers if worker.node is screen]


async def _wait_for_workers(
    workers: Iterable[Worker[Any]], *, timeout: float, description: str
) -> None:
    """Wait for workers without hiding failures or cancelling them on timeout."""

    wait_task = asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
    try:
        results = await asyncio.wait_for(asyncio.shield(wait_task), timeout)
    except TimeoutError as exc:
        # ``shield`` leaves the UI worker running. Retrieve its result later so
        # asyncio does not report an unobserved exception while the test fails.
        wait_task.add_done_callback(_consume_late_result)
        raise AssertionError(f"Timed out waiting for {description} workers") from exc

    for result in results:
        # Exclusive Textual workers are intentionally superseded by a newer
        # request. All other worker failures are surfaced to the test.
        if isinstance(result, WorkerCancelled):
            continue
        if isinstance(result, BaseException):
            raise result


async def settle_tui(
    pilot: Pilot[Any], *, timeout: float = DEFAULT_TIMEOUT, description: str = "current screen"
) -> None:
    """Wait for a current screen's mount, workers, and queued callbacks to settle."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        # This drains mount, call_later, and layout messages. A second drain
        # after observing no worker closes the window where mount work queues a
        # worker after the first idle pass.
        await pilot.pause()
        workers = _screen_workers(pilot)
        if workers:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for {description} to settle")
            await _wait_for_workers(workers, timeout=remaining, description=description)
            continue

        await pilot.pause()
        if not _screen_workers(pilot):
            return
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description} to settle")


async def wait_for_tui(
    pilot: Pilot[Any],
    predicate: Callable[[], bool],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    description: str = "expected TUI state",
) -> None:
    """Wait until a concrete rendered, mounted, or selected state is true."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"Timed out waiting for {description}")
        await settle_tui(pilot, timeout=remaining, description=description)
        try:
            matched = predicate()
        except NoMatches:
            # Query-based predicates are useful for waiting on a freshly-pushed
            # screen. Treat an as-yet unmounted target as an unmet condition.
            matched = False
        if matched:
            return
        # Yield without adding a wall-clock delay, allowing a just-posted UI
        # callback to start its next message-pump turn.
        await asyncio.sleep(0)
