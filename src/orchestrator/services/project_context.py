"""Load compact, durable repository context for agent prompts."""

from collections.abc import Callable
from pathlib import Path

PROJECT_CONTEXT_FILE = "docs/PROJECT_CONTEXT.md"
MAX_PROJECT_CONTEXT_CHARS = 12_000
PLANNER_INSTRUCTIONS_FILE = "PLANNER.md"
IMPLEMENTER_INSTRUCTIONS_FILE = "IMPLEMENTER.md"
REVIEWER_INSTRUCTIONS_FILE = "REVIEWER.md"
MAX_AGENT_INSTRUCTIONS_CHARS = 12_000


def load_project_context(repository_path: Path) -> str | None:
    """Return docs/PROJECT_CONTEXT.md content when available, otherwise no context."""
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
            "[docs/PROJECT_CONTEXT.md truncated; inspect the repository file for the full context.]"
        )
    return (
        "DURABLE PROJECT CONTEXT (docs/PROJECT_CONTEXT.md):\n"
        "This is supporting context; the current repository remains authoritative.\n"
        f"{content}\n\n"
    )


def load_planner_instructions(repository_path: Path) -> str | None:
    """Return root PLANNER.md content when available."""
    return _load_instruction_file(repository_path, PLANNER_INSTRUCTIONS_FILE)


def load_implementer_instructions(repository_path: Path) -> str | None:
    """Return root IMPLEMENTER.md content when available."""
    return _load_instruction_file(repository_path, IMPLEMENTER_INSTRUCTIONS_FILE)


def load_reviewer_instructions(repository_path: Path) -> str | None:
    """Return root REVIEWER.md content when available."""
    return _load_instruction_file(repository_path, REVIEWER_INSTRUCTIONS_FILE)


def planner_instructions_prompt(repository_path: Path) -> str:
    """Render local planner guidance from the configured checkout."""
    return _agent_instructions_prompt(
        repository_path,
        None,
        PLANNER_INSTRUCTIONS_FILE,
        load_planner_instructions,
        "planner",
    )


def implementer_instructions_prompt(
    repository_path: Path, fallback_repository_path: Path | None = None
) -> str:
    """Render local implementer guidance, isolated from other agent roles."""
    return _agent_instructions_prompt(
        repository_path,
        fallback_repository_path,
        IMPLEMENTER_INSTRUCTIONS_FILE,
        load_implementer_instructions,
        "implementer",
    )


def reviewer_instructions_prompt(
    repository_path: Path, fallback_repository_path: Path | None = None
) -> str:
    """Render local reviewer guidance, isolated from other agent roles."""
    return _agent_instructions_prompt(
        repository_path,
        fallback_repository_path,
        REVIEWER_INSTRUCTIONS_FILE,
        load_reviewer_instructions,
        "reviewer",
    )


def _load_instruction_file(repository_path: Path, filename: str) -> str | None:
    path = repository_path / filename
    try:
        # A role file is local guidance, never an alias to an arbitrary file on the host.
        if path.is_symlink():
            return None
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return content or None


def _agent_instructions_prompt(
    repository_path: Path,
    fallback_repository_path: Path | None,
    filename: str,
    loader: Callable[[Path], str | None],
    role: str,
) -> str:
    """Render one role's additional guidance without allowing duplicate context."""
    content = loader(repository_path)
    # Ignored local files are unavailable in a newly-created worktree. If the control checkout
    # does not have the file, still support a versioned file present in the workspace.
    if content is None and (
        not (repository_path / filename).exists()
        and fallback_repository_path is not None
        and fallback_repository_path != repository_path
    ):
        content = loader(fallback_repository_path)
    if content is None:
        return ""
    if len(content) > MAX_AGENT_INSTRUCTIONS_CHARS:
        content = (
            f"{content[:MAX_AGENT_INSTRUCTIONS_CHARS]}\n\n"
            f"[{filename} truncated; inspect the repository file for the full instructions.]"
        )
    return (
        f"REPOSITORY {role.upper()} INSTRUCTIONS ({filename}):\n"
        "These are additional role-specific guidance. The original task, repository state, "
        "and core orchestrator invariants remain authoritative.\n"
        f"{content}\n\n"
    )
