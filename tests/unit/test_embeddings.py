from __future__ import annotations

import threading
from pathlib import Path

import pytest

from orchestrator.models.repository_context import EmbeddingIdentity
from orchestrator.services.embeddings import FastEmbedEmbeddingProvider


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
