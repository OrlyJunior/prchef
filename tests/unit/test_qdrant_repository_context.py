from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from copy import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orchestrator.config.project_config import RepositoryContextSettings
from orchestrator.models import WorkflowExecution, WorkflowStatus
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_context import EmbeddingIdentity, IndexLifecycleState
from orchestrator.services.exceptions import WorkflowCancelledError
from orchestrator.services.qdrant_repository_context import (
    QdrantClientManager,
    QdrantRepositoryContextStrategy,
    canonical_path_identifier,
    create_qdrant_client,
    repository_collection_name,
)
from orchestrator.services.repository_context import RepositoryContextRequest
from orchestrator.services.repository_documents import RepositorySourcePipeline, chunk_source_file
from orchestrator.services.workspace_cleaner import WorkspaceCleanupService

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("qdrant_client") is None,
    reason="qdrant-client is not installed",
)


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        provider: str = "fake",
        model: str = "deterministic",
        version: str = "1",
        dimension: int = 2,
    ) -> None:
        self._identity = EmbeddingIdentity(provider, model, version, dimension)
        self.document_texts: list[str] = []

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_texts.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        lowered = text.casefold()
        if "alpha" in lowered:
            values = [1.0, 0.0]
        elif "beta" in lowered:
            values = [0.0, 1.0]
        else:
            values = [0.70710678, 0.70710678]
        return values + [0.0] * (self._identity.dimension - 2)


def _settings(index_path: Path, *, budget: int = 12_000) -> RepositoryContextSettings:
    return RepositoryContextSettings.model_validate(
        {
            "strategy": "qdrant",
            "context_budget_chars": budget,
            "chunk_size_chars": 256,
            "chunk_overlap_chars": 32,
            "embedding": {
                "provider": "fastembed",
                "model": "unused-in-test",
                "version": "1",
                "dimension": 2,
            },
            "qdrant": {"mode": "local", "path": index_path},
        }
    )


def _request(root: Path, query: str = "alpha") -> RepositoryContextRequest:
    return RepositoryContextRequest(
        role="planner",
        repository_path=root,
        task=DevelopmentTask(
            id="semantic-1",
            source="test",
            title=query,
            description="repository search",
            url="",
        ),
        repository_slug="owner/repository",
        base_branch="develop",
    )


def _repository(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    (root / ".git").mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _committed_repository(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=Test",
            "-c", "user.email=test@example.test", "commit", "-m", "initial",
        ],
        check=True,
        capture_output=True,
    )


def test_collection_identity_is_stable_and_separates_keys_and_paths(tmp_path: Path) -> None:
    first = tmp_path / "First"
    second = tmp_path / "Second"
    first.mkdir()
    second.mkdir()

    assert repository_collection_name("repo", first) == repository_collection_name("repo", first)
    assert repository_collection_name("repo-a", first) != repository_collection_name(
        "repo-b", first
    )
    assert repository_collection_name("repo", first) != repository_collection_name("repo", second)
    assert len(canonical_path_identifier(first)) == 64
    if os.name == "nt":
        assert canonical_path_identifier(first) == canonical_path_identifier(
            Path(str(first).upper())
        )


def test_local_index_persists_and_reuses_unchanged_files(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"alpha.py": "def alpha():\n    return 'semantic'\n"})
    first_embedding = FakeEmbeddingProvider()
    first = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=first_embedding
    )

    first_result = first.render(_request(root))
    embedded_count = len(first_embedding.document_texts)
    second_result = first.render(_request(root))

    assert "alpha.py" in first_result
    assert "alpha.py" in second_result
    assert embedded_count > 0
    assert len(first_embedding.document_texts) == embedded_count

    reopened_embedding = FakeEmbeddingProvider()
    reopened = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=reopened_embedding
    )
    reopened_result = reopened.render(_request(root))

    assert "alpha.py" in reopened_result
    assert reopened_embedding.document_texts == []


