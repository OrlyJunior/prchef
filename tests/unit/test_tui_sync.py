from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.worker import WorkerCancelled

from tests.unit.tui_sync import _screen_workers, _wait_for_workers, wait_for_tui


class _Worker:
    """Small worker double for synchronization failure-path coverage."""

    def __init__(
        self,
        node: object,
        *,
        group: str = "default",
        result: Any = None,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self.node = node
        self.group = group
        self.is_cancelled = False
        self._result = result
        self._error = error
        self._release = asyncio.Event()
        if not block:
            self._release.set()
        self.cancel_calls = 0

    async def wait(self) -> Any:
        if self.is_cancelled:
            raise WorkerCancelled("worker was cancelled")
        await self._release.wait()
        if self.is_cancelled:
            raise WorkerCancelled("worker was cancelled")
        if self._error is not None:
            raise self._error
        return self._result

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.is_cancelled = True
        self._release.set()


def test_wait_for_tui_bounds_a_blocked_message_pump() -> None:
    class Pilot:
        async def pause(self) -> None:
            await asyncio.Event().wait()

    async def drive() -> None:
        with pytest.raises(AssertionError, match="Timed out waiting for blocked UI"):
            await wait_for_tui(
                Pilot(), lambda: False, timeout=0.01, description="blocked UI"  # type: ignore[arg-type]
            )

    asyncio.run(drive())


def test_wait_for_tui_bounds_an_unmet_predicate() -> None:
    class Pilot:
        app = SimpleNamespace(screen=object(), workers=[])

        async def pause(self) -> None:
            pass

    async def drive() -> None:
        with pytest.raises(AssertionError, match="Timed out waiting for missing state"):
            await wait_for_tui(
                Pilot(), lambda: False, timeout=0.01, description="missing state"  # type: ignore[arg-type]
            )

    asyncio.run(drive())


def test_wait_for_tui_tolerates_a_temporarily_missing_widget() -> None:
    class Pilot:
        app = SimpleNamespace(screen=object(), workers=[])

        async def pause(self) -> None:
            pass

    attempts = 0

    def mounted() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NoMatches("not mounted yet")
        return True

    async def drive() -> None:
        await wait_for_tui(Pilot(), mounted, description="widget mount")  # type: ignore[arg-type]
        assert attempts == 2

    asyncio.run(drive())


def test_screen_worker_scope_can_include_descendants() -> None:
    screen = SimpleNamespace(parent=None)
    child = SimpleNamespace(parent=screen)
    other = SimpleNamespace(parent=None)
    owned = _Worker(screen)
    descendant = _Worker(child)
    unrelated = _Worker(other)
    pilot = SimpleNamespace(app=SimpleNamespace(
        screen=screen, workers=[owned, descendant, unrelated]
    ))
    assert _screen_workers(pilot) == [owned]
    assert _screen_workers(pilot, descendants=True) == [owned, descendant]


def test_wait_for_tui_waits_for_a_descendant_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    class WorkerApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Widget(id="worker-owner")

    async def drive() -> None:
        application = WorkerApp()
        async with application.run_test() as pilot:
            child = application.query_one("#worker-owner", Widget)
            release = asyncio.Event()
            joined = asyncio.Event()
            unrelated_release = asyncio.Event()
            descendant = child.run_worker(release.wait())
            unrelated = application.run_worker(unrelated_release.wait())
            original_wait = descendant.wait

            async def observe_wait() -> Any:
                joined.set()
                return await original_wait()

            monkeypatch.setattr(descendant, "wait", observe_wait)
            waiting = asyncio.create_task(wait_for_tui(
                pilot, lambda: True, descendants=True, description="descendant layout"
            ))
            try:
                assert descendant.node is child and child.parent is application.screen
                # Even a true predicate must join the real child-owned worker.
                await asyncio.wait_for(joined.wait(), timeout=10)
                assert not waiting.done()
                release.set()
                await asyncio.wait_for(waiting, timeout=10)
                assert descendant.is_finished
                assert not unrelated.is_finished
            finally:
                waiting.cancel()
                descendant.cancel()
                unrelated.cancel()
                await asyncio.gather(
                    waiting, descendant.wait(), unrelated.wait(), return_exceptions=True
                )

    asyncio.run(drive())


@pytest.mark.parametrize("error", [RuntimeError("descendant failed"), WorkerCancelled()])
def test_wait_for_tui_propagates_descendant_worker_errors(error: BaseException) -> None:
    class Pilot:
        async def pause(self) -> None:
            pass

    async def drive() -> None:
        screen = SimpleNamespace(parent=None)
        descendant = _Worker(SimpleNamespace(parent=screen), error=error)
        pilot = Pilot()
        pilot.app = SimpleNamespace(screen=screen, workers=[descendant])
        with pytest.raises(type(error)) as raised:
            await wait_for_tui(pilot, lambda: True, descendants=True)  # type: ignore[arg-type]
        assert raised.value is error

    asyncio.run(drive())


def test_wait_for_workers_propagates_worker_failure() -> None:
    async def drive() -> None:
        with pytest.raises(RuntimeError, match="index failed"):
            await _wait_for_workers(
                [_Worker(object(), error=RuntimeError("index failed"))],  # type: ignore[arg-type]
                timeout=0.1,
                description="index status",
            )

    asyncio.run(drive())


def test_wait_for_workers_propagates_unexpected_cancellation() -> None:
    async def drive() -> None:
        cancelled = _Worker(object())
        cancelled.cancel()
        with pytest.raises(WorkerCancelled):
            await _wait_for_workers(
                [cancelled],  # type: ignore[arg-type]
                timeout=0.1,
                description="task load",
            )

    asyncio.run(drive())


def test_wait_for_workers_allows_only_a_superseded_cancellation() -> None:
    async def drive() -> None:
        node = object()
        superseded = _Worker(node)
        superseded.cancel()
        replacement = _Worker(node)
        await _wait_for_workers(
            [superseded, replacement],  # type: ignore[arg-type]
            timeout=0.1,
            description="task reload",
        )

    asyncio.run(drive())


def test_wait_for_workers_allows_a_later_scheduled_supersession() -> None:
    async def drive() -> None:
        node = object()
        superseded = _Worker(node)
        superseded.cancel()
        live_workers = [superseded]
        replacement = _Worker(node)

        async def schedule_replacement() -> None:
            await asyncio.sleep(0)
            live_workers.append(replacement)

        replacement_task = asyncio.create_task(schedule_replacement())
        await _wait_for_workers(
            [superseded],  # type: ignore[arg-type]
            timeout=0.1,
            description="task reload",
            refresh_workers=lambda: live_workers,  # type: ignore[return-value]
        )
        await replacement_task
        assert live_workers == [superseded, replacement]

    asyncio.run(drive())


def test_wait_for_workers_cancels_and_reaps_timed_out_workers() -> None:
    async def drive() -> None:
        worker = _Worker(object(), block=True)
        with pytest.raises(AssertionError, match="Timed out waiting for slow workers"):
            await _wait_for_workers(
                [worker],  # type: ignore[arg-type]
                timeout=0.01,
                description="slow",
            )
        assert worker.cancel_calls == 1
        assert worker.is_cancelled

    asyncio.run(drive())
