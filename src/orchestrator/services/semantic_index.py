"""Persistent, repository-isolated storage for bounded source chunks."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config.settings import Settings
from orchestrator.models.semantic_index import (
    IndexChunk,
    IndexRefreshResult,
    RepositoryIndexIdentity,
)
from orchestrator.services.repository_context import (
    is_ignored_repository_path,
    is_repository_text_file,
)

DEFAULT_MAX_CHUNK_CHARS = 4_000
DEFAULT_MAX_CHUNK_LINES = 120
INDEX_DATABASE_FILENAME = "index.sqlite3"
_SCHEMA_VERSION = "1"
_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class|function|interface|type|struct|enum)\s+"
    r"([A-Za-z_]\w*)"
)

_LANGUAGES: Mapping[str, str] = {
    ".c": "c",
    ".cfg": "config",
    ".conf": "config",
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".ini": "ini",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".mjs": "javascript",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_FILENAME_LANGUAGES: Mapping[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "rakefile": "ruby",
}


class SemanticIndexError(RuntimeError):
    """Raised when an index cannot be safely opened or refreshed."""


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    path: str
    content_hash: str
    language: str | None
    chunks: tuple[IndexChunk, ...]


def resolve_semantic_index_root(
    index_root: str | Path | None = None,
    *,
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the global index root, including ``Settings`` and its env file."""

    environment = os.environ if environ is None else environ
    configured = index_root
    if configured is None and settings is not None:
        configured = settings.semantic_index_path
    if configured is None and environ is None:
        configured = Settings().semantic_index_path
    if configured is None:
        environment_path = environment.get("ORCHESTRATOR_SEMANTIC_INDEX_PATH", "").strip()
        configured = environment_path or None
    if configured is None:
        return ((home or Path.home()).expanduser() / ".orchestrator" / "indices").resolve()
    return Path(configured).expanduser().resolve()


