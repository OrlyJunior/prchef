"""Load compact, durable repository context for agent prompts."""

from pathlib import Path

PROJECT_CONTEXT_FILE = "PROJECT.md"
MAX_PROJECT_CONTEXT_CHARS = 12_000


def load_project_context(repository_path: Path) -> str | None:
    """Return root PROJECT.md content when available, otherwise no context."""
    path = repository_path / PROJECT_CONTEXT_FILE
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def project_context_prompt(repository_path: Path) -> str:
    """Render repository context without allowing it to dominate an agent prompt."""
    content = load_project_context(repository_path)
    if content is None:
        return ""
    if len(content) > MAX_PROJECT_CONTEXT_CHARS:
        content = (
            f"{content[:MAX_PROJECT_CONTEXT_CHARS]}\n\n"
            "[PROJECT.md truncated; inspect the repository file for the full context.]"
        )
    return (
        "DURABLE PROJECT CONTEXT (PROJECT.md):\n"
        "This is supporting context; the current repository remains authoritative.\n"
        f"{content}\n\n"
    )
