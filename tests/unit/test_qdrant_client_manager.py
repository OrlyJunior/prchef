"""Lifecycle of the process-owned Qdrant client manager.

Concurrent in-process executions cannot each open the embedded local store,
which takes an exclusive lock on its directory. One lazily created, long-lived
client per configured connection is reused instead and closed only at shutdown.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from orchestrator.config.project_config import QdrantSettings
from orchestrator.services.qdrant_repository_context import (
    QdrantClientManager,
    qdrant_connection_key,
)

TIMEOUT = 10.0


class _Client:
    def __init__(self, settings: QdrantSettings) -> None:
        self.settings = settings
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Factory:
    def __init__(self) -> None:
        self.clients: list[_Client] = []

    def __call__(self, settings: QdrantSettings) -> _Client:
        client = _Client(settings)
        self.clients.append(client)
        return client


def _local(path: Path) -> QdrantSettings:
    return QdrantSettings(mode="local", path=path)


def test_repeated_operations_reuse_one_client_per_store(tmp_path: Path) -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)
    settings = _local(tmp_path / "index")

    with manager.session(settings) as first, manager.session(settings) as nested:
        assert first is nested

    with manager.session(settings) as later:
        assert later is first

    # The client is created lazily, exactly once, and not closed between uses.
    assert len(factory.clients) == 1
    assert factory.clients[0].close_calls == 0
    assert manager.open_connections() == 1


def test_a_manager_that_is_never_used_opens_no_client() -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)

    assert factory.clients == []
    assert manager.open_connections() == 0


def test_different_stores_keep_distinct_clients(tmp_path: Path) -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)

    with manager.session(_local(tmp_path / "one")) as first:
        pass
    with manager.session(_local(tmp_path / "two")) as second:
        pass
    with manager.session(QdrantSettings(mode="server", url="http://localhost:6333")) as remote:
        pass

    assert first is not second
    assert remote is not first
    assert manager.open_connections() == 3


def test_equivalent_local_paths_share_one_client(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    index = tmp_path / "index"
    index.mkdir()
    (tmp_path / "other").mkdir()
    indirect = tmp_path / "other" / ".." / "index"

    with manager.session(_local(index)) as direct:
        pass
    with manager.session(_local(indirect)) as equivalent:
        pass

    assert direct is equivalent
    assert manager.open_connections() == 1


def test_connection_key_is_canonical_for_the_same_local_store(tmp_path: Path) -> None:
    index = tmp_path / "index"
    index.mkdir()
    (tmp_path / "other").mkdir()

    assert qdrant_connection_key(_local(index)) == qdrant_connection_key(
        _local(tmp_path / "other" / ".." / "index")
    )
    assert qdrant_connection_key(_local(index)) != qdrant_connection_key(
        _local(tmp_path / "other")
    )


def test_local_operations_are_serialized_through_one_lock(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    settings = _local(tmp_path / "index")
    holder_inside = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with manager.session(settings):
            holder_inside.set()
            assert release_holder.wait(TIMEOUT)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert holder_inside.wait(TIMEOUT)

    # Inspecting the store's lock from another thread proves exclusion without
    # depending on how long a competing operation happens to be blocked.
    lock = manager._operation_locks[qdrant_connection_key(settings)]
    assert lock.acquire(blocking=False) is False

    release_holder.set()
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_queued_local_operations_all_complete(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    settings = _local(tmp_path / "index")
    entries: list[int] = []
    entries_lock = threading.Lock()
    finished = threading.Semaphore(0)

    def use(index: int) -> None:
        with manager.session(settings):
            with entries_lock:
                entries.append(index)
        finished.release()

    threads = [threading.Thread(target=use, args=(index,), daemon=True) for index in range(5)]
    for thread in threads:
        thread.start()
    for _ in threads:
        assert finished.acquire(timeout=TIMEOUT)
    for thread in threads:
        thread.join(TIMEOUT)
        assert not thread.is_alive()

    assert sorted(entries) == [0, 1, 2, 3, 4]


def test_server_operations_are_not_serialized() -> None:
    manager = QdrantClientManager(_Factory())
    settings = QdrantSettings(mode="server", url="http://localhost:6333")
    # A barrier only clears if both sessions are held at the same time.
    overlap = threading.Barrier(2, timeout=TIMEOUT)

    def use() -> None:
        with manager.session(settings):
            overlap.wait()

    threads = [threading.Thread(target=use, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
        assert not thread.is_alive()


def test_an_operation_failure_does_not_close_the_shared_client(tmp_path: Path) -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)
    settings = _local(tmp_path / "index")

    with pytest.raises(RuntimeError, match="index operation failed"):
        with manager.session(settings):
            raise RuntimeError("index operation failed")

    assert factory.clients[0].close_calls == 0
    # The same client remains usable for other executions.
    with manager.session(settings) as client:
        assert client is factory.clients[0]
    assert len(factory.clients) == 1


def test_shutdown_closes_each_client_exactly_once(tmp_path: Path) -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)
    with manager.session(_local(tmp_path / "one")):
        pass
    with manager.session(_local(tmp_path / "two")):
        pass

    manager.close()
    manager.close()

    assert [client.close_calls for client in factory.clients] == [1, 1]
    assert manager.open_connections() == 0


def test_sessions_are_refused_after_shutdown(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    manager.close()

    with pytest.raises(RuntimeError, match="already closed"):
        with manager.session(_local(tmp_path / "index")):
            pass


def test_try_admission_skips_before_client_creation(tmp_path: Path) -> None:
    factory = _Factory()
    manager = QdrantClientManager(factory)
    settings = _local(tmp_path / "index")
    inside, release = threading.Event(), threading.Event()

    def workflow() -> None:
        with manager.operation(settings):
            inside.set()
            assert release.wait(TIMEOUT)

    thread = threading.Thread(target=workflow, daemon=True)
    thread.start()
    assert inside.wait(TIMEOUT)
    try:
        with manager.operation(_local(tmp_path / "other" / ".." / "index"),
                               blocking=False) as admitted:
            assert not admitted
        assert not factory.clients
        with manager.operation(_local(tmp_path / "different"), blocking=False) as admitted:
            assert admitted
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()


def test_workflow_waits_for_admitted_refresh_and_reenters(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    settings = _local(tmp_path / "index")
    attempted, entered = threading.Event(), threading.Event()
    with manager.operation(settings):
        def workflow() -> None:
            attempted.set()
            with manager.operation(settings):
                with manager.session(settings):
                    entered.set()

        thread = threading.Thread(target=workflow, daemon=True)
        thread.start()
        assert attempted.wait(TIMEOUT)
        # The holding thread still owns the exact lock the workflow must enter.
        assert not entered.is_set()
    assert entered.wait(TIMEOUT)
    thread.join(TIMEOUT)
    assert not thread.is_alive()


def test_slow_creation_does_not_block_try_admission_or_other_stores(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()
    settings = _local(tmp_path / "one")

    def factory(config: QdrantSettings) -> _Client:
        if config == settings:
            started.set()
            assert release.wait(TIMEOUT)
        return _Client(config)

    manager = QdrantClientManager(factory)

    def create() -> None:
        with manager.session(settings):
            pass

    thread = threading.Thread(target=create, daemon=True)
    thread.start()
    assert started.wait(TIMEOUT)
    try:
        with manager.operation(settings, blocking=False) as admitted:
            assert not admitted
        with manager.session(_local(tmp_path / "two")):
            pass
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()


def test_failed_client_creation_releases_admission_and_can_retry(tmp_path: Path) -> None:
    calls = 0

    def factory(config: QdrantSettings) -> _Client:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Storage folder is already accessed by another instance")
        return _Client(config)

    manager = QdrantClientManager(factory)
    settings = _local(tmp_path / "index")
    with pytest.raises(RuntimeError, match="Storage folder"):
        with manager.session(settings):
            pass
    assert manager.open_connections() == 0
    with manager.operation(settings, blocking=False) as admitted:
        assert admitted
        with manager.session(settings):
            pass
    assert calls == 2


def test_shutdown_waits_without_closing_active_or_nested_sessions(tmp_path: Path) -> None:
    manager = QdrantClientManager(_Factory())
    settings = _local(tmp_path / "index")
    closed = threading.Event()

    def close() -> None:
        manager.close()
        closed.set()

    with manager.operation(settings):
        with manager.session(settings) as client:
            thread = threading.Thread(target=close, daemon=True)
            thread.start()
            with manager._settled:
                assert manager._settled.wait_for(lambda: manager._closing, timeout=TIMEOUT)
            assert client.close_calls == 0
            assert not closed.is_set()
            with manager.session(settings) as nested:
                assert nested is client
    assert closed.wait(TIMEOUT)
    thread.join(TIMEOUT)
    assert client.close_calls == 1


def test_complete_server_operations_coordinate_without_serializing_raw_sessions() -> None:
    manager = QdrantClientManager(_Factory())
    settings = QdrantSettings(mode="server", url="http://localhost:6333")
    held, release = threading.Event(), threading.Event()

    def workflow() -> None:
        with manager.operation(settings):
            held.set()
            assert release.wait(TIMEOUT)

    thread = threading.Thread(target=workflow, daemon=True)
    thread.start()
    assert held.wait(TIMEOUT)
    try:
        with manager.operation(settings, blocking=False) as admitted:
            assert not admitted
        with manager.session(settings):
            pass
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()