def test_unchanged_index_does_not_rewrite_root_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _repository(root, {"alpha.py": "alpha source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    strategy.build_index()
    monkeypatch.setattr(
        strategy, "_write_inventory",
        lambda *args: pytest.fail("unchanged root inventory was rewritten"),
    )

    strategy.refresh_changed_files()


def test_indexed_commit_inspection_does_not_scroll_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    _committed_repository(root, {"alpha.py": "def alpha():\n    return 'semantic'\n"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    assert strategy.inspect_indexed_commit() is None
    strategy.build_index()
    expected = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        strategy, "_scroll_source_points",
        lambda *args: pytest.fail("provenance lookup scrolled repository points"),
    )

    assert strategy.inspect_indexed_commit() == expected


def test_changed_and_deleted_files_replace_stale_points(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "def alpha():\n    return 'alpha'\n"})
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=embedding
    )
    assert "source.py" in strategy.render(_request(root, "alpha"))
    initial_count = len(embedding.document_texts)

    (root / "source.py").write_text("def beta():\n    return 'beta'\n", encoding="utf-8")
    changed = strategy.render(_request(root, "beta"))

    assert "source.py" in changed
    assert "beta" in changed
    assert len(embedding.document_texts) > initial_count

    (root / "source.py").unlink()
    deleted = strategy.render(_request(root, "beta"))

    assert "source.py" not in deleted


def test_repositories_are_collection_isolated_and_unrelated_root_is_rejected(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    index = tmp_path / "index"
    _repository(root_a, {"a.py": "alpha only"})
    _repository(root_b, {"b.py": "beta only"})
    strategy_a = QdrantRepositoryContextStrategy(
        _settings(index), "a", root_a, embedding_provider=FakeEmbeddingProvider()
    )
    strategy_b = QdrantRepositoryContextStrategy(
        _settings(index), "b", root_b, embedding_provider=FakeEmbeddingProvider()
    )

    assert "a.py" in strategy_a.render(_request(root_a, "alpha"))
    assert "b.py" in strategy_b.render(_request(root_b, "beta"))
    assert strategy_a.collection_name != strategy_b.collection_name
    with pytest.raises(ValueError, match="not the configured repository"):
        strategy_a.render(_request(root_b, "alpha"))


def test_linked_worktree_points_are_isolated_within_one_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    index = tmp_path / "index"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "source.py").write_text("alpha control checkout", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "feature", str(worktree)],
        check=True,
        capture_output=True,
    )
    (worktree / "source.py").write_text("beta linked worktree", encoding="utf-8")
    with pytest.raises(ValueError, match="outside repository working trees"):
        QdrantRepositoryContextStrategy(
            _settings(worktree / ".index"),
            "repo",
            root,
            embedding_provider=FakeEmbeddingProvider(),
        )
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )

    control_result = strategy.render(_request(root, "alpha"))
    worktree_result = strategy.render(_request(worktree, "beta"))
    control_again = strategy.render(_request(root, "alpha"))

    assert "alpha control checkout" in control_result
    assert "beta linked worktree" not in control_result
    assert "beta linked worktree" in worktree_result
    assert "alpha control checkout" not in worktree_result
    assert "alpha control checkout" in control_again


def test_temporary_snapshot_reuses_persistent_vectors_and_embeds_only_changed_file(
    tmp_path: Path,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    index = tmp_path / "index"
    _committed_repository(
        root,
        {"unchanged.py": "alpha unchanged source", "changed.py": "beta original source"},
    )
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=embedding
    )
    baseline = strategy.build_index()
    assert baseline.point_count == 2
    embedding.document_texts.clear()
    strategy.build_index()
    assert embedding.document_texts == []
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
        check=True,
        capture_output=True,
    )
    try:
        identical = strategy.render(replace(_request(snapshot), temporary_source=True))
        assert "alpha unchanged source" in identical
        assert embedding.document_texts == []
        (snapshot / "changed.py").write_text("beta changed source", encoding="utf-8")
        changed = strategy.render(replace(_request(snapshot, "beta"), temporary_source=True))
        assert "beta changed source" in changed
        assert embedding.document_texts == ["beta changed source"]
        assert strategy.inspect_index().point_count == baseline.point_count
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(snapshot)
            ) == []
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
            check=True,
            capture_output=True,
        )


def test_snapshot_upserts_only_changed_and_added_files(tmp_path: Path) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    _committed_repository(root, {
        "unchanged.py": "alpha unchanged source",
        "changed.py": "beta old source",
    })
    upserted_paths: list[str] = []

    def factory(settings: object) -> object:
        client = create_qdrant_client(settings)  # type: ignore[arg-type]

        class TrackingClient:
            def __getattr__(self, name: str) -> Any:
                return getattr(client, name)

            def upsert(self, **kwargs: Any) -> Any:
                upserted_paths.extend(
                    point.payload["path"] for point in kwargs["points"]
                    if (point.payload or {}).get("kind") == "chunk"
                )
                return client.upsert(**kwargs)

            def close(self) -> None:
                client.close()

        return TrackingClient()

    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(), client_factory=factory,
    )
    baseline = strategy.build_index()
    upserted_paths.clear()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        original_query = strategy._query
        points_at_query: list[list[str]] = []

        def inspect_query(*args: Any) -> Any:
            with strategy._session() as client:
                points_at_query.append([
                    (point.payload or {})["path"] for point in strategy._scroll_source_points(
                        client, models, canonical_path_identifier(snapshot)
                    )
                ])
            return original_query(*args)

        strategy._query = inspect_query  # type: ignore[method-assign]
        request = replace(_request(snapshot, "alpha"), temporary_source=True)
        assert "alpha unchanged source" in strategy.render(request)
        assert upserted_paths == []
        assert points_at_query == [[]]

        (snapshot / "changed.py").write_text("beta new source", encoding="utf-8")
        (snapshot / "added.py").write_text("beta added source", encoding="utf-8")
        result = strategy.render(replace(_request(snapshot, "beta"), temporary_source=True))
        assert "beta new source" in result
        assert "beta old source" not in result
        assert "beta added source" in result
        assert sorted(upserted_paths) == ["added.py", "changed.py"]
        assert sorted(points_at_query[1]) == ["added.py", "changed.py"]
        assert strategy.inspect_index().point_count == baseline.point_count
        assert strategy.inspect_indexed_commit() == baseline.last_indexed_commit
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
            check=True, capture_output=True,
        )


