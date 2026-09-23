from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator.clients.exceptions import CommandExecutionError, CommandTimeoutError
from orchestrator.clients.windows_job import CREATE_SUSPENDED, WindowsJob
from orchestrator.runtime_logging import format_command

logger = logging.getLogger(__name__)

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
# Bounds on waiting after a timed-out process tree is killed, so a descendant that escaped the
# kill and still holds an output pipe cannot keep the timeout from being reported.
_POST_KILL_DRAIN_SECONDS = 10.0
_TASKKILL_TIMEOUT_SECONDS = 30.0


def _is_windows() -> bool:
    return os.name == "nt"


def _partial_output(value: bytes | str | None) -> str:
    """Decode output captured before a timeout; it may be raw bytes even in text mode."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class _ProcessTreeSurvivedError(Exception):
    """A timed-out command's process tree could not be confirmed terminated."""


def _start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None,
    env: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[str], WindowsJob | None]:
    """Launch a command so that its whole process tree can later be killed.

    POSIX commands lead their own session and process group. Windows commands start suspended
    inside a job object, so every descendant is contained before the command can launch one.
    """
    job: WindowsJob | None = WindowsJob.create() if sys.platform == "win32" else None
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env) if env is not None else None,
            shell=False,
            start_new_session=sys.platform != "win32",
            creationflags=CREATE_SUSPENDED if job is not None else 0,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    if job is not None:
        created_job = job
        try:
            if not created_job.adopt_suspended(process.pid):
                # Fall back to taskkill, which reaches descendants only through a live root.
                created_job.close()
                job = None
        except BaseException:
            created_job.close()
            # It never ran, so it launched nothing; closing the pipes needs only this process.
            try:
                process.kill()
            except OSError:
                pass
            process.communicate()
            raise
    return process, job


def _terminate_process_tree(process: subprocess.Popen[str], job: WindowsJob | None) -> bool:
    """Kill a command and the descendants it launched, such as test runners behind npm/Gradle.

    Returns whether every descendant is known to have been stopped.
    """
    if job is not None:
        stopped = job.terminate(wait_seconds=_POST_KILL_DRAIN_SECONDS)
    elif sys.platform == "win32":
        stopped = _taskkill_process_tree(process.pid)
    else:
        try:
            # The group outlives its leader, so this also reaches orphaned descendants.
            os.killpg(process.pid, signal.SIGKILL)
            stopped = True
        except ProcessLookupError:
            stopped = True
        except PermissionError:
            logger.warning("[command] Could not terminate process group %s", process.pid)
            stopped = False
    try:
        process.kill()
    except OSError:
        pass
    return stopped


def _taskkill_process_tree(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("[command] Could not terminate process tree %s", pid, exc_info=True)
        return False
    if result.returncode != 0:
        # Typically the launched command already exited; descendants it left are unreachable.
        logger.warning(
            "[command] taskkill could not terminate process tree %s (exit %s)",
            pid,
            result.returncode,
        )
        return False
    return True


def _drain_terminated_process(
    process: subprocess.Popen[str],
) -> tuple[bytes | str | None, bytes | str | None, bool]:
    """Collect output produced before termination and reap the command, with a bounded wait.

    The final flag reports whether every output pipe closed and the command was reaped.
    """
    try:
        stdout, stderr = process.communicate(timeout=_POST_KILL_DRAIN_SECONDS)
        return stdout, stderr, True
    except subprocess.TimeoutExpired as exc:
        # A descendant outside the terminated tree still holds an output pipe. Keep what was read
        # so far and stop waiting.
        logger.warning(
            "[command] Output stayed open after terminating timed-out process %s", process.pid
        )
        try:
            process.wait(timeout=_POST_KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("[command] Timed-out process %s did not exit", process.pid)
        return exc.stdout, exc.stderr, False


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None,
    timeout_seconds: float,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Run a command; on timeout, terminate its whole process tree before raising.

    ``subprocess.run`` kills only the launched process and then waits without a bound for its
    output pipes, which a surviving child can hold open indefinitely. A timeout is reported as
    ``TimeoutExpired`` only once the tree is confirmed stopped, since callers may then act in the
    same working directory; otherwise ``_ProcessTreeSurvivedError`` is raised.
    """
    process, job = _start_process(command, cwd=cwd, input_text=input_text, env=env)
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        stopped = _terminate_process_tree(process, job)
        partial_stdout, partial_stderr, drained = _drain_terminated_process(process)
        if not (stopped and drained):
            raise _ProcessTreeSurvivedError from None
        raise subprocess.TimeoutExpired(
            list(command), timeout_seconds, output=partial_stdout, stderr=partial_stderr
        ) from None
    except BaseException:
        _terminate_process_tree(process, job)
        raise
    finally:
        if job is not None:
            job.close()
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


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
            completed = _run_process(
                resolved_command,
                cwd=cwd,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                env=env,
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
        except _ProcessTreeSurvivedError as exc:
            # Deliberately not a CommandTimeoutError: callers must not treat this as a completed,
            # correctable failure while part of the command may still be running.
            raise CommandExecutionError(
                f"Command timed out after {timeout_seconds}s and its process tree could not be "
                f"confirmed terminated: {format_command(command)}"
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
