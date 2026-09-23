from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator.clients.exceptions import CommandExecutionError, CommandTimeoutError
from orchestrator.runtime_logging import format_command

logger = logging.getLogger(__name__)

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")


def _is_windows() -> bool:
    return os.name == "nt"


def _partial_output(value: bytes | str | None) -> str:
    """Decode output captured before a timeout; it may be raw bytes even in text mode."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0


class CommandClient:
    """Small subprocess boundary shared by local CLI clients."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout_seconds: int = 900,
        env: Mapping[str, str] | None = None,
        on_progress: Callable[[float], None] | None = None,
        heartbeat_interval_seconds: float = 60.0,
    ) -> CommandResult:
        resolved_command = self._resolve_command(command, cwd=cwd, env=env)
        started = time.monotonic()
        resolved_executable = resolved_command[0] if resolved_command else "<none>"
        logger.debug("[command] Running: %s", format_command(command))
        logger.debug(
            "[command] Resolved executable: %s",
            format_command([resolved_executable]),
        )
        try:
            effective_cwd = cwd.resolve()
        except (OSError, RuntimeError):
            effective_cwd = cwd
        logger.debug("[command] Working directory: %s", effective_cwd)
        logger.debug("[command] Arguments: %s", format_command(resolved_command[1:]))
        stop_heartbeat = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        if on_progress is not None and heartbeat_interval_seconds > 0:
            def heartbeat() -> None:
                while not stop_heartbeat.wait(heartbeat_interval_seconds):
                    try:
                        on_progress(time.monotonic() - started)
                    except Exception:
                        logger.debug("[command] Progress callback failed", exc_info=True)

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
        try:
            completed = subprocess.run(
                resolved_command,
                cwd=cwd,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=dict(env) if env is not None else None,
                shell=False,
            )
        except FileNotFoundError as exc:
            details = self._os_error_details(exc)
            logger.error(
                "[command] Executable not found: %s (%s)", command[0], details
            )
            raise CommandExecutionError(f"Executable not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(
                f"Command timed out after {timeout_seconds}s: {format_command(command)}",
                timeout_seconds=timeout_seconds,
                stdout=_partial_output(exc.stdout),
                stderr=_partial_output(exc.stderr),
                duration_seconds=time.monotonic() - started,
            ) from exc
        except OSError as exc:
            raise CommandExecutionError(f"Cannot execute {format_command(command)}: {exc}") from exc
        finally:
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
        logger.debug(
            "[command] Finished: %s (exit %s, %.1fs)",
            format_command(command),
            completed.returncode,
            time.monotonic() - started,
        )
        return CommandResult(
            command=list(command),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _resolve_command(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None
    ) -> list[str]:
        """Resolve native Windows command shims without enabling shell parsing."""
        resolved = list(command)
        if not resolved or not _is_windows():
            return resolved

        executable = resolved[0]
        if "/" in executable or "\\" in executable:
            return resolved

        suffix = Path(executable).suffix.casefold()
        if suffix and suffix not in _WINDOWS_EXECUTABLE_SUFFIXES:
            return resolved

        environment = os.environ if env is None else env
        path = CommandClient._environment_value(environment, "PATH")
        search_path = str(cwd)
        if path:
            search_path = os.pathsep.join((search_path, path))

        candidates = (
            (executable,)
            if suffix
            else tuple(f"{executable}{item}" for item in _WINDOWS_EXECUTABLE_SUFFIXES)
        )
        for candidate in candidates:
            located = shutil.which(candidate, path=search_path)
            if located is not None:
                resolved[0] = located
                return resolved
        return resolved

    @staticmethod
    def _environment_value(environment: Mapping[str, str], name: str) -> str | None:
        for key, value in environment.items():
            if key.casefold() == name.casefold():
                return value
        return None

    @staticmethod
    def _os_error_details(error: OSError) -> str:
        details = [f"filename={error.filename!r}", f"errno={error.errno!r}"]
        winerror = getattr(error, "winerror", None)
        if winerror is not None:
            details.append(f"winerror={winerror!r}")
        return ", ".join(details)
