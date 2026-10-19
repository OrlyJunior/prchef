"""Resolve and render provider-neutral repository context strategies."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from prchef.models.development_task import DevelopmentTask

if TYPE_CHECKING:
    from prchef.config.project_config import RepositoryContextSettings
    from prchef.services.embeddings import EmbeddingProvider

MAX_CONTEXT_CHARS = 12_000
MAX_TRAVERSAL_DEPTH = 4
MAX_DISCOVERED_FILES = 400
MAX_TREE_ENTRIES = 180
MAX_TREE_CHARS = 3_500
MAX_CANDIDATE_FILES = 8
MAX_FILE_BYTES = 48_000
MAX_SNIPPET_CHARS = 800
_ROLE_INSTRUCTION_FILES = frozenset({"planner.md", "implementer.md", "reviewer.md"})

IGNORED_DIRECTORY_NAMES = frozenset(
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
        ".cfg",
        ".conf",
        ".css",
        ".go",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_FILENAMES = frozenset({"dockerfile", "makefile", "rakefile"})
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "from",
        "have",
        "into",
        "must",
        "should",
        "that",
        "the",
        "their",
        "these",
        "this",
        "with",
        "would",
    }
)
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class|function|interface|type|struct|enum)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    relative_path: str
    content: str
    symbols: tuple[str, ...]
    matched_keywords: tuple[str, ...]
    score: int


class RepositoryContextCollector:
    """Build bounded repository context without changing the checkout."""

    def __init__(self, repository_path: Path) -> None:
        self._repository_path = repository_path

    def collect(self, task: DevelopmentTask) -> str:
        """Return best-effort prompt context, or an empty string on filesystem failure."""
        try:
            files = self._discover_files()
            if not files:
                return ""
            keywords = _task_keywords(task)
            tree = self._tree_overview(files)
            candidates = self._find_candidates(files, keywords)
            return _cap_context(_render_context(tree, candidates))
        except (OSError, UnicodeError, ValueError):
            return ""

    def _discover_files(self) -> list[Path]:
        root = self._repository_path
        try:
            if not root.is_dir():
                return []
        except OSError:
            return []

        files: list[Path] = []
        pending: list[tuple[Path, int]] = [(root, 0)]
        while pending and len(files) < MAX_DISCOVERED_FILES:
            directory, depth = pending.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda entry: entry.name.lower())
            except OSError:
                continue
            for entry in entries:
                if entry.name in IGNORED_DIRECTORY_NAMES:
                    continue
                if depth == 0 and entry.name.casefold() in _ROLE_INSTRUCTION_FILES:
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        if depth < MAX_TRAVERSAL_DEPTH:
                            pending.append((entry, depth + 1))
                    elif entry.is_file():
                        files.append(entry)
                        if len(files) >= MAX_DISCOVERED_FILES:
                            break
                except OSError:
                    continue
        return files

    def _tree_overview(self, files: list[Path]) -> list[str]:
        entries: list[str] = []
        seen: set[tuple[str, ...]] = set()
        character_count = 0
        for path in files:
            try:
                relative_parts = path.relative_to(self._repository_path).parts
            except ValueError:
                continue
            for index, part in enumerate(relative_parts):
                prefix = relative_parts[: index + 1]
                if prefix in seen:
                    continue
                seen.add(prefix)
                suffix = "/" if index < len(relative_parts) - 1 else ""
                entry = f"{'  ' * index}{part}{suffix}"
                if len(entries) >= MAX_TREE_ENTRIES or (
                    character_count + len(entry) > MAX_TREE_CHARS
                ):
                    return entries
                entries.append(entry)
                character_count += len(entry)
        return entries

    def _find_candidates(self, files: list[Path], keywords: tuple[str, ...]) -> list[_Candidate]:
        if not keywords:
            return []
        candidates: list[_Candidate] = []
        for path in files:
            if not _is_text_file(path):
                continue
            try:
                relative_path = path.relative_to(self._repository_path).as_posix()
                content = _read_text(path)
            except (OSError, UnicodeError, ValueError):
                continue
            if content is None:
                continue
            path_lower = relative_path.lower()
            content_lower = content.lower()
            symbols = tuple(match.group(1) for match in _SYMBOL_PATTERN.finditer(content))
            searchable_symbols = " ".join(symbols).lower()
            matched = tuple(
                keyword
                for keyword in keywords
                if (
                    keyword in path_lower
                    or keyword in searchable_symbols
                    or keyword in content_lower
                )
            )
            if not matched:
                continue
            path_hits = sum(keyword in path_lower for keyword in matched)
            symbol_hits = sum(keyword in searchable_symbols for keyword in matched)
            content_hits = sum(content_lower.count(keyword) for keyword in matched)
            candidates.append(
                _Candidate(
                    path=path,
                    relative_path=relative_path,
                    content=content,
                    symbols=symbols,
                    matched_keywords=matched,
                    score=path_hits * 100 + symbol_hits * 20 + min(content_hits, 20),
                )
            )
        ranked = sorted(
            candidates, key=lambda candidate: (-candidate.score, candidate.relative_path)
        )
        return ranked[:MAX_CANDIDATE_FILES]


@dataclass(frozen=True, slots=True)
class RepositoryContextRequest:
    """Role-aware inputs available to repository context strategies."""

    role: Literal["planner", "reviewer"]
    repository_path: Path
    task: DevelopmentTask
    repository_slug: str
    base_branch: str
    feature_branch: str | None = None
    temporary_source: bool = False


class RepositoryContextStrategy(Protocol):
    """Orchestrator boundary for role-specific repository context."""

    def render(self, request: RepositoryContextRequest) -> str:
        """Render the repository context block for one agent request."""
        ...


class RepositoryIndexLifecycle(Protocol):
    """Optional management boundary implemented by index-backed strategies."""

    def inspect_index(self) -> Any:
        """Return the current index lifecycle status."""
        ...

    def build_index(self) -> Any:
        """Build a fresh index for the configured repository root."""
        ...

    def refresh_changed_files(self) -> Any:
        """Synchronize changed and deleted files without a full rebuild."""
        ...

    def clear_index(self) -> Any:
        """Drop the configured repository's index."""
        ...


