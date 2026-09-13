"""Infrastructure-neutral repository indexing and retrieval records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    provider: str
    model: str
    version: str
    dimension: int

    def as_payload(self) -> dict[str, str | int]:
        return {
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "dimension": self.dimension,
        }


@dataclass(frozen=True, slots=True)
class RepositorySourceFile:
    repository: str
    path: str
    language: str
    content_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class RepositoryChunk:
    repository: str
    source_root_id: str
    path: str
    language: str
    symbol: str | None
    content_hash: str
    start_line: int
    end_line: int
    text: str
    ordinal: int
    point_id: str


@dataclass(frozen=True, slots=True)
class RetrievedRepositoryChunk:
    point_id: str
    score: float
    repository: str
    path: str
    language: str
    symbol: str | None
    content_hash: str
    start_line: int
    end_line: int
    text: str
