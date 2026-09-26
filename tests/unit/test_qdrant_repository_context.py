from __future__ import annotations

import importlib.util
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.config.project_config import RepositoryContextSettings
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_context import EmbeddingIdentity, IndexLifecycleState
from orchestrator.services.qdrant_repository_context import (
    QdrantClientManager,
    QdrantRepositoryContextStrategy,
    canonical_path_identifier,
    create_qdrant_client,
    repository_collection_name,
)
from orchestrator.services.repository_context import RepositoryContextRequest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("qdrant_client") is None,
    reason="qdrant-client is not installed",
)


class FakeEmbeddingProvider:
    def __init__(self, *, version: str = "1") -> None:
        self._identity = EmbeddingIdentity("fake", "deterministic", version, 2)
        self.document_texts: list[str] = []

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_texts.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        if "alpha" in lowered:
            return [1.0, 0.0]
        if "beta" in lowered:
            return [0.0, 1.0]
        return [0.70710678, 0.70710678]


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
                assert "REPOSITORY CONTEXT DIAGNOSTIC" in result
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
    assert "REPOSITORY CONTEXT DIAGNOSTIC" in failed
    status = strategy.inspect_index()
    assert status.state is IndexLifecycleState.FAILED
    assert status.error_message == "Semantic index operation failed (RuntimeError)."


def test_embedding_version_change_recreates_and_reindexes_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    index = tmp_path / "index"
    _repository(root, {"alpha.py": "alpha semantic source"})
    original = QdrantRepositoryContextStrategy(
        _settings(index), "repo", root, embedding_provider=FakeEmbeddingProvider(version="1")
    )
    original.render(_request(root))
    replacement_embedding = FakeEmbeddingProvider(version="2")
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

    def fail_client(_: object) -> object:
        raise RuntimeError("secret server details")

    strategy = QdrantRepositoryContextStrategy(
        _settings(index),
        "repo",
        root,
        embedding_provider=FakeEmbeddingProvider(),
        client_factory=fail_client,
    )

    result = strategy.render(_request(root))

    assert "REPOSITORY CONTEXT DIAGNOSTIC" in result
    assert "secret server details" not in result
    assert (root / "source.py").read_bytes() == before


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
                    (point.payload or {}).get("kind") == "chunk" for point in points  # type: ignore[union-attr]
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

    assert "REPOSITORY CONTEXT DIAGNOSTIC" in failed
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

    assert "REPOSITORY CONTEXT DIAGNOSTIC" in failed
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
