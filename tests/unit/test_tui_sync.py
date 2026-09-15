from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual.worker import WorkerCancelled

from tests.unit.tui_sync import _wait_for_workers


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