def test_linked_query_reads_checkout_vectors_once_in_bounded_pages(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {"source.py": "alpha source\n" * 80})
    settings = _settings(tmp_path / "index")
    observed: list[tuple[str, bool, int]] = []
    tracking = {"enabled": False}

    def factory(qdrant_settings: object) -> object:
        client = create_qdrant_client(qdrant_settings)  # type: ignore[arg-type]

        class Client:
            def __getattr__(self, name: str) -> object:
                return getattr(client, name)

            def scroll(self, **kwargs: Any) -> Any:
                page, offset = client.scroll(**kwargs)
                if tracking["enabled"]:
                    observed.append(("scroll", kwargs["with_vectors"], len(page)))
                return page, offset

            def retrieve(self, **kwargs: Any) -> Any:
                if tracking["enabled"]:
                    observed.append(("retrieve", kwargs["with_vectors"], len(kwargs["ids"])))
                return client.retrieve(**kwargs)

            def close(self) -> None:
                client.close()

        return Client()

    strategy = QdrantRepositoryContextStrategy(
        settings, "repo", root, embedding_provider=FakeEmbeddingProvider(),
        client_factory=factory,
    )
    strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        tracking["enabled"] = True
        assert "alpha source" in strategy.render(_request(worktree))
        assert not any(kind == "scroll" for kind, _, _ in observed)
        assert any(kind == "retrieve" and vectors for kind, vectors, _ in observed)
        assert all(count <= 64 for kind, _, count in observed if kind == "retrieve")
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


def test_large_collection_reuse_and_linked_retrieval_use_bounded_ids(tmp_path: Path) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    _committed_repository(root, {
        "same.py": "alpha shared source",
        "changed.py": "beta old source",
    })
    embedding = FakeEmbeddingProvider()
    calls: list[tuple[str, int]] = []
    collection_name = repository_collection_name("repo", root)

    def factory(settings: object) -> object:
        client = create_qdrant_client(settings)  # type: ignore[arg-type]

        class TrackingClient:
            def __getattr__(self, name: str) -> Any:
                return getattr(client, name)

            def scroll(self, **kwargs: Any) -> Any:
                if kwargs["collection_name"] == collection_name:
                    pytest.fail("main repository collection was scrolled")
                return client.scroll(**kwargs)

            def retrieve(self, **kwargs: Any) -> Any:
                if kwargs["collection_name"] == collection_name:
                    calls.append(("retrieve", len(kwargs["ids"])))
                return client.retrieve(**kwargs)

            def close(self) -> None:
                client.close()

        return TrackingClient()

    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=embedding, client_factory=factory,
    )
    strategy.build_index()
    with strategy._session() as client:
        client.upsert(
            collection_name=strategy.collection_name,
            points=[models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, str(number))),
                vector={"dense": [1.0, 0.0]},
                payload={"kind": "unrelated"},
            ) for number in range(512)],
            wait=True,
        )
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        (snapshot / "changed.py").write_text("beta new source", encoding="utf-8")
        embedding.document_texts.clear()
        first_result = strategy.render(
            replace(_request(snapshot, "beta"), temporary_source=True)
        )
        assert "beta new source" in first_result
        assert embedding.document_texts == ["beta new source"]
        assert calls and all(size <= 64 for _, size in calls)
        embedding.document_texts.clear()
        second_result = strategy.render(
            replace(_request(snapshot, "beta"), temporary_source=True)
        )
        assert second_result == first_result
        assert embedding.document_texts == []
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
            check=True, capture_output=True,
        )


def test_worktree_excludes_deleted_checkout_file_and_removes_redundant_points(
    tmp_path: Path,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {
        "deleted.py": "alpha deleted source",
        "changed.py": "beta old source",
        "unchanged.py": "other unchanged source",
    })
    settings = _settings(tmp_path / "index").model_copy(update={"candidate_count": 1})
    strategy = QdrantRepositoryContextStrategy(
        settings, "repo", root, embedding_provider=FakeEmbeddingProvider()
    )
    baseline = strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        (worktree / "deleted.py").unlink()
        (worktree / "changed.py").write_text("alpha new source", encoding="utf-8")
        result = strategy.render(_request(worktree, "alpha"))
        assert "alpha new source" in result
        assert "alpha deleted source" not in result
        assert "beta old source" not in result
        ranked_checkout = strategy.render(_request(worktree, "beta"))
        assert "other unchanged source" in ranked_checkout
        assert "alpha new source" not in ranked_checkout
        with strategy._session() as client:
            points = strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            )
            assert [(point.payload or {})["path"] for point in points] == ["changed.py"]

        (worktree / "changed.py").write_text("beta old source", encoding="utf-8")
        assert "beta old source" in strategy.render(_request(worktree, "beta"))
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            ) == []
        strategy.delete_worktree_points(worktree)
        assert strategy.inspect_index().point_count == baseline.point_count
        assert strategy.inspect_indexed_commit() == baseline.last_indexed_commit
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


def test_failed_worktree_retrieval_keeps_checkout_provenance_and_cleanup_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {"source.py": "alpha checkout source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    baseline = strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        (worktree / "source.py").write_text("beta execution source", encoding="utf-8")

        def fail_query(*_: object) -> None:
            raise RuntimeError("simulated query failure")

        monkeypatch.setattr(strategy, "_query", fail_query)
        assert "LOCAL REPOSITORY CONTEXT" in strategy.render(_request(worktree))
        status = strategy.inspect_index()
        assert status.state is IndexLifecycleState.READY
        assert status.last_indexed_commit == baseline.last_indexed_commit
        assert status.point_count == baseline.point_count
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            )

        strategy.delete_worktree_points(worktree)
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            ) == []
        assert strategy.inspect_index().point_count == baseline.point_count
        assert strategy.inspect_indexed_commit() == baseline.last_indexed_commit
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


