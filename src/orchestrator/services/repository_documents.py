"""Read-only repository source discovery and deterministic bounded chunking."""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

from orchestrator.models.repository_context import RepositoryChunk, RepositorySourceFile

MAX_INDEXED_FILES = 2_000
MAX_INDEXED_FILE_BYTES = 512_000
MAX_FALLBACK_DEPTH = 12
_PRIVATE_PATHS = frozenset(
    {"planner.md", "implementer.md", "reviewer.md", "docs/project_context.md"}
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "coverage",
        "target",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_FILENAMES = frozenset({"dockerfile", "makefile", "rakefile"})
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")
_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:(?:async\s+)?def|class|function|interface|type|struct|enum|"
    r"(?:pub\s+)?fn)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".md": "markdown",
    ".mjs": "javascript",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


class RepositorySourcePipeline:
    def __init__(self, repository: str, root: Path) -> None:
        self._repository = repository
        self._root = root.resolve()

    def discover(self) -> list[RepositorySourceFile]:
        if not self._root.is_dir():
            raise OSError("Repository source root is unavailable")
        files: list[RepositorySourceFile] = []
        for relative_path in self._discover_relative_paths():
            if len(files) >= MAX_INDEXED_FILES:
                break
            if relative_path.is_absolute() or ".." in relative_path.parts:
                continue
            normalized = relative_path.as_posix()
            if not self._eligible(relative_path, normalized):
                continue
            path = self._root / relative_path
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved_path = path.resolve()
                resolved_path.relative_to(self._root)
            except ValueError:
                continue
            except OSError as exc:
                raise OSError("Repository source discovery failed") from exc
            try:
                data = resolved_path.read_bytes()
            except OSError as exc:
                raise OSError("Repository source read failed") from exc
            if len(data) > MAX_INDEXED_FILE_BYTES or b"\0" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            files.append(
                RepositorySourceFile(
                    repository=self._repository,
                    path=normalized,
                    language=language_for_path(relative_path),
                    content_hash=hashlib.sha256(data).hexdigest(),
                    text=text,
                )
            )
        return files

    def _discover_relative_paths(self) -> list[Path]:
        if not self._is_git_root():
            return self._fallback_relative_paths()
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            decoded = result.stdout.decode("utf-8", errors="surrogateescape")
            return sorted(
                (Path(item) for item in decoded.split("\0") if item),
                key=lambda path: (path.as_posix().casefold(), path.as_posix()),
            )

        return self._fallback_relative_paths()

    def _is_git_root(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(self._root), "rev-parse", "--show-toplevel"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False
        return Path(result.stdout.strip()).resolve() == self._root

    def _fallback_relative_paths(self) -> list[Path]:
        discovered: list[Path] = []
        pending = [(self._root, 0)]
        while pending and len(discovered) < MAX_INDEXED_FILES:
            directory, depth = pending.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                if directory == self._root:
                    raise OSError("Repository source discovery failed") from exc
                continue
            for entry in entries:
                if entry.name.casefold() in _IGNORED_DIRECTORIES or entry.is_symlink():
                    continue
                if entry.is_dir():
                    if depth < MAX_FALLBACK_DEPTH:
                        pending.append((entry, depth + 1))
                elif entry.is_file():
                    discovered.append(entry.relative_to(self._root))
                    if len(discovered) >= MAX_INDEXED_FILES:
                        break
        return sorted(discovered, key=lambda path: (path.as_posix().casefold(), path.as_posix()))

    @staticmethod
    def _eligible(path: Path, normalized: str) -> bool:
        lowered = normalized.casefold()
        if lowered in _PRIVATE_PATHS:
            return False
        if any(part.casefold() in _IGNORED_DIRECTORIES for part in path.parts[:-1]):
            return False
        if lowered.endswith(_GENERATED_SUFFIXES):
            return False
        return path.name.casefold() in _TEXT_FILENAMES or path.suffix.casefold() in _TEXT_SUFFIXES


def language_for_path(path: Path) -> str:
    if path.name.casefold() == "dockerfile":
        return "dockerfile"
    if path.name.casefold() in {"makefile", "rakefile"}:
        return path.name.casefold()
    return _LANGUAGES.get(path.suffix.casefold(), "text")


def chunk_source_file(
    source: RepositorySourceFile,
    *,
    source_root_id: str,
    chunk_size_chars: int,
    overlap_chars: int,
) -> list[RepositoryChunk]:
    if not source.text:
        return []
    units: list[tuple[int, str]] = []
    for line_number, line in enumerate(source.text.splitlines(keepends=True), start=1):
        if not line:
            units.append((line_number, line))
            continue
        for offset in range(0, len(line), chunk_size_chars):
            units.append((line_number, line[offset : offset + chunk_size_chars]))
    if not units:
        units.append((1, source.text))

    symbols = _symbols_by_line(source.text)
    chunks: list[RepositoryChunk] = []
    start = 0
    while start < len(units):
        end = start
        size = 0
        while end < len(units):
            unit_size = len(units[end][1])
            if end > start and size + unit_size > chunk_size_chars:
                break
            size += unit_size
            end += 1
            if size >= chunk_size_chars:
                break
        text = "".join(value for _, value in units[start:end]).rstrip()
        if text:
            start_line = units[start][0]
            end_line = units[end - 1][0]
            ordinal = len(chunks)
            identity = "\0".join(
                (
                    source.repository,
                    source_root_id,
                    source.path,
                    source.content_hash,
                    str(ordinal),
                )
            )
            chunks.append(
                RepositoryChunk(
                    repository=source.repository,
                    source_root_id=source_root_id,
                    path=source.path,
                    language=source.language,
                    symbol=_nearest_symbol(symbols, start_line),
                    content_hash=source.content_hash,
                    start_line=start_line,
                    end_line=end_line,
                    text=text,
                    ordinal=ordinal,
                    point_id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                )
            )
        if end >= len(units):
            break
        overlap = 0
        next_start = end
        while next_start > start + 1:
            candidate_size = len(units[next_start - 1][1])
            if overlap + candidate_size > overlap_chars:
                break
            next_start -= 1
            overlap += candidate_size
        start = max(start + 1, next_start)
    return chunks


def _symbols_by_line(text: str) -> list[tuple[int, str]]:
    symbols: list[tuple[int, str]] = []
    for match in _SYMBOL_PATTERN.finditer(text):
        symbols.append((text.count("\n", 0, match.start()) + 1, match.group(1)))
    return symbols


def _nearest_symbol(symbols: list[tuple[int, str]], line: int) -> str | None:
    nearest: str | None = None
    for symbol_line, symbol in symbols:
        if symbol_line > line:
            break
        nearest = symbol
    return nearest
