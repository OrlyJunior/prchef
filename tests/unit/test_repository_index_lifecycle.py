"""Index acceptance coverage using only in-memory clients and embeddings."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.config.project_config import (
    ProjectConfig,
    QdrantSettings,
    RepositoryContextSettings,
)
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_context import EmbeddingIdentity, IndexLifecycleState
from orchestrator.services import qdrant_repository_context as backend
from orchestrator.services.exceptions import WorkflowCancelledError
from orchestrator.services.qdrant_repository_context import (
    QdrantClientManager,
    QdrantRepositoryContextStrategy,
    RepositoryIndexOperationError,
)
from orchestrator.services.repository_context import RepositoryContextRequest
from orchestrator.tui.bridge import WorkflowController

TIMEOUT = 10.0


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.vectors: dict[str, Any] = {}
        self.close_calls = 0

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.collections[collection_name] = {}
        self.vectors[collection_name] = vectors_config

    def get_collection(self, name: str) -> Any:
        return SimpleNamespace(config=SimpleNamespace(
            params=SimpleNamespace(vectors=self.vectors[name])
        ))

    def retrieve(self, collection_name: str, ids: Any, **kwargs: Any) -> list[Any]:
        return [self.collections[collection_name][str(key)] for key in ids
                if str(key) in self.collections[collection_name]]

    def upsert(self, collection_name: str, points: Any, **kwargs: Any) -> None:
        self.collections[collection_name].update({str(point.id): point for point in points})

    def delete(self, collection_name: str, points_selector: Any, **kwargs: Any) -> None:
        for key in points_selector.points:
            self.collections[collection_name].pop(str(key), None)

    def delete_collection(self, name: str) -> None:
        del self.collections[name]

    def close(self) -> None:
        self.close_calls += 1


class FakeEmbedding:
    identity = EmbeddingIdentity(provider="fastembed", model="fake", version="1", dimension=2)

    def __init__(self) -> None:
        self.calls = 0
        self.on_embed: Any = None

    def embed_documents(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        if self.on_embed:
            self.on_embed()
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # None of these tests import qdrant_client or fastembed, or construct either.
    monkeypatch.setattr(backend, "_qdrant_models", lambda: SimpleNamespace(
        PointStruct=SimpleNamespace, VectorParams=SimpleNamespace,
        PointIdsList=SimpleNamespace, Distance=SimpleNamespace(COSINE="cosine"),
    ))
    monkeypatch.setattr(backend, "_git_head", lambda root: "checkout-head")
    monkeypatch.setattr(backend, "_git_worktree_roots", lambda root: [])
    root = tmp_path / "checkout"
    root.mkdir()
    client = FakeClient()
    embedding = FakeEmbedding()
    settings = RepositoryContextSettings(
        chunk_size_chars=256, chunk_overlap_chars=0,
        qdrant=QdrantSettings(mode="local", path=tmp_path / "store"),
    )
    manager = QdrantClientManager(lambda config: client)
    strategy = QdrantRepositoryContextStrategy(
        settings, "repo", root, embedding_provider=embedding, client_manager=manager,
    )
    return SimpleNamespace(root=root, client=client, embedding=embedding, settings=settings,
                           manager=manager, strategy=strategy)


def test_incremental_counts_cover_unchanged_deleted_empty_and_other_roots(index: Any) -> None:
    (index.root / "a.txt").write_text("first line of source\n" * 50)
    (index.root / "b.txt").write_text("second line of source\n" * 50)
    built = index.strategy.build_index()
    assert built.state is IndexLifecycleState.READY
    assert built.document_count == 2
    assert built.point_count > 2
    original_b = len(index.strategy._read_inventory(
        index.client, index.strategy._configured_path_id
    )["b.txt"])
    (index.root / "a.txt").write_text("changed\n")
    refreshed = index.strategy.refresh_changed_files()
    assert refreshed.last_indexed_commit == "checkout-head"
    assert refreshed.document_count == 2
    assert refreshed.point_count == original_b + 1
    # Additional root chunks, metadata, inventory, and content caches aren't checkout chunks.
    index.client.upsert(index.strategy.collection_name, [SimpleNamespace(
        id="other-root", vector={"dense": [1.0, 0.0]},
        payload={"kind": "chunk", "source_root_id": "linked", "path": "c.txt"},
    )])
    assert index.strategy.inspect_index() == refreshed
    calls = index.embedding.calls
    assert index.strategy.refresh_changed_files().point_count == refreshed.point_count
    assert index.embedding.calls == calls
    (index.root / "b.txt").unlink()
    assert index.strategy.refresh_changed_files().point_count == 1
    (index.root / "a.txt").unlink()
    empty = index.strategy.refresh_changed_files()
    assert empty.state is IndexLifecycleState.READY
    assert (empty.document_count, empty.point_count) == (0, 0)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("action", ["inspect_index", "refresh_changed_files", "clear_index"])
def test_external_storage_lock_is_transient_and_retryable(
    index: Any, wrapped: bool, action: str,
) -> None:
    (index.root / "a.txt").write_text("source")
    ready = index.strategy.build_index()
    metadata = dict(index.client.collections[index.strategy.collection_name]
                    [backend._METADATA_POINT_ID].payload)
    available_factory = index.manager._client_factory
    index.manager._clients.clear()

    def busy(config: Any) -> Any:
        error = RuntimeError("Storage folder /private/path is already accessed by another "
                             "instance of Qdrant client")
        if wrapped:
            raise ValueError("wrapped") from error
        raise error

    index.manager._client_factory = busy
    result = getattr(index.strategy, action)()
    assert result.state is IndexLifecycleState.BUSY
    assert "another process" in result.error_message
    assert "next screen open or manual refresh" in result.error_message
    assert "/private/path" not in result.error_message
    assert index.manager.open_connections() == 0
    assert index.client.collections[index.strategy.collection_name][
        backend._METADATA_POINT_ID].payload == metadata
    index.manager._client_factory = available_factory
    assert index.strategy.refresh_changed_files().state is IndexLifecycleState.READY
    assert index.strategy.inspect_index().last_indexed_commit == ready.last_indexed_commit


def test_unrelated_error_still_fails(index: Any) -> None:
    def failed() -> None:
        raise RuntimeError("an unrelated lock in application logic")

    index.embedding.on_embed = failed
    (index.root / "a.txt").write_text("source")
    with pytest.raises(RepositoryIndexOperationError):
        index.strategy.refresh_changed_files()
    assert index.strategy.inspect_index().state is IndexLifecycleState.FAILED


@pytest.mark.parametrize("action", ["build_index", "refresh_changed_files", "clear_index"])
def test_backend_action_failure_preserves_sanitized_diagnostic(
    index: Any, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    (index.root / "a.txt").write_text("source")
    index.strategy.build_index()
    (index.root / "a.txt").write_text("changed")

    def fail(*args: Any) -> None:
        raise RuntimeError(
            "Vector dimension mismatch; rebuild the index. api_key=private-secret "
            "at https://user:private-password@qdrant.invalid/index"
        )

    if action == "clear_index":
        monkeypatch.setattr(index.client, "delete_collection", fail)
    else:
        index.embedding.on_embed = fail
    with pytest.raises(RepositoryIndexOperationError) as caught:
        getattr(index.strategy, action)()
    for message in (str(caught.value), index.strategy.inspect_index().error_message):
        assert "Vector dimension mismatch; rebuild the index." in message
        assert "private-secret" not in message
        assert "private-password" not in message
        assert "qdrant.invalid" not in message


@pytest.mark.parametrize("change_head", [False, True])
def test_changes_during_refresh_never_publish_newer_provenance(
    index: Any, monkeypatch: pytest.MonkeyPatch, change_head: bool,
) -> None:
    (index.root / "a.txt").write_text("source")
    ready = index.strategy.build_index()
    (index.root / "a.txt").write_text("changed")

    def change() -> None:
        if change_head:
            monkeypatch.setattr(backend, "_git_head", lambda root: "new-head")
        else:
            (index.root / "a.txt").write_text("changed again externally")

    index.embedding.on_embed = change
    with pytest.raises(RepositoryIndexOperationError):
        index.strategy.refresh_changed_files()
    failed = index.strategy.inspect_index()
    assert failed.state is IndexLifecycleState.FAILED
    assert failed.last_indexed_commit == ready.last_indexed_commit
    assert failed.last_successful_indexed_at == ready.last_successful_indexed_at


def test_cancellation_releases_complete_operation(index: Any) -> None:
    def cancel() -> None:
        raise WorkflowCancelledError("cancelled")

    index.strategy._cancellation_check = cancel
    with pytest.raises(WorkflowCancelledError):
        index.strategy.build_index()
    with index.manager.operation(index.settings.qdrant, blocking=False) as admitted:
        assert admitted


def controller_for(index: Any, monkeypatch: pytest.MonkeyPatch) -> WorkflowController:
    config = ProjectConfig.model_validate({
        "path": index.root, "configured_repository_key": "repo",
        "repository_context": index.settings, "roles": {"spec": "codex", "review": "codex"},
    })
    controller = WorkflowController(
        workflow_repository=SimpleNamespace(), config_loader=SimpleNamespace(),
        client_factory=SimpleNamespace(), task_provider_factory=lambda *args: None,
        repository_index_clients=index.manager,
    )
    monkeypatch.setattr(controller, "project_config", lambda repository: config)
    monkeypatch.setattr("orchestrator.tui.bridge.resolve_repository_context_strategy",
                        lambda *args, **kwargs: index.strategy)
    return controller


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("operation", ["build_index", "refresh_changed_files", "clear_index"])
def test_controller_preserves_actionable_index_failure(
    index: Any, monkeypatch: pytest.MonkeyPatch, background: bool, operation: str,
) -> None:
    controller = controller_for(index, monkeypatch)
    message = "Vector dimension mismatch: expected 2, got 3; rebuild the index."

    def fail() -> None:
        raise RepositoryIndexOperationError(message)

    monkeypatch.setattr(index.strategy, operation, fail)
    if background:
        completed = threading.Event()
        statuses: list[Any] = []

        def listener(status: Any) -> None:
            statuses.append(status)
            if status.state is not IndexLifecycleState.INDEXING:
                completed.set()

        controller.request_repository_index("repo", listener, operation=operation)
        assert completed.wait(TIMEOUT)
        failed = statuses[-1]
    else:
        with pytest.raises(RepositoryIndexOperationError, match="Vector dimension mismatch"):
            controller._run_repository_index_operation("repo", operation)
        identity = controller._index_identity(controller.project_config("repo"))
        failed = controller._index_statuses[identity]
    assert failed.state is IndexLifecycleState.FAILED
    assert failed.error_message == message
    with controller._index_operation_lock:
        assert not controller._index_operations


def test_automatic_refresh_preserves_actionable_failure(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index.strategy.build_index()
    monkeypatch.setattr(backend, "_git_head", lambda root: "next-head")
    controller = controller_for(index, monkeypatch)
    completed = threading.Event()
    statuses: list[Any] = []

    def fail() -> None:
        raise RepositoryIndexOperationError("Embedding unavailable; check model configuration.")

    def listener(status: Any) -> None:
        statuses.append(status)
        if status.state is not IndexLifecycleState.INDEXING:
            completed.set()

    monkeypatch.setattr(index.strategy, "refresh_changed_files", fail)
    controller.request_repository_index("repo", listener)
    assert completed.wait(TIMEOUT)
    assert statuses[-1].state is IndexLifecycleState.FAILED
    assert statuses[-1].error_message == "Embedding unavailable; check model configuration."


def test_open_refresh_is_owned_deduplicated_and_status_never_inspects_active_work(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (index.root / "a.txt").write_text("source")
    index.strategy.build_index()
    monkeypatch.setattr(backend, "_git_head", lambda root: "next-head")
    (index.root / "a.txt").write_text("changed")
    controller = controller_for(index, monkeypatch)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    def embed() -> None:
        entered.set()
        assert release.wait(TIMEOUT)

    index.embedding.on_embed = embed
    first: list[Any] = []
    second: list[Any] = []

    def listen(status: Any) -> None:
        second.append(status)
        if status.state is not IndexLifecycleState.INDEXING:
            completed.set()

    controller.request_repository_index("repo", first.append)
    assert entered.wait(TIMEOUT)
    calls = index.embedding.calls
    try:
        with monkeypatch.context() as active_patch:
            active_patch.setattr(index.strategy, "inspect_index",
                                 lambda: pytest.fail("active inspect"))
            controller.request_repository_index("repo", listen)
            assert controller.repository_index_status("repo").state is IndexLifecycleState.INDEXING
            assert second[-1].state is IndexLifecycleState.INDEXING
            assert index.embedding.calls == calls
    finally:
        release.set()
    assert completed.wait(TIMEOUT)
    assert first[-1].state is IndexLifecycleState.READY
    assert second[-1] == first[-1]
    assert first[-1].last_indexed_commit == "next-head"


def test_screen_skip_does_no_discovery_and_never_queues_work(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_for(index, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def workflow() -> None:
        with index.manager.operation(index.settings.qdrant):
            entered.set()
            assert release.wait(TIMEOUT)

    thread = threading.Thread(target=workflow, daemon=True)
    thread.start()
    assert entered.wait(TIMEOUT)
    monkeypatch.setattr(backend.RepositorySourcePipeline, "discover",
                        lambda *args, **kwargs: pytest.fail("screen discovered while busy"))
    try:
        assert controller.refresh_repository_index("repo").state is IndexLifecycleState.BUSY
        assert controller.repository_index_status("repo").state is IndexLifecycleState.BUSY
        assert not controller._index_operations
        assert not index.embedding.calls
        assert index.manager.open_connections() == 0
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()


def request_for(root: Path, *, temporary: bool = False) -> RepositoryContextRequest:
    return RepositoryContextRequest(
        role="planner", repository_path=root,
        task=DevelopmentTask(id="1", source="system", title="Task", description="", url=""),
        repository_slug="repo", base_branch="main", temporary_source=temporary,
    )


def test_distinct_strategy_retrieval_and_cleanup_share_store_admission(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (index.root / "a.txt").write_text("checkout")
    linked = index.root.parent / "snapshot"
    linked.mkdir()
    (linked / "a.txt").write_text("snapshot at fetched remote base")
    query_entered, cleanup_entered, release = (
        threading.Event(), threading.Event(), threading.Event()
    )
    monkeypatch.setattr(index.strategy, "_validate_request_root", lambda root: root)
    monkeypatch.setattr(index.strategy, "_query", lambda *args: (query_entered.set() or []))
    monkeypatch.setattr(index.strategy, "_render_results", lambda *args: "semantic result")
    delete = index.strategy._delete_source_points

    def cleanup(root_id: str) -> None:
        cleanup_entered.set()
        assert release.wait(TIMEOUT)
        delete(root_id)

    monkeypatch.setattr(index.strategy, "_delete_source_points", cleanup)
    other = QdrantRepositoryContextStrategy(
        index.settings, "other-collection", index.root, embedding_provider=FakeEmbedding(),
        client_manager=index.manager,
    )
    controller = controller_for(index, monkeypatch)
    monkeypatch.setattr("orchestrator.tui.bridge.resolve_repository_context_strategy",
                        lambda *args, **kwargs: other)
    results: list[str] = []
    thread = threading.Thread(target=lambda: results.append(index.strategy.render(
        request_for(linked, temporary=True)
    )), daemon=True)
    thread.start()
    assert cleanup_entered.wait(TIMEOUT)
    try:
        assert query_entered.is_set()
        assert controller.clear_repository_index("repo").state is IndexLifecycleState.BUSY
        # Reentrant first-use build recorded checkout HEAD, not the fetched snapshot's base.
        metadata = index.client.collections[index.strategy.collection_name][
            backend._METADATA_POINT_ID].payload
        assert metadata["last_indexed_commit"] == "checkout-head"
        assert not index.client.collection_exists(other.collection_name)
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert results == ["semantic result"]
    assert index.strategy._read_inventory(
        index.client, backend.canonical_path_identifier(linked)
    ) == {}
    assert index.strategy.inspect_index().document_count == 1


def test_real_strategy_workflow_waits_for_refresh_without_fallback(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (index.root / "a.txt").write_text("source")
    entered, release, attempting, queried = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event()
    )

    def embed() -> None:
        entered.set()
        assert release.wait(TIMEOUT)

    index.embedding.on_embed = embed
    refresher = threading.Thread(target=index.strategy.refresh_changed_files, daemon=True)
    refresher.start()
    assert entered.wait(TIMEOUT)
    other = QdrantRepositoryContextStrategy(
        index.settings, "other", index.root, embedding_provider=FakeEmbedding(),
        client_manager=index.manager,
    )
    monkeypatch.setattr(other, "_query", lambda *args: (queried.set() or []))
    monkeypatch.setattr(other, "_render_results", lambda *args: "semantic result")
    results: list[str] = []

    def workflow() -> None:
        attempting.set()
        results.append(other.render(request_for(index.root)))

    workflow_thread = threading.Thread(target=workflow, daemon=True)
    workflow_thread.start()
    assert attempting.wait(TIMEOUT)
    try:
        assert not queried.is_set()
        assert not index.client.collection_exists(other.collection_name)
    finally:
        release.set()
        refresher.join(TIMEOUT)
        workflow_thread.join(TIMEOUT)
    assert not refresher.is_alive() and not workflow_thread.is_alive()
    assert results == ["semantic result"]


@pytest.mark.parametrize("strategy_name", ["qdrant", "heuristic"])
def test_ready_and_heuristic_openings_do_not_refresh(
    index: Any, monkeypatch: pytest.MonkeyPatch, strategy_name: str,
) -> None:
    controller = controller_for(index, monkeypatch)
    config = controller.project_config("repo")
    config.repository_context.strategy = strategy_name
    if strategy_name == "qdrant":
        index.strategy.build_index()
    monkeypatch.setattr(controller, "_perform_repository_index_operation",
                        lambda *args: pytest.fail("ready opening refreshed"))
    completed = threading.Event()
    statuses: list[Any] = []

    def listener(status: Any) -> None:
        statuses.append(status)
        if status.state is not IndexLifecycleState.INDEXING:
            completed.set()

    controller.request_repository_index("repo", listener)
    assert completed.wait(TIMEOUT)
    assert statuses[-1].state is IndexLifecycleState.READY
    assert statuses[-1].strategy == strategy_name


def test_external_busy_on_open_retries_only_on_next_open(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index.strategy.build_index()
    monkeypatch.setattr(backend, "_git_head", lambda root: "next-head")
    controller = controller_for(index, monkeypatch)
    index.manager._clients.clear()
    busy_calls = 0

    def busy(config: Any) -> Any:
        nonlocal busy_calls
        busy_calls += 1
        raise RuntimeError(
            "Storage folder is already accessed by another instance of Qdrant client"
        )

    index.manager._client_factory = busy
    completed = threading.Event()
    statuses: list[Any] = []

    def listener(status: Any) -> None:
        statuses.append(status)
        if status.state is not IndexLifecycleState.INDEXING:
            completed.set()

    controller.request_repository_index("repo", listener)
    assert completed.wait(TIMEOUT)
    with controller._index_operation_lock:
        assert not controller._index_operations
    assert statuses[-1].state is IndexLifecycleState.BUSY
    assert busy_calls == 1
    index.manager._client_factory = lambda config: index.client
    completed.clear()
    controller.request_repository_index("repo", listener)
    assert completed.wait(TIMEOUT)
    assert statuses[-1].state is IndexLifecycleState.READY
    assert statuses[-1].last_indexed_commit == "next-head"


def test_configuration_change_discards_old_background_completion(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (index.root / "a.txt").write_text("source")
    index.strategy.build_index()
    (index.root / "a.txt").write_text("changed")
    monkeypatch.setattr(backend, "_git_head", lambda root: "next-head")
    controller = controller_for(index, monkeypatch)
    config = controller.project_config("repo")
    entered, release = threading.Event(), threading.Event()

    def embed() -> None:
        entered.set()
        assert release.wait(TIMEOUT)

    index.embedding.on_embed = embed
    statuses: list[Any] = []
    controller.request_repository_index("repo", statuses.append)
    assert entered.wait(TIMEOUT)
    with controller._index_operation_lock:
        run = next(iter(controller._index_operations.values()))
    monkeypatch.setattr(controller, "project_config", lambda repository: config.model_copy(
        update={"path": index.root.parent / "new-checkout"}
    ))
    release.set()
    assert run.done.wait(TIMEOUT)
    assert statuses[-1].state is IndexLifecycleState.INDEXING
    assert not controller._index_operations


def test_shutdown_settles_owned_refresh_before_client_close(
    index: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (index.root / "a.txt").write_text("source")
    controller = controller_for(index, monkeypatch)
    entered, release, waiting, stopped = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event()
    )

    def embed() -> None:
        entered.set()
        assert release.wait(TIMEOUT)

    class ObservedEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waiting.set()
            return super().wait(timeout)

    index.embedding.on_embed = embed
    controller.request_repository_index("repo", lambda status: None, operation="build_index")
    assert entered.wait(TIMEOUT)
    with controller._index_operation_lock:
        run = next(iter(controller._index_operations.values()))
        run.done = ObservedEvent()

    def shutdown() -> None:
        controller.shutdown_for_exit()
        index.manager.close()
        stopped.set()

    thread = threading.Thread(target=shutdown, daemon=True)
    thread.start()
    assert waiting.wait(TIMEOUT)
    try:
        assert controller._index_stopping
        assert index.client.close_calls == 0
        controller.request_repository_index("repo", lambda status: pytest.fail("new admission"))
        assert not stopped.is_set()
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert stopped.is_set()
    assert index.client.close_calls == 1
    assert not controller._index_operations
