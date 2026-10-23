from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from prchef.models.repository_context import EmbeddingIdentity
from prchef.services.embeddings import FastEmbedEmbeddingProvider


@pytest.mark.parametrize("initialized", [False, True])
def test_download_completion_requires_initialized_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialized: bool
) -> None:
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"x" * 1_048_576)
    cache_scans = iter(([], [model_file]))
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: next(cache_scans))

    messages: list[str] = []
    provider = FastEmbedEmbeddingProvider(
        EmbeddingIdentity("fastembed", "test-model", "1", 2), messages.append
    )
    finished = threading.Event()
    finished.set()
    model_ready = threading.Event()
    if initialized:
        model_ready.set()

    provider._watch_download(tmp_path, finished, model_ready)

    assert messages == (
        ["Local embedding model download finished: 1 MiB received"] if initialized else []
    )


def test_fastembed_receives_bounded_cpu_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    import prchef.services.embeddings as embeddings

    created: list[tuple[str, int]] = []

    class FakeTextEmbedding:
        def __init__(self, *, model_name: str, threads: int) -> None:
            created.append((model_name, threads))

        def passage_embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        embeddings,
        "_load_optional_module",
        lambda _: SimpleNamespace(TextEmbedding=FakeTextEmbedding),
    )
    provider = FastEmbedEmbeddingProvider(
        EmbeddingIdentity("fastembed", "test-model", "1", 2), cpu_threads=3
    )

    assert provider.embed_documents(["source"]) == [[1.0, 0.0]]
    assert created == [("test-model", 3)]