class RepositoryContextStrategyError(ValueError):
    """Raised when a configured repository context strategy cannot be resolved."""


def semantic_fallback_diagnostic(exc: Exception) -> dict[str, str]:
    """Classify backend failures without exposing exception text or local paths."""

    causes: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(causes) < 4:
        causes.append(current)
        current = current.__cause__
    detail = " ".join(f"{type(item).__name__} {item}" for item in causes).casefold()
    if "no module" in detail or "is required for qdrant" in detail:
        reason = "The local semantic dependencies are unavailable."
        remedy = "Reinstall the orchestrator with its default dependencies."
    elif any(word in detail for word in ("unsupported", "platform", "onnxruntime", "architecture")):
        reason = "The local embedding runtime is unsupported on this platform."
        remedy = "Use a supported Python platform or select strategy: heuristic."
    elif any(
        word in detail for word in ("offline", "download", "huggingface", "connection", "network")
    ):
        reason = "The embedding model download or semantic connection is unavailable."
        remedy = "Retry online after the model is cached, or select strategy: heuristic."
    elif "lock" in detail:
        reason = "The local Qdrant index is locked by another process."
        remedy = "Close the other process or configure Qdrant server mode."
    else:
        reason = f"Semantic setup or retrieval failed ({type(exc).__name__})."
        remedy = (
            "Check the Qdrant index and embedding settings, then retry or select "
            "strategy: heuristic."
        )
    return {"kind": "REPOSITORY_CONTEXT_FALLBACK", "reason": reason, "remedy": remedy}


