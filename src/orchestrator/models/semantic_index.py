from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RepositoryIndexIdentity:
    """Stable identity for one configured repository checkout."""

    repository_key: str
    canonical_path: str
    identifier: str

    @classmethod
    def from_repository(
        cls, repository_key: str, repository_path: str | Path
    ) -> RepositoryIndexIdentity:
        key = repository_key.strip()
        if not key:
            raise ValueError("repository_key must not be empty")
        resolved = Path(repository_path).expanduser().resolve()
        # normcase makes identity stable across case-only spelling differences on
        # case-insensitive platforms while preserving POSIX case semantics.
        canonical_path = os.path.normcase(str(resolved))
        payload = json.dumps(
            {"repository_key": key, "canonical_path": canonical_path},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identifier = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(
            repository_key=key,
            canonical_path=canonical_path,
            identifier=identifier,
        )

    @classmethod
    def create(
        cls, repository_key: str, repository_path: str | Path
    ) -> RepositoryIndexIdentity:
        """Compatibility spelling for callers constructing identities directly."""

        return cls.from_repository(repository_key, repository_path)

    @property
    def identity_hash(self) -> str:
        return self.identifier

    @property
    def namespace(self) -> str:
        return self.identifier


@dataclass(frozen=True, slots=True)
class IndexChunk:
    """One bounded, repository-scoped source excerpt."""

    repository: str
    path: str
    language: str | None
    symbol: str | None
    content_hash: str
    text: str
    start_line: int
    end_line: int
    chunk_id: str = ""

    @property
    def repository_key(self) -> str:
        return self.repository

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True, slots=True)
class IndexRefreshResult:
    """Observable incremental work performed by one atomic refresh."""

    indexed_files: int
    unchanged_files: int
    deleted_files: int
    indexed_chunks: int
    total_files: int
    total_chunks: int

    @property
    def reindexed_files(self) -> int:
        return self.indexed_files

    @property
    def reused_files(self) -> int:
        return self.unchanged_files