@pytest.mark.parametrize(
    "staleness", ["missing", "incomplete", "corrupt", "changed_checkout"]
)
def test_worktree_owns_files_without_a_valid_checkout_chunk_set(
    tmp_path: Path, staleness: str
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    original = "alpha source " * 40
    _committed_repository(root, {"source.py": original})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        if staleness == "changed_checkout":
            (root / "source.py").write_text("beta dirty checkout", encoding="utf-8")
        else:
            with strategy._session() as client:
                points = strategy._scroll_source_points(
                    client, models, canonical_path_identifier(root)
                )
                assert len(points) > 1
                if staleness == "corrupt":
                    client.set_payload(
                        collection_name=strategy.collection_name,
                        payload={"chunk_text": "corrupted checkout chunk"},
                        points=[points[0].id],
                        wait=True,
                    )
                else:
                    doomed = points if staleness == "missing" else points[:1]
                    client.delete(
                        collection_name=strategy.collection_name,
                        points_selector=models.PointIdsList(
                            points=[point.id for point in doomed]
                        ),
                        wait=True,
                    )
        result = strategy.render(_request(worktree, "alpha"))
        assert "alpha source" in result
        assert "corrupted checkout chunk" not in result
        assert "beta dirty checkout" not in result
        with strategy._session() as client:
            points = strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            )
            assert points
            assert all((point.payload or {})["path"] == "source.py" for point in points)
        strategy.delete_worktree_points(worktree)
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            ) == []
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


@pytest.mark.parametrize(
    "invalid_vector",
    [None, {"dense": [1.0]}, {"dense": [float("nan"), 0.0]}, {"dense": [0.0, 0.0]}],
    ids=["missing", "wrong_dimension", "non_finite", "zero"],
)
def test_worktree_owns_checkout_file_with_unusable_dense_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_vector: Any
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {"source.py": "alpha current source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    baseline = strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    original_scroll = strategy._scroll_source_points
    original_validate = strategy._valid_dense_vector

    def validate_with_bad_checkout_vector(point: Any) -> bool:
        if (point.payload or {}).get("source_root_id") == canonical_path_identifier(root):
            corrupted = copy(point)
            corrupted.vector = invalid_vector
            return original_validate(corrupted)
        return original_validate(point)

    monkeypatch.setattr(strategy, "_valid_dense_vector", validate_with_bad_checkout_vector)
    try:
        assert "alpha current source" in strategy.render(_request(worktree, "alpha"))
        with strategy._session() as client:
            own_points = original_scroll(client, models, canonical_path_identifier(worktree))
            assert len(own_points) == baseline.point_count
            assert all((point.payload or {})["path"] == "source.py" for point in own_points)
        assert strategy.inspect_indexed_commit() == baseline.last_indexed_commit
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


def test_removed_checkout_chunk_during_query_cannot_silently_change_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {"source.py": "alpha current source"})
    diagnostics: list[dict[str, str]] = []
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(), on_diagnostic=diagnostics.append,
    )
    strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        original_query = strategy._query

        def remove_approved_point(*args: Any) -> Any:
            with strategy._session() as client:
                points = strategy._scroll_source_points(
                    client, models, canonical_path_identifier(root)
                )
                client.delete(
                    collection_name=strategy.collection_name,
                    points_selector=models.PointIdsList(points=[points[0].id]),
                    wait=True,
                )
            return original_query(*args)

        monkeypatch.setattr(strategy, "_query", remove_approved_point)
        result = strategy.render(_request(worktree, "alpha"))
        assert "alpha current source" in result
        assert "SEMANTIC REPOSITORY CONTEXT" not in result
        assert diagnostics[-1]["kind"] == "REPOSITORY_CONTEXT_FALLBACK"
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


@pytest.mark.parametrize("change", ["rewrite", "delete"])
@pytest.mark.parametrize("temporary", [False, True])
def test_linked_root_change_during_query_cannot_return_old_checkout_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, temporary: bool
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "linked"
    _committed_repository(root, {"source.py": "alpha original source"})
    diagnostics: list[dict[str, str]] = []
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(), on_diagnostic=diagnostics.append,
    )
    baseline = strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        original_query = strategy._query

        def change_root_during_query(*args: Any) -> Any:
            if change == "rewrite":
                (worktree / "source.py").write_text("alpha changed source", encoding="utf-8")
            else:
                (worktree / "source.py").unlink()
            return original_query(*args)

        monkeypatch.setattr(strategy, "_query", change_root_during_query)
        result = strategy.render(replace(_request(worktree, "alpha"), temporary_source=temporary))
        assert "alpha original source" not in result
        if change == "rewrite":
            assert "alpha changed source" in result
        assert diagnostics[-1]["kind"] == "REPOSITORY_CONTEXT_FALLBACK"
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(worktree)
            ) == []
        assert strategy.inspect_index().point_count == baseline.point_count
        assert strategy.inspect_indexed_commit() == baseline.last_indexed_commit
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


