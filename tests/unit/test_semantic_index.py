from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from orchestrator.services.semantic_index import (
    RepositorySemanticIndex,
    SemanticIndexError,
    SemanticIndexManager,
    resolve_semantic_index_root,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_two_repositories_have_isolated_storage_and_retrieval(tmp_path: Path) -> None:
    repository_a = tmp_path / "repos" / "a"
    repository_b = tmp_path / "repos" / "b"
    _write(repository_a / "alpha.py", "def alpha_only():\n    return 'alpha'\n")
    _write(repository_b / "beta.py", "def beta_only():\n    return 'beta'\n")
    manager = SemanticIndexManager(tmp_path / "state" / "indices")

    manager.index_repository("repo-a", repository_a)
    manager.index_repository("repo-b", repository_b)

    index_a = manager.for_repository("repo-a", repository_a)
    index_b = manager.for_repository("repo-b", repository_b)
    assert index_a.storage_path != index_b.storage_path
    assert index_a.storage_path.is_file()
    assert index_b.storage_path.is_file()
    assert all(chunk.repository == "repo-a" for chunk in index_a.chunks())
    assert all(chunk.repository == "repo-b" for chunk in index_b.chunks())
    assert [chunk.path for chunk in index_a.retrieve("alpha_only")] == ["alpha.py"]
    assert index_a.retrieve("beta_only") == []
    assert index_b.retrieve("alpha_only") == []


def test_repository_identity_uses_key_and_canonical_path(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    root = tmp_path / "indices"

    first = RepositorySemanticIndex("first", repository, index_root=root)
    second = RepositorySemanticIndex("second", repository, index_root=root)
    equivalent = RepositorySemanticIndex("first", repository / ".", index_root=root)

    assert first.storage_path != second.storage_path
    assert first.storage_path == equivalent.storage_path


def test_second_refresh_reuses_unchanged_files_without_rechunking(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    _write(repository / "source.py", "def stable():\n    return 1\n")
    index = RepositorySemanticIndex("repo", repository, index_root=tmp_path / "indices")
    first = index.refresh()
    original_chunk_file = index._chunk_file
    calls = 0

    def count_chunks(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original_chunk_file(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(index, "_chunk_file", count_chunks)
    second = index.refresh()

    assert first.indexed_files == 1
    assert second.indexed_files == 0
    assert second.unchanged_files == 1
    assert calls == 0


def test_modified_file_replaces_chunks_and_deleted_file_is_purged(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    changed = repository / "changed.py"
    removed = repository / "removed.py"
    _write(changed, "def before():\n    return 'old'\n")
    _write(removed, "def removed():\n    return True\n")
    index = RepositorySemanticIndex("repo", repository, index_root=tmp_path / "indices")
    index.refresh()
    old_hash = next(chunk.content_hash for chunk in index.chunks() if chunk.path == "changed.py")

    changed.write_text("def after():\n    return 'new'\n", encoding="utf-8")
    removed.unlink()
    result = index.refresh()

    chunks = index.chunks()
    changed_chunks = [chunk for chunk in chunks if chunk.path == "changed.py"]
    assert result.indexed_files == 1
    assert result.deleted_files == 1
    assert changed_chunks[0].content_hash != old_hash
    assert changed_chunks[0].symbol == "after"
    assert "new" in changed_chunks[0].text
    assert all(chunk.path != "removed.py" for chunk in chunks)


def test_shared_ignore_rules_omit_generated_hidden_private_binary_and_symlinks(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _write(repository / "src" / "included.py", "def included():\n    pass\n")
    _write(repository / "build" / "generated.py", "def generated():\n    pass\n")
    _write(repository / ".private" / "secret.py", "TOKEN = 'secret'\n")
    _write(repository / ".hidden.py", "TOKEN = 'secret'\n")
    _write(repository / "IMPLEMENTER.md", "private agent guidance")
    binary = repository / "binary.py"
    binary.write_bytes(b"before\0after")
    link = repository / "linked.py"
    try:
        link.symlink_to(repository / "src" / "included.py")
    except (OSError, NotImplementedError):
        link = None  # type: ignore[assignment]
    index = RepositorySemanticIndex("repo", repository, index_root=tmp_path / "indices")

    index.refresh()

    paths = {chunk.path for chunk in index.chunks()}
    assert paths == {"src/included.py"}
    if link is not None:
        assert "linked.py" not in paths


def test_index_survives_a_new_service_instance(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _write(repository / "persistent.py", "def persists():\n    return True\n")
    root = tmp_path / "indices"
    RepositorySemanticIndex("repo", repository, index_root=root).refresh()

    reloaded = RepositorySemanticIndex("repo", repository, index_root=root)

    assert [chunk.symbol for chunk in reloaded.chunks()] == ["persists"]


def test_refresh_failure_rolls_back_to_prior_usable_index(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    source = repository / "safe.py"
    _write(source, "def safe():\n    return 'before'\n")
    root = tmp_path / "indices"
    index = RepositorySemanticIndex("repo", repository, index_root=root)
    index.refresh()
    prior_chunks = index.chunks()
    source.write_text("def unsafe():\n    return 'after'\n", encoding="utf-8")

    def fail(*_: object, **__: object):
        raise RuntimeError("simulated chunking failure")

    monkeypatch.setattr(index, "_chunk_file", fail)
    with pytest.raises(SemanticIndexError, match="simulated chunking failure"):
        index.refresh()

    reloaded = RepositorySemanticIndex("repo", repository, index_root=root)
    assert reloaded.chunks() == prior_chunks


def test_chunks_are_bounded_and_include_location_language_and_symbol(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _write(
        repository / "bounded.py",
        "def bounded():\n" + "    value = 'abcdefghij'\n" * 8,
    )
    index = RepositorySemanticIndex(
        "repo",
        repository,
        index_root=tmp_path / "indices",
        max_chunk_chars=50,
        max_chunk_lines=3,
    )
    index.refresh()

    chunks = index.chunks()
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 50 for chunk in chunks)
    assert all(chunk.end_line - chunk.start_line + 1 <= 3 for chunk in chunks)
    assert chunks[0].language == "python"
    assert chunks[0].symbol == "bounded"
    assert chunks[0].location.startswith("bounded.py:1-")


def test_index_root_resolution_and_working_tree_guard(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert resolve_semantic_index_root(environ={}, home=home) == (
        home / ".orchestrator" / "indices"
    ).resolve()
    configured = resolve_semantic_index_root(
        environ={"ORCHESTRATOR_SEMANTIC_INDEX_PATH": str(tmp_path / "configured")},
        home=home,
    )
    assert configured == (tmp_path / "configured").resolve()

    repository = tmp_path / "repo"
    repository.mkdir()
    with pytest.raises(ValueError, match="outside"):
        RepositorySemanticIndex("repo", repository, index_root=repository / ".state")
