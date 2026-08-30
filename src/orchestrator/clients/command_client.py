from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.runtime_logging import format_command

logger = logging.getLogger(__name__)


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
        started = time.monotonic()
        logger.debug("[command] Running: %s", format_command(command))
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
                list(command),
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
            raise CommandExecutionError(f"Executable not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandExecutionError(
                f"Command timed out after {timeout_seconds}s: {format_command(command)}"
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