def test_first_snapshot_retrieval_builds_durable_index_and_reports_progress(tmp_path: Path) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    _committed_repository(root, {"alpha.py": "def alpha(): return 'semantic'"})
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
        check=True,
        capture_output=True,
    )
    embedding = FakeEmbeddingProvider()
    progress: list[str] = []
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=embedding, on_progress=progress.append,
    )
    try:
        assert strategy.inspect_index().state is IndexLifecycleState.NOT_INDEXED
        context = strategy.render(replace(_request(snapshot), temporary_source=True))
        assert "alpha.py" in context
        assert strategy.inspect_index().state is IndexLifecycleState.READY
        assert len(embedding.document_texts) == 1
        assert any("Discovered 1 files" in message for message in progress)
        assert any("Embedded 1/1 chunks" in message for message in progress)
        assert progress[-1] == "Semantic index ready"
        with strategy._session() as client:
            assert strategy._scroll_source_points(
                client, models, canonical_path_identifier(snapshot)
            ) == []
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
            check=True,
            capture_output=True,
        )


def test_failed_snapshot_cleanup_preserves_cancellation_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    _committed_repository(root, {"source.py": "alpha snapshot source"})
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
        check=True,
        capture_output=True,
    )
    diagnostics: list[dict[str, str]] = []
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(), on_diagnostic=diagnostics.append,
    )
    (snapshot / "source.py").write_text("beta changed snapshot source", encoding="utf-8")
    original_delete = strategy._delete_source_points
    attempts = 0

    def fail_first_delete(source_root_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private snapshot path and credentials")
        original_delete(source_root_id)

    def cancel_query(*_: object) -> None:
        raise WorkflowCancelledError("cancelled")

    monkeypatch.setattr(strategy, "_delete_source_points", fail_first_delete)
    monkeypatch.setattr(strategy, "_query", cancel_query)
    try:
        with pytest.raises(WorkflowCancelledError):
            strategy.render(replace(_request(snapshot), temporary_source=True))
        source_root_id = canonical_path_identifier(snapshot)
        with strategy._session() as client:
            assert strategy._scroll_source_points(client, models, source_root_id)
            assert client.retrieve(
                collection_name=strategy._cleanup_collection_name,
                ids=[strategy._cleanup_marker_id(source_root_id)],
                with_payload=True,
                with_vectors=False,
            )
        assert diagnostics[0]["kind"] == "REPOSITORY_CONTEXT_CLEANUP_FAILURE"
        assert "next retrieval" in diagnostics[0]["remedy"]
        assert "private" not in str(diagnostics)

        recovered = QdrantRepositoryContextStrategy(
            _settings(tmp_path / "index"), "repo", root,
            embedding_provider=FakeEmbeddingProvider(),
        )
        assert "source.py" in recovered.render(_request(root))
        with recovered._session() as client:
            assert recovered._scroll_source_points(client, models, source_root_id) == []
            assert client.retrieve(
                collection_name=recovered._cleanup_collection_name,
                ids=[recovered._cleanup_marker_id(source_root_id)],
                with_payload=True,
                with_vectors=False,
            ) == []
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
            check=True,
            capture_output=True,
        )


def test_ready_retrieval_does_not_scan_index_for_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _repository(root, {"source.py": "alpha source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    strategy.build_index()
    original_scroll = strategy._scroll_source_points
    scans = 0

    def count_scans(*args: Any) -> list[Any]:
        nonlocal scans
        scans += 1
        return original_scroll(*args)

    monkeypatch.setattr(strategy, "_scroll_source_points", count_scans)
    assert "source.py" in strategy.render(_request(root))
    assert scans == 0


def test_embedding_progress_is_reported_after_each_batch(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _repository(root, {f"source_{number}.py": f"def alpha_{number}(): return {number}"
                       for number in range(65)})
    progress: list[str] = []
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(), on_progress=progress.append,
    )

    strategy.render(_request(root))

    assert "Embedded 64/65 chunks" in progress
    assert "Embedded 65/65 chunks" in progress


def test_execution_worktree_reuses_retained_worktree_vectors(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    first = tmp_path / "execution-one"
    second = tmp_path / "execution-two"
    index = tmp_path / "index"
    _committed_repository(root, {"source.py": "alpha control source"})
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=embedding
    )
    strategy.build_index()
    for worktree in (first, second):
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
            check=True,
            capture_output=True,
        )
        (worktree / "source.py").write_text("beta worktree source", encoding="utf-8")
    try:
        embedding.document_texts.clear()
        assert "beta worktree source" in strategy.render(_request(first, "beta"))
        assert embedding.document_texts == ["beta worktree source"]
        embedding.document_texts.clear()
        assert "beta worktree source" in strategy.render(_request(second, "beta"))
        assert embedding.document_texts == []
        assert "beta worktree source" not in strategy.render(_request(root, "alpha"))
    finally:
        for worktree in (first, second):
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
                check=True,
                capture_output=True,
            )


def test_content_cache_survives_snapshot_cleanup_and_reuses_across_path_and_ordinal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    execution = tmp_path / "execution"
    _committed_repository(root, {"base.py": "alpha base source"})
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root, embedding_provider=embedding
    )
    strategy.build_index()
    for worktree in (snapshot, execution):
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
            check=True, capture_output=True,
        )
    try:
        (snapshot / "first.py").write_text("beta shared text", encoding="utf-8")
        embedding.document_texts.clear()
        assert "beta shared text" in strategy.render(
            replace(_request(snapshot, "beta"), temporary_source=True)
        )
        assert embedding.document_texts == ["beta shared text"]
        (execution / "other.py").write_text("x" * 255 + "\nbeta shared text", encoding="utf-8")
        embedding.document_texts.clear()
        assert "beta shared text" in strategy.render(_request(execution, "beta"))
        assert "beta shared text" not in embedding.document_texts
    finally:
        for worktree in (snapshot, execution):
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
                check=True, capture_output=True,
            )