class RepositorySemanticIndex:
    """Maintain one durable index for one configured repository identity."""

    def __init__(
        self,
        repository_key: str,
        repository_path: str | Path,
        *,
        index_root: str | Path | None = None,
        settings: Settings | None = None,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    ) -> None:
        if max_chunk_chars <= 0 or max_chunk_lines <= 0:
            raise ValueError("chunk bounds must be positive")
        self.identity = RepositoryIndexIdentity.from_repository(
            repository_key, repository_path
        )
        self.repository_path = Path(self.identity.canonical_path)
        self.index_root = resolve_semantic_index_root(index_root, settings=settings)
        if _is_within(self.index_root, self.repository_path):
            raise ValueError("semantic index storage must be outside the repository working tree")
        self.storage_directory = self.index_root / self.identity.identifier
        self.database_path = self.storage_directory / INDEX_DATABASE_FILENAME
        self.max_chunk_chars = max_chunk_chars
        self.max_chunk_lines = max_chunk_lines

    @property
    def storage_path(self) -> Path:
        return self.database_path

    def refresh(self) -> IndexRefreshResult:
        """Atomically incorporate new, changed, and deleted source files."""

        if not self.repository_path.is_dir():
            raise SemanticIndexError(
                f"Repository path is not a directory: {self.repository_path}"
            )
        connection = self._open(require_compatible_policy=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            stored_hashes = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT path, content_hash FROM files")
            }
            reusable_hashes = stored_hashes if self._chunk_policy_matches(connection) else {}
            discovered_paths = self._discover_files()
            discovered_names = {
                path.relative_to(self.repository_path).as_posix()
                for path in discovered_paths
            }
            prepared: list[_PreparedFile] = []
            unchanged = 0
            for path in discovered_paths:
                relative_path = path.relative_to(self.repository_path).as_posix()
                raw = path.read_bytes()
                if b"\0" in raw:
                    discovered_names.discard(relative_path)
                    continue
                content_hash = hashlib.sha256(raw).hexdigest()
                if reusable_hashes.get(relative_path) == content_hash:
                    unchanged += 1
                    continue
                content = raw.decode("utf-8", errors="replace")
                language = _language_for(path)
                prepared.append(
                    _PreparedFile(
                        path=relative_path,
                        content_hash=content_hash,
                        language=language,
                        chunks=tuple(
                            self._chunk_file(
                                relative_path, content, content_hash, language
                            )
                        ),
                    )
                )

            deleted_paths = set(stored_hashes).difference(discovered_names)
            for relative_path in sorted(deleted_paths):
                connection.execute("DELETE FROM files WHERE path = ?", (relative_path,))
            indexed_chunks = 0
            for source in prepared:
                connection.execute("DELETE FROM files WHERE path = ?", (source.path,))
                connection.execute(
                    "INSERT INTO files(path, content_hash, language) VALUES (?, ?, ?)",
                    (source.path, source.content_hash, source.language),
                )
                connection.executemany(
                    """
                    INSERT INTO chunks(
                        chunk_id, repository_key, repository_identifier, path,
                        language, symbol, content_hash, text, start_line, end_line, chunk_number
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            chunk.chunk_id,
                            chunk.repository,
                            self.identity.identifier,
                            chunk.path,
                            chunk.language,
                            chunk.symbol,
                            chunk.content_hash,
                            chunk.text,
                            chunk.start_line,
                            chunk.end_line,
                            chunk_number,
                        )
                        for chunk_number, chunk in enumerate(source.chunks)
                    ),
                )
                indexed_chunks += len(source.chunks)

            self._store_chunk_policy(connection)
            total_files = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            total_chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            connection.commit()
            return IndexRefreshResult(
                indexed_files=len(prepared),
                unchanged_files=unchanged,
                deleted_files=len(deleted_paths),
                indexed_chunks=indexed_chunks,
                total_files=total_files,
                total_chunks=total_chunks,
            )
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, (SemanticIndexError, ValueError)):
                raise
            raise SemanticIndexError(
                f"Could not refresh semantic index for {self.identity.repository_key!r}: {exc}"
            ) from exc
        finally:
            connection.close()

    def index_repository(self) -> IndexRefreshResult:
        """Alias making the explicit indexing operation clear at call sites."""

        return self.refresh()

    def chunks(self) -> list[IndexChunk]:
        """Load all chunks in this repository namespace."""

        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT chunk_id, repository_key, repository_identifier, path,
                       language, symbol, content_hash, text, start_line, end_line
                FROM chunks
                WHERE repository_key = ? AND repository_identifier = ?
                ORDER BY path, chunk_number
                """,
                (self.identity.repository_key, self.identity.identifier),
            ).fetchall()
            return [self._row_to_chunk(row) for row in rows]
        finally:
            connection.close()

    def get_chunks(self) -> list[IndexChunk]:
        return self.chunks()

    def retrieve(self, query: str, *, limit: int = 10) -> list[IndexChunk]:
        """Return isolated lexical candidates from this repository's chunk store."""

        if limit <= 0:
            return []
        tokens = tuple(dict.fromkeys(token.casefold() for token in _TOKEN_PATTERN.findall(query)))
        if not tokens:
            return []
        scored: list[tuple[int, IndexChunk]] = []
        for chunk in self.chunks():
            text = chunk.text.casefold()
            path = chunk.path.casefold()
            symbol = (chunk.symbol or "").casefold()
            score = sum(
                text.count(token) + (5 if token in path else 0) + (10 if token in symbol else 0)
                for token in tokens
            )
            if score:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))
        return [chunk for _, chunk in scored[:limit]]

    def search(self, query: str, *, limit: int = 10) -> list[IndexChunk]:
        return self.retrieve(query, limit=limit)

    def _open(self, *, require_compatible_policy: bool = True) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self.storage_directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.database_path,
                timeout=30,
                isolation_level=None,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            if require_compatible_policy and not self._chunk_policy_matches(connection):
                raise SemanticIndexError(
                    "Semantic index chunk bounds changed; refresh the index before retrieval"
                )
            connection.commit()
            return connection
        except Exception as exc:
            if connection is not None:
                connection.rollback()
                connection.close()
            if isinstance(exc, SemanticIndexError):
                raise
            if not isinstance(exc, (OSError, sqlite3.Error)):
                raise
            raise SemanticIndexError(
                f"Could not open semantic index {self.database_path}: {exc}"
            ) from exc

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                language TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                repository_key TEXT NOT NULL,
                repository_identifier TEXT NOT NULL,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                language TEXT,
                symbol TEXT,
                content_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                chunk_number INTEGER NOT NULL,
                UNIQUE(path, chunk_number)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path)",
        )
        for statement in statements:
            connection.execute(statement)
        expected = {
            "schema_version": _SCHEMA_VERSION,
            "repository_key": self.identity.repository_key,
            "canonical_path": self.identity.canonical_path,
            "repository_identifier": self.identity.identifier,
        }
        stored = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        if stored:
            mismatches = {
                key: (stored.get(key), value)
                for key, value in expected.items()
                if key in stored and stored[key] != value
            }
            if mismatches:
                raise SemanticIndexError(
                    f"Index identity or schema mismatch at {self.database_path}: {mismatches}"
                )
            missing = {key: value for key, value in expected.items() if key not in stored}
            if missing:
                has_index_data = any(
                    int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in ("files", "chunks")
                )
                if has_index_data:
                    raise SemanticIndexError(
                        f"Index identity metadata is incomplete at {self.database_path}"
                    )
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", missing.items()
                )
        else:
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (*expected.items(), *self._chunk_policy().items()),
            )
        stored = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        if any(stored.get(key) != value for key, value in expected.items()):
            raise SemanticIndexError(
                f"Index identity changed while opening {self.database_path}"
            )

    def _chunk_policy(self) -> dict[str, str]:
        return {
            "max_chunk_chars": str(self.max_chunk_chars),
            "max_chunk_lines": str(self.max_chunk_lines),
        }

    def _chunk_policy_matches(self, connection: sqlite3.Connection) -> bool:
        stored = dict(
            connection.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?)",
                ("max_chunk_chars", "max_chunk_lines"),
            ).fetchall()
        )
        return stored == self._chunk_policy()

    def _store_chunk_policy(self, connection: sqlite3.Connection) -> None:
        for key, value in self._chunk_policy().items():
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _discover_files(self) -> list[Path]:
        files: list[Path] = []
        pending = [self.repository_path]
        while pending:
            directory = pending.pop()
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name.casefold())
            directories: list[Path] = []
            for entry in entries:
                if is_ignored_repository_path(entry, self.repository_path):
                    continue
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    directories.append(entry)
                elif entry.is_file() and is_repository_text_file(entry):
                    files.append(entry)
            pending.extend(reversed(directories))
        return sorted(files, key=lambda path: path.relative_to(self.repository_path).as_posix())

    def _chunk_file(
        self,
        relative_path: str,
        content: str,
        content_hash: str,
        language: str | None,
    ) -> Iterable[IndexChunk]:
        symbols = _extract_symbols(content, language)
        lines = content.splitlines(keepends=True)
        current: list[str] = []
        current_chars = 0
        start_line = 1
        current_line_count = 0

        def emit(end_line: int) -> IndexChunk | None:
            if not current:
                return None
            text = "".join(current)
            symbol = _symbol_for_range(symbols, start_line, end_line)
            chunk_number_seed = f"{relative_path}\0{content_hash}\0{start_line}\0{end_line}\0{text}"
            chunk_id = hashlib.sha256(chunk_number_seed.encode("utf-8")).hexdigest()
            return IndexChunk(
                repository=self.identity.repository_key,
                path=relative_path,
                language=language,
                symbol=symbol,
                content_hash=content_hash,
                text=text,
                start_line=start_line,
                end_line=end_line,
                chunk_id=chunk_id,
            )

        for line_number, line in enumerate(lines, start=1):
            segments = [
                line[offset : offset + self.max_chunk_chars]
                for offset in range(0, len(line), self.max_chunk_chars)
            ] or [""]
            for segment in segments:
                exceeds_bounds = current and (
                    current_chars + len(segment) > self.max_chunk_chars
                    or current_line_count >= self.max_chunk_lines
                )
                if exceeds_bounds:
                    chunk = emit(line_number if current_line_count == 0 else line_number - 1)
                    if chunk is not None:
                        yield chunk
                    current = []
                    current_chars = 0
                    current_line_count = 0
                    start_line = line_number
                if not current:
                    start_line = line_number
                current.append(segment)
                current_chars += len(segment)
                # Multiple segments of one long physical line share its location.
                if current_line_count == 0 or start_line != line_number:
                    current_line_count += 1
                if current_chars >= self.max_chunk_chars:
                    chunk = emit(line_number)
                    if chunk is not None:
                        yield chunk
                    current = []
                    current_chars = 0
                    current_line_count = 0
                    start_line = line_number + 1
        if current:
            chunk = emit(len(lines))
            if chunk is not None:
                yield chunk

    def _row_to_chunk(self, row: sqlite3.Row | tuple[object, ...]) -> IndexChunk:
        repository_key = str(row[1])
        repository_identifier = str(row[2])
        if (
            repository_key != self.identity.repository_key
            or repository_identifier != self.identity.identifier
        ):
            raise SemanticIndexError("Repository isolation violation in semantic index")
        return IndexChunk(
            chunk_id=str(row[0]),
            repository=repository_key,
            path=str(row[3]),
            language=str(row[4]) if row[4] is not None else None,
            symbol=str(row[5]) if row[5] is not None else None,
            content_hash=str(row[6]),
            text=str(row[7]),
            start_line=int(str(row[8])),
            end_line=int(str(row[9])),
        )