class HeuristicRepositoryContextStrategy:
    """Compatibility strategy backed by the existing bounded collector."""

    def render(self, request: RepositoryContextRequest) -> str:
        if request.role == "planner":
            return repository_context_prompt(request.repository_path, request.task)
        return "REPOSITORY CONTEXT:\n" + json.dumps(
            {
                "repository": request.repository_slug,
                "base_branch": request.base_branch,
                "feature_branch": request.feature_branch,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n\n"


RepositoryContextStrategyBuilder = Callable[..., RepositoryContextStrategy]


def _build_qdrant_strategy(**kwargs: Any) -> RepositoryContextStrategy:
    from prchef.services.qdrant_repository_context import (
        QdrantRepositoryContextStrategy,
    )

    return QdrantRepositoryContextStrategy(**kwargs)


_STRATEGY_BUILDERS: dict[str, RepositoryContextStrategyBuilder] = {
    "heuristic": HeuristicRepositoryContextStrategy,
    "qdrant": _build_qdrant_strategy,
}


def repository_context_strategy_names() -> tuple[str, ...]:
    """Return the stable names accepted by configuration and resolution."""

    return tuple(sorted(_STRATEGY_BUILDERS))


def resolve_repository_context_strategy(
    name: str,
    *,
    settings: RepositoryContextSettings | None = None,
    repository_key: str | None = None,
    configured_root: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    client_factory: Callable[[Any], Any] | None = None,
    client_manager: Any | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_diagnostic: Callable[[dict[str, str]], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> RepositoryContextStrategy:
    """Resolve a stable strategy name through the orchestrator-owned registry.

    ``client_manager`` lets the composing process share one long-lived index
    client across concurrent executions; index-free strategies ignore it.
    """

    normalized = name.strip().casefold()
    try:
        builder = _STRATEGY_BUILDERS[normalized]
    except KeyError as exc:
        supported = ", ".join(repository_context_strategy_names())
        raise RepositoryContextStrategyError(
            f"Unknown repository context strategy {name!r}; supported strategies: {supported}"
        ) from exc
    if normalized == "heuristic":
        return builder()
    if settings is None or configured_root is None:
        raise RepositoryContextStrategyError(
            "Qdrant repository context requires resolved settings and the configured "
            "repository root"
        )
    if repository_key is None:
        if on_diagnostic is not None:
            on_diagnostic(
                {
                    "kind": "REPOSITORY_CONTEXT_FALLBACK",
                    "reason": "This repository was selected by path without a registered key.",
                    "remedy": (
                        "Register the repository in orchestrator.yml and select its key "
                        "to use semantic retrieval."
                    ),
                }
            )
        return HeuristicRepositoryContextStrategy()
    kwargs: dict[str, Any] = {
        "settings": settings,
        "repository_key": repository_key,
        "configured_root": configured_root,
    }
    if embedding_provider is not None:
        kwargs["embedding_provider"] = embedding_provider
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    if client_manager is not None:
        kwargs["client_manager"] = client_manager
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    if on_diagnostic is not None:
        kwargs["on_diagnostic"] = on_diagnostic
    if cancellation_check is not None:
        kwargs["cancellation_check"] = cancellation_check
    try:
        return builder(**kwargs)
    except Exception as exc:
        if on_diagnostic is not None:
            on_diagnostic(semantic_fallback_diagnostic(exc))
        return HeuristicRepositoryContextStrategy()


def collect_repository_context(repository_path: Path, task: DevelopmentTask) -> str:
    """Collect provider-neutral, local repository context for a task."""
    return RepositoryContextCollector(repository_path).collect(task)


def repository_context_prompt(repository_path: Path, task: DevelopmentTask) -> str:
    """Render local repository context for inclusion in a planner prompt."""
    return collect_repository_context(repository_path, task)


def _task_keywords(task: DevelopmentTask) -> tuple[str, ...]:
    keywords: list[str] = []
    for token in _TOKEN_PATTERN.findall(f"{task.title} {task.description}"):
        keyword = token.lower()
        if len(keyword) > 80 or keyword in _STOP_WORDS or keyword in keywords:
            continue
        keywords.append(keyword)
        if len(keywords) == 24:
            break
    return tuple(keywords)


def _is_text_file(path: Path) -> bool:
    return path.name.lower() in _TEXT_FILENAMES or path.suffix.lower() in _TEXT_SUFFIXES


def _read_text(path: Path) -> str | None:
    with path.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if b"\0" in data:
        return None
    return data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")


def _render_context(tree: list[str], candidates: list[_Candidate]) -> str:
    tree_lines = "\n".join(f"- `{path}`" for path in tree)
    parts = [
        "LOCAL REPOSITORY CONTEXT (read-only, bounded local snapshot):",
        (
            "The checked-out repository is authoritative; use this context to resolve details "
            "when possible."
        ),
        "PROJECT STRUCTURE:",
        tree_lines,
    ]
    if candidates:
        parts.extend(
            [
                "LIKELY RELEVANT FILES:",
                *(
                    (
                        f"- `{candidate.relative_path}` "
                        f"(matched: {', '.join(candidate.matched_keywords)})"
                    )
                    for candidate in candidates
                ),
                "RELEVANT CODE EXCERPTS:",
            ]
        )
        parts.extend(_render_candidate(candidate) for candidate in candidates)
    return "\n\n".join(parts) + "\n\n"


def _render_candidate(candidate: _Candidate) -> str:
    symbol_text = ", ".join(candidate.symbols[:8]) or "no recognized symbols"
    excerpt = _snippet(candidate.content, candidate.matched_keywords)
    return f"FILE: `{candidate.relative_path}`\nSYMBOLS: {symbol_text}\n```\n{excerpt}\n```"


def _snippet(content: str, keywords: tuple[str, ...]) -> str:
    lowered = content.lower()
    match_positions = [lowered.find(keyword) for keyword in keywords]
    positions = [position for position in match_positions if position >= 0]
    start = max(0, min(positions) - MAX_SNIPPET_CHARS // 3) if positions else 0
    end = min(len(content), start + MAX_SNIPPET_CHARS)
    excerpt = content[start:end].strip()
    if start:
        excerpt = f"...\n{excerpt}"
    if end < len(content):
        excerpt = f"{excerpt}\n..."
    return excerpt


def _cap_context(context: str) -> str:
    if len(context) <= MAX_CONTEXT_CHARS:
        return context
    notice = "\n[Repository context truncated to preserve prompt budget.]\n\n"
    return f"{context[: MAX_CONTEXT_CHARS - len(notice)]}{notice}"