def test_snapshot_then_changed_root_embeds_only_unseen_text(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    snapshot = tmp_path / "planning"
    execution = tmp_path / "execution"
    _committed_repository(root, {
        "one.py": "alpha one source",
        "two.py": "alpha two source",
        "three.py": "beta old source",
    })
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root, embedding_provider=embedding
    )
    strategy.build_index()
    for worktree in (snapshot, execution):
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
            check=True, capture_output=True,
        )
    try:
        embedding.document_texts.clear()
        assert "one.py" in strategy.render(
            replace(_request(snapshot, "alpha"), temporary_source=True)
        )
        assert embedding.document_texts == []
        (execution / "three.py").write_text("beta new source", encoding="utf-8")
        assert "beta new source" in strategy.render(_request(execution, "beta"))
        assert embedding.document_texts == ["beta new source"]
    finally:
        for worktree in (snapshot, execution):
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
                check=True, capture_output=True,
            )


@pytest.mark.parametrize(
    "defect", ["missing", "wrong_text", "wrong_hash", "wrong_identity", "invalid_vector"]
)
def test_invalid_content_cache_candidate_is_embedded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    worktree = tmp_path / "execution"
    _committed_repository(root, {"source.py": "alpha shared source"})
    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root, embedding_provider=embedding
    )
    strategy.build_index()
    cache_id = strategy._content_id("alpha shared source")
    with strategy._session() as client:
        if defect == "missing":
            client.delete(
                collection_name=strategy.collection_name,
                points_selector=models.PointIdsList(points=[cache_id]), wait=True,
            )
        elif defect != "invalid_vector":
            changes: dict[str, Any] = {
                "wrong_text": {"chunk_text": "different"},
                "wrong_hash": {"text_hash": "different"},
                "wrong_identity": {"embedding": {"provider": "different"}},
            }
            client.set_payload(
                collection_name=strategy.collection_name,
                payload=changes[defect], points=[cache_id], wait=True,
            )
    if defect == "invalid_vector":
        original_validate = strategy._valid_dense_vector

        def reject_cache(point: Any) -> bool:
            return ((point.payload or {}).get("kind") != "content_cache"
                    and original_validate(point))

        monkeypatch.setattr(strategy, "_valid_dense_vector", reject_cache)
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    try:
        (worktree / "other.py").write_text("alpha shared source", encoding="utf-8")
        embedding.document_texts.clear()
        assert "other.py" in strategy.render(_request(worktree, "alpha"))
        assert embedding.document_texts == ["alpha shared source"]
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
            check=True, capture_output=True,
        )


@pytest.mark.parametrize(
    "defect", [
        "missing", "wrong_text", "wrong_hash", "wrong_identity", "wrong_schema",
        "invalid_vector",
    ],
)
def test_invalid_checkout_counterpart_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    _repository(root, {"source.py": "alpha source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=FakeEmbeddingProvider(),
    )
    strategy.build_index()
    source = RepositorySourcePipeline("repo", root).discover()[0]
    chunks = chunk_source_file(
        source, source_root_id="linked", chunk_size_chars=256, overlap_chars=32,
    )
    with strategy._session() as client:
        client.delete(
            collection_name=strategy.collection_name,
            points_selector=models.PointIdsList(points=[strategy._content_id(chunks[0].text)]),
            wait=True,
        )
        checkout_id = strategy._scroll_source_points(
            client, models, canonical_path_identifier(root)
        )[0].id
        if defect == "missing":
            client.delete(
                collection_name=strategy.collection_name,
                points_selector=models.PointIdsList(points=[checkout_id]), wait=True,
            )
        elif defect != "invalid_vector":
            changes: dict[str, Any] = {
                "wrong_text": {"chunk_text": "different"},
                "wrong_hash": {"content_hash": "different"},
                "wrong_identity": {"embedding": {"provider": "different"}},
                "wrong_schema": {"index_schema_version": -1},
            }
            client.set_payload(
                collection_name=strategy.collection_name,
                points=[checkout_id], payload=changes[defect], wait=True,
            )
    if defect == "invalid_vector":
        original_validate = strategy._valid_dense_vector
        monkeypatch.setattr(
            strategy, "_valid_dense_vector",
            lambda point: (False if (point.payload or {}).get("kind") == "chunk"
                           else original_validate(point)),
        )
    with strategy._session() as client:
        assert strategy._reusable_vectors(client, models, chunks) == {}


def test_cleanup_removes_execution_worktree_points_before_worktree(tmp_path: Path) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    workspace_root = tmp_path / "workspaces"
    worktree = workspace_root / root.name / "execution-one"
    index = tmp_path / "index"
    _committed_repository(root, {"source.py": "alpha control source"})
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )
    strategy.build_index()
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    assert "alpha control source" in strategy.render(_request(worktree))

    class Repository:
        def list_executions(self) -> list[WorkflowExecution]:
            return [
                WorkflowExecution(
                    id="execution-one",
                    workspace_path=str(worktree),
                    status=WorkflowStatus.APPROVED,
                )
            ]

    class Git:
        def remove_worktree(self, path: Path) -> bool:
            with strategy._session() as client:
                assert strategy._scroll_source_points(
                    client, models, canonical_path_identifier(path)
                ) == []
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
                check=True,
                capture_output=True,
            )
            return True

    service = WorkspaceCleanupService(  # type: ignore[arg-type]
        Repository(), Git(), root, workspace_root,
        delete_index_points=strategy.delete_worktree_points,
    )
    report = service.cleanup(dry_run=False)
    assert report.removed_count == 1
    assert not worktree.exists()
    assert strategy.inspect_index().point_count == 1