class SemanticIndexManager:
    """Small facade for callers managing multiple configured repositories."""

    def __init__(
        self,
        index_root: str | Path | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.index_root = resolve_semantic_index_root(index_root, settings=settings)

    def for_repository(
        self, repository_key: str, repository_path: str | Path
    ) -> RepositorySemanticIndex:
        return RepositorySemanticIndex(
            repository_key, repository_path, index_root=self.index_root
        )

    def index_repository(
        self, repository_key: str, repository_path: str | Path
    ) -> IndexRefreshResult:
        return self.for_repository(repository_key, repository_path).refresh()

    def chunks(self, repository_key: str, repository_path: str | Path) -> list[IndexChunk]:
        return self.for_repository(repository_key, repository_path).chunks()

    def get_chunks(
        self, repository_key: str, repository_path: str | Path
    ) -> list[IndexChunk]:
        return self.chunks(repository_key, repository_path)

    def retrieve(
        self,
        repository_key: str,
        repository_path: str | Path,
        query: str,
        *,
        limit: int = 10,
    ) -> list[IndexChunk]:
        return self.for_repository(repository_key, repository_path).retrieve(query, limit=limit)

    def search(
        self,
        repository_key: str,
        repository_path: str | Path,
        query: str,
        *,
        limit: int = 10,
    ) -> list[IndexChunk]:
        return self.retrieve(repository_key, repository_path, query, limit=limit)


SemanticIndexService = SemanticIndexManager
SemanticIndex = RepositorySemanticIndex


def _is_within(path: Path, possible_parent: Path) -> bool:
    try:
        path.relative_to(possible_parent)
        return True
    except ValueError:
        return False


def _language_for(path: Path) -> str | None:
    return _FILENAME_LANGUAGES.get(path.name.casefold()) or _LANGUAGES.get(
        path.suffix.casefold()
    )


def _extract_symbols(content: str, language: str | None) -> list[tuple[int, int, str]]:
    if language == "python":
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            pass
        else:
            nodes = (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            )
            return sorted(
                (
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                    node.name,
                )
                for node in nodes
            )
    symbols: list[tuple[int, int, str]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = _SYMBOL_PATTERN.match(line)
        if match:
            symbols.append((line_number, line_number, match.group(1)))
    return symbols


def _symbol_for_range(
    symbols: list[tuple[int, int, str]], start_line: int, end_line: int
) -> str | None:
    enclosing = [symbol for symbol in symbols if symbol[0] <= start_line <= symbol[1]]
    if enclosing:
        return max(enclosing, key=lambda symbol: symbol[0])[2]
    declared = [symbol for symbol in symbols if start_line <= symbol[0] <= end_line]
    return declared[0][2] if declared else None
