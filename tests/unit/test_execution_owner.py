from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from unittest.mock import Mock, call

import pytest

from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.services.execution_owner import ExecutionOwnerHeartbeat


class HeartbeatTicks:
    """Advance the worker's scheduler explicitly, without any wall-clock delay."""

    def __init__(self) -> None:
        self.intervals: Queue[float] = Queue()
        self.decisions: Queue[bool] = Queue()
        self.stopped = Event()

    def wait(self, interval: float) -> bool:
        self.intervals.put(interval)
        return self.decisions.get(timeout=5)

    def set(self) -> None:
        self.stopped.set()
        self.decisions.put(True)


def test_registration_precedes_admission_and_refresh_is_independent() -> None:
    repository = Mock(spec=WorkflowRepository)
    service = ExecutionOwnerHeartbeat(repository)
    ticks = HeartbeatTicks()
    service._stop = ticks  # type: ignore[assignment]
    try:
        service.start()
        assert ticks.intervals.get(timeout=5) == 10
        repository.create_execution("blocked-work")
        assert repository.mock_calls[:2] == [
            call.register_owner(service.owner_id), call.create_execution("blocked-work"),
        ]
        # No UI timer, stage event or dispatcher action is necessary to refresh.
        for count in (1, 2):
            ticks.decisions.put(False)
            assert ticks.intervals.get(timeout=5) == 10
            assert repository.refresh_owner.call_count == count
        service.start()
        repository.register_owner.assert_called_once()
    finally:
        service.close()
    repository.interrupt_owned_and_end.assert_called_once()


def test_failed_initial_registration_prevents_worker_start() -> None:
    repository = Mock(spec=WorkflowRepository)
    repository.register_owner.side_effect = RuntimeError("database unavailable")
    service = ExecutionOwnerHeartbeat(repository)
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.start()
    assert service._thread is None
    service.close()
    repository.refresh_owner.assert_not_called()
    repository.interrupt_owned_and_end.assert_not_called()


def test_failed_heartbeat_logs_and_retries(caplog: pytest.LogCaptureFixture) -> None:
    repository = Mock(spec=WorkflowRepository)
    repository.refresh_owner.side_effect = [RuntimeError("write failed"), None]
    service = ExecutionOwnerHeartbeat(repository)
    ticks = HeartbeatTicks()
    service._stop = ticks  # type: ignore[assignment]
    try:
        service.start()
        assert ticks.intervals.get(timeout=5) == 10
        for _ in range(2):
            ticks.decisions.put(False)
            assert ticks.intervals.get(timeout=5) == 10
        assert repository.refresh_owner.call_count == 2
        assert "Could not persist execution owner heartbeat; will retry" in caplog.text
    finally:
        service.close()


def test_shutdown_joins_inflight_refresh_before_ending_owner_and_is_idempotent() -> None:
    repository = Mock(spec=WorkflowRepository)
    entered = Event()
    release = Event()

    def refresh(_owner_id: str) -> None:
        entered.set()
        assert release.wait(5)

    repository.refresh_owner.side_effect = refresh
    service = ExecutionOwnerHeartbeat(repository)
    ticks = HeartbeatTicks()
    service._stop = ticks  # type: ignore[assignment]
    service.start()
    try:
        assert ticks.intervals.get(timeout=5) == 10
        ticks.decisions.put(False)
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(service.close)
            assert ticks.stopped.wait(5)
            repository.interrupt_owned_and_end.assert_not_called()
            release.set()
            closing.result(timeout=5)
        service.close()
        repository.refresh_owner.assert_called_once_with(service.owner_id)
        repository.interrupt_owned_and_end.assert_called_once()
        assert repository.mock_calls[-1] == call.interrupt_owned_and_end()
    finally:
        release.set()
        service.close()