def test_temporary_planning_worktrees_leave_index_unchanged_on_query_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qdrant_client import models

    root = tmp_path / "repository"
    index = tmp_path / "index"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    source = root / "source.py"
    source.write_text("alpha committed content", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )
    baseline = strategy.build_index()
    with pytest.raises(ValueError, match="requires a linked worktree"):
        strategy.render(replace(_request(root), temporary_source=True))
    source.write_text("beta fetched base content", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "remote base",
        ],
        check=True,
        capture_output=True,
    )
    fetched_commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert baseline.last_indexed_commit != fetched_commit
    source.write_text("gamma dirty control content", encoding="utf-8")

    for index_number in range(2):
        snapshot = tmp_path / f"planning-{index_number}"
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(snapshot), "HEAD"],
            check=True,
            capture_output=True,
        )
        try:
            request = replace(_request(snapshot, "beta"), temporary_source=True)
            if index_number == 1:
                def fail_query(*_: object) -> None:
                    raise RuntimeError("simulated query failure")

                monkeypatch.setattr(strategy, "_query", fail_query)
            before = strategy.inspect_index()
            result = strategy.render(request)
            if index_number == 0:
                assert "beta fetched base content" in result
                assert "gamma dirty control content" not in result
            else:
                assert "LOCAL REPOSITORY CONTEXT" in result
            assert source.read_text(encoding="utf-8") == "gamma dirty control content"
            status = strategy.inspect_index()
            assert status.state is before.state is IndexLifecycleState.STALE
            assert status.error_message == before.error_message is None
            assert status.last_indexed_commit == baseline.last_indexed_commit
            assert status.point_count == baseline.point_count
            with strategy._session() as client:
                assert strategy._scroll_source_points(
                    client,
                    models,
                    canonical_path_identifier(snapshot),
                ) == []
        finally:
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(snapshot)],
                check=True,
                capture_output=True,
            )

    failed = strategy.render(_request(root, "alpha"))
    assert "LOCAL REPOSITORY CONTEXT" in failed
    status = strategy.inspect_index()
    assert status.state is IndexLifecycleState.FAILED
    assert status.error_message == "Semantic index operation failed (RuntimeError)."


@pytest.mark.parametrize(
    "changed_identity",
    [
        {"provider": "other"},
        {"model": "other"},
        {"version": "2"},
        {"dimension": 3},
    ],
)
def test_embedding_identity_change_recreates_and_reindexes_collection(
    tmp_path: Path, changed_identity: dict[str, Any]
) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"alpha.py": "alpha semantic source"})
    original = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider(version="1")
    )
    original.render(_request(root))
    replacement_embedding = FakeEmbeddingProvider(**changed_identity)
    replacement = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=replacement_embedding
    )

    result = replacement.render(_request(root))

    assert "alpha.py" in result
    assert replacement_embedding.document_texts


def test_semantic_ties_are_deterministic_and_rendering_obeys_budget(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(
        root,
        {
            "b.py": "alpha " * 70,
            "a.py": "alpha " * 70,
        },
    )
    strategy = QdrantRepositoryContextStrategy(
        _settings(index, budget=900),
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
    )

    result = strategy.render(_request(root, "alpha"))

    assert len(result) <= 900
    if "a.py" in result and "b.py" in result:
        assert result.index("a.py") < result.index("b.py")


def test_qdrant_failure_is_sanitized_and_does_not_mutate_repository(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "alpha source"})
    before = (root / "source.py").read_bytes()
    diagnostics: list[dict[str, str]] = []

    def fail_client(_: object) -> object:
        raise RuntimeError("secret server details")

    strategy = QdrantRepositoryContextStrategy(
        _settings(index),
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
        client_factory=fail_client,
        on_diagnostic=diagnostics.append,
    )

    result = strategy.render(_request(root))

    assert "LOCAL REPOSITORY CONTEXT" in result
    assert "secret server details" not in result
    assert diagnostics[0]["kind"] == "REPOSITORY_CONTEXT_FALLBACK"
    assert "secret server details" not in str(diagnostics)
    assert diagnostics[0]["remedy"]
    assert (root / "source.py").read_bytes() == before


