"""Provider-neutral embedding boundary and lazy local implementation."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from prchef.models.repository_context import EmbeddingIdentity


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingIdentity:
        """Return immutable compatibility metadata without loading a model."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed repository chunks in input order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one semantic query."""
        ...


class FastEmbedEmbeddingProvider:
    """Lazy local embedding provider backed by FastEmbed."""

    def __init__(
        self,
        identity: EmbeddingIdentity,
        on_progress: Callable[[str], None] | None = None,
        *,
        cpu_threads: int = 2,
    ) -> None:
        if cpu_threads < 1:
            raise ValueError("Embedding CPU thread count must be positive")
        self._identity = identity
        self._model: object | None = None
        self._on_progress = on_progress
        self._cpu_threads = cpu_threads

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        method = getattr(model, "passage_embed", None) or model.embed
        vectors = self._materialize(method(list(texts)))
        self._validate(vectors, len(texts))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        model = self._get_model()
        method = getattr(model, "query_embed", None) or model.embed
        vectors = self._materialize(method([text]))
        self._validate(vectors, 1)
        return vectors[0]

    def _get_model(self) -> Any:
        if self._model is None:
            if self._on_progress is not None:
                self._on_progress("Loading local embedding model; first use may download it")
            try:
                text_embedding = _load_optional_module("fastembed").TextEmbedding
            except ImportError as exc:  # pragma: no cover - packaging regression guard
                raise RuntimeError("FastEmbed is required for Qdrant repository context") from exc
            # FastEmbed does not expose a download callback. Observe only byte
            # counts in its cache while it initializes; no cache paths or model
            # downloader output cross the diagnostic boundary.
            finished = threading.Event()
            initialized = threading.Event()
            watcher: threading.Thread | None = None
            if self._on_progress is not None:
                configured_cache = os.getenv("FASTEMBED_CACHE_PATH")
                cache = (
                    Path(configured_cache)
                    if configured_cache else Path(tempfile.gettempdir()) / "fastembed_cache"
                )
                watcher = threading.Thread(
                    target=self._watch_download,
                    args=(cache, finished, initialized),
                    daemon=True,
                    name="orchestrator-embedding-download-progress",
                )
                watcher.start()
            try:
                self._model = text_embedding(
                    model_name=self._identity.model, threads=self._cpu_threads
                )
                initialized.set()
            finally:
                finished.set()
                if watcher is not None:
                    watcher.join(timeout=1)
            if self._on_progress is not None:
                self._on_progress("Local embedding model ready")
        return self._model

    def _watch_download(
        self, cache: Path, finished: threading.Event, initialized: threading.Event
    ) -> None:
        assert self._on_progress is not None

        def size() -> int:
            try:
                return sum(
                    path.stat().st_size for path in cache.rglob("*") if path.is_file()
                )
            except OSError:
                return 0

        baseline = size()
        last_reported = 0
        while not finished.wait(0.5):
            received = max(0, size() - baseline)
            if received - last_reported >= 8 * 1_048_576:
                last_reported = received
                self._on_progress(
                    f"Downloading local embedding model: {received // 1_048_576} MiB received"
                )
        received = max(0, size() - baseline)
        if received and initialized.is_set():
            self._on_progress(
                f"Local embedding model download finished: {received // 1_048_576} MiB received"
            )

    @staticmethod
    def _materialize(vectors: Iterable[Any]) -> list[list[float]]:
        materialized: list[list[float]] = []
        for vector in vectors:
            values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
            materialized.append([float(value) for value in values])
        return materialized

    def _validate(self, vectors: list[list[float]], expected_count: int) -> None:
        if len(vectors) != expected_count:
            raise RuntimeError("Embedding provider returned an unexpected vector count")
        if any(len(vector) != self._identity.dimension for vector in vectors):
            raise RuntimeError(
                "Embedding provider vector dimension does not match configured dimension"
            )


def _load_optional_module(name: str) -> Any:
    """Load an implementation dependency without importing its types into this boundary."""

    return import_module(name)
