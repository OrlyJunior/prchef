"""Provider-neutral embedding boundary and lazy local implementation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from importlib import import_module
from typing import Any, Protocol

from orchestrator.models.repository_context import EmbeddingIdentity


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

    def __init__(self, identity: EmbeddingIdentity) -> None:
        self._identity = identity
        self._model: object | None = None

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
            try:
                text_embedding = _load_optional_module("fastembed").TextEmbedding
            except ImportError as exc:  # pragma: no cover - packaging regression guard
                raise RuntimeError("FastEmbed is required for Qdrant repository context") from exc
            self._model = text_embedding(model_name=self._identity.model)
        return self._model

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