def test_unavailable_embedding_model_uses_heuristic_with_download_remedy(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _repository(root, {"source.py": "def alpha(): return 'fallback'"})
    diagnostics: list[dict[str, str]] = []

    class UnavailableEmbedding(FakeEmbeddingProvider):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("offline download at /private/secret")

    strategy = QdrantRepositoryContextStrategy(
        _settings(tmp_path / "index"), "repo", root,
        embedding_provider=UnavailableEmbedding(), on_diagnostic=diagnostics.append,
    )

    assert "LOCAL REPOSITORY CONTEXT" in strategy.render(_request(root))
    assert "download" in diagnostics[0]["reason"]
    assert "Retry online" in diagnostics[0]["remedy"]
    assert "/private/secret" not in str(diagnostics)


def test_failed_partial_sync_is_not_queried_and_retry_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "alpha source"})
    settings = _settings(index)
    state = {"fail": True, "queries": 0}

    def factory(qdrant_settings: object) -> object:
        client = create_qdrant_client(qdrant_settings)  # type: ignore[arg-type]

        class Client:
            def __getattr__(self, name: str) -> object:
                return getattr(client, name)

            def upsert(self, **kwargs: object) -> object:
                result = client.upsert(**kwargs)
                points = kwargs.get("points", [])
                if state["fail"] and any(
                    (point.payload or {}).get("kind") == "chunk"  # type: ignore[union-attr]
                    for point in points
                ):
                    state["fail"] = False
                    raise RuntimeError("injected partial failure")
                return result

            def query_points(self, **kwargs: object) -> object:
                state["queries"] += 1
                return client.query_points(**kwargs)

            def close(self) -> None:
                client.close()

        return Client()

    embedding = FakeEmbeddingProvider()
    strategy = QdrantRepositoryContextStrategy(
        settings,
        "repo",
        root,
        embedding_provider=embedding,
        client_factory=factory,
    )

    failed = strategy.render(_request(root))
    recovered = strategy.render(_request(root))

    assert "LOCAL REPOSITORY CONTEXT" in failed
    assert state["queries"] == 1
    assert "source.py" in recovered


def test_collection_inspection_failure_does_not_delete_existing_index(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "alpha source"})
    settings = _settings(index)
    QdrantRepositoryContextStrategy(
        settings,
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
    ).render(_request(root))
    state = {"deletes": 0}

    def factory(qdrant_settings: object) -> object:
        client = create_qdrant_client(qdrant_settings)  # type: ignore[arg-type]

        class Client:
            def __getattr__(self, name: str) -> object:
                return getattr(client, name)

            def get_collection(self, collection_name: str) -> object:
                raise RuntimeError("transient inspection failure")

            def delete_collection(self, collection_name: str) -> object:
                state["deletes"] += 1
                return client.delete_collection(collection_name)

            def close(self) -> None:
                client.close()

        return Client()

    failed = QdrantRepositoryContextStrategy(
        settings,
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
        client_factory=factory,
    ).render(_request(root))

    assert "LOCAL REPOSITORY CONTEXT" in failed
    assert "transient inspection failure" not in failed
    assert state["deletes"] == 0
    recovered = QdrantRepositoryContextStrategy(
        settings,
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
    ).render(_request(root))
    assert "source.py" in recovered


def test_index_lifecycle_build_refresh_stale_and_clear(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "source.py").write_text("def alpha():\n    return 'alpha'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    strategy = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )

    assert strategy.inspect_index().state is IndexLifecycleState.NOT_INDEXED
    built = strategy.build_index()
    assert built.state is IndexLifecycleState.READY
    assert built.last_successful_indexed_at is not None
    assert built.last_indexed_commit is not None
    assert built.document_count == 1
    assert built.point_count >= 1

    (root / "source.py").write_text("def beta():\n    return 'beta'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "change",
        ],
        check=True,
        capture_output=True,
    )
    assert strategy.inspect_index().state is IndexLifecycleState.STALE
    assert strategy.refresh_changed_files().state is IndexLifecycleState.READY
    assert strategy.clear_index().state is IndexLifecycleState.NOT_INDEXED


def test_semantic_failure_uses_heuristic_fallback_when_enabled(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "def alpha():\n    return 'fallback'\n"})
    settings = _settings(index).model_copy(update={"fallback_to_heuristic": True})

    strategy = QdrantRepositoryContextStrategy(
        settings,
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
        client_factory=lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    result = strategy.render(_request(root, "alpha"))

    assert "LOCAL REPOSITORY CONTEXT" in result
    assert "source.py" in result


def test_injected_manager_reuses_one_client_across_operations(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "alpha source"})
    opened: list[object] = []
    closed: list[object] = []

    def factory(qdrant_settings: object) -> object:
        client = create_qdrant_client(qdrant_settings)  # type: ignore[arg-type]

        class Client:
            def __getattr__(self, name: str) -> object:
                return getattr(client, name)

            def close(self) -> None:
                closed.append(self)
                client.close()

        tracked = Client()
        opened.append(tracked)
        return tracked

    manager = QdrantClientManager(factory)
    strategy = QdrantRepositoryContextStrategy(
        _settings(index),
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
        client_manager=manager,
    )

    strategy.render(_request(root))
    strategy.inspect_index()
    # The strategy does not own the manager, so it never closes it.
    strategy.close()

    assert len(opened) == 1
    assert closed == []

    manager.close()
    assert len(closed) == 1


def test_a_standalone_strategy_does_not_hold_the_local_store_open(tmp_path: Path) -> None:
    """Embedded Qdrant locks its directory, so an unshared client must not linger."""

    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"source.py": "alpha source"})

    first = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )
    assert "source.py" in first.render(_request(root))

    # A second strategy over the same store must still be able to open it.
    second = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider()
    )
    assert "source.py" in second.render(_request(root))
    assert second.inspect_index().state is IndexLifecycleState.READY
