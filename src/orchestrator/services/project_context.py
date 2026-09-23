"""Load compact, durable repository context for agent prompts."""

import os
import sys
from collections.abc import Callable
from io import TextIOBase
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
        with _open_instruction_file(path) as instruction_file:
            content = instruction_file.read().strip()
    except (OSError, UnicodeError):
        return None
    return content or None


def _open_instruction_file(path: Path) -> TextIOBase:
    """Open the role file itself, refusing symlinks even if the path changes."""
    if sys.platform == "win32":
        return _open_windows_instruction_file(path)

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("Opening role files without following symlinks is unsupported")
    descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        return os.fdopen(descriptor, "r", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_instruction_file(path: Path) -> TextIOBase:
    """Open a Windows role file with reparse-point following disabled."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    file_attribute_reparse_point = 0x400
    file_flag_open_reparse_point = 0x00200000
    file_attribute_tag_info = 9

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    file_info = kernel32.GetFileInformationByHandleEx
    file_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    file_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # OPEN_REPARSE_POINT makes the handle refer to the link, not its target.
    handle = create_file(str(path), 0x80000000, 0x7, None, 3, file_flag_open_reparse_point, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = FileAttributeTagInfo()
        if not file_info(
            handle, file_attribute_tag_info, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.FileAttributes & file_attribute_reparse_point:
            raise OSError("Role file is a reparse point")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        return os.fdopen(descriptor, "r", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


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
