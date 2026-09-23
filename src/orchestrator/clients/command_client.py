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
from typing import TypeAlias

from orchestrator.clients.exceptions import CommandExecutionError, CommandTimeoutError
from orchestrator.clients.windows_job import CREATE_SUSPENDED, WindowsJob
from orchestrator.runtime_logging import format_command

logger = logging.getLogger(__name__)

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
# Bounds on waiting after a timed-out process tree is killed, so a descendant that escaped the
# kill and still holds an output pipe cannot keep the timeout from being reported.
_POST_KILL_DRAIN_SECONDS = 10.0
_TASKKILL_TIMEOUT_SECONDS = 30.0
_PROCESS_GROUP_POLL_SECONDS = 0.05
# Run as a standalone script, never imported; its status lines are mirrored here.
_SUPERVISOR_SCRIPT = Path(__file__).with_name("process_supervisor.py")
_SUPERVISOR_STOPPED = "stopped"
_SUPERVISOR_SPAWN_ERROR = "spawn-error"


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


class _TreeSupervisor:
    """The orchestrator's side of a Linux command launched under ``process_supervisor.py``."""

    def __init__(self, status_fd: int) -> None:
        self._status_fd: int | None = status_fd
        self._status: list[str] | None = None

    def request_stop(self, process: subprocess.Popen[str]) -> None:
        # Never kill the supervisor itself: that would release descendants it still contains.
        try:
            process.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def stopped(self, process: subprocess.Popen[str]) -> bool:
        """Whether the supervisor reported that no descendant of the command survived."""
        return _SUPERVISOR_STOPPED in self._status_lines(process)

    def raise_spawn_error(self, process: subprocess.Popen[str], command: Sequence[str]) -> None:
        """Raise the error the supervisor hit launching the command, as a direct launch would."""
        for line in self._status_lines(process):
            name, _, value = line.partition(" ")
            if name == _SUPERVISOR_SPAWN_ERROR:
                code = int(value) if value.isdigit() else 0
                # OSError picks the matching subclass, such as FileNotFoundError for ENOENT.
                raise OSError(code, os.strerror(code), command[0])

    def close(self) -> None:
        if self._status_fd is not None:
            os.close(self._status_fd)
            self._status_fd = None

    def _status_lines(self, process: subprocess.Popen[str]) -> list[str]:
        # The report is complete, and reading cannot block, only once the supervisor has exited.
        if self._status is None and process.returncode is not None and self._status_fd is not None:
            chunks: list[bytes] = []
            while chunk := os.read(self._status_fd, 4096):
                chunks.append(chunk)
            self._status = b"".join(chunks).decode("ascii", errors="replace").splitlines()
        return self._status or []


_Containment: TypeAlias = WindowsJob | _TreeSupervisor


def _popen(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None,
    env: Mapping[str, str] | None,
    creationflags: int = 0,
    pass_fds: Sequence[int] = (),
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(args),
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
        creationflags=creationflags,
        pass_fds=pass_fds,
    )


def _can_supervise(command: Sequence[str]) -> bool:
    return (
        sys.platform == "linux"
        and bool(command)
        and bool(sys.executable)
        and _SUPERVISOR_SCRIPT.is_file()
    )


def _start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None,
    env: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[str], _Containment | None]:
    """Launch a command so that its whole process tree can later be killed.

    Windows commands start suspended inside a job object, so every descendant is contained before
    the command can launch one. Linux commands run under ``process_supervisor.py``, a child
    subreaper that keeps every descendant killable, even one that left the process group with
    ``setsid``. Other POSIX commands, such as on macOS, only lead their own session and process
    group, whose members are killed and awaited as a group.
    """
    if sys.platform == "win32":
        return _start_in_job(command, cwd=cwd, input_text=input_text, env=env)
    if not _can_supervise(command):
        return _popen(command, cwd=cwd, input_text=input_text, env=env), None
    status_read, status_write = os.pipe()
    try:
        process = _popen(
            [sys.executable, "-I", "-S", str(_SUPERVISOR_SCRIPT), str(status_write), *command],
            cwd=cwd,
            input_text=input_text,
            env=env,
            pass_fds=(status_write,),
        )
    except BaseException:
        os.close(status_read)
        raise
    finally:
        # Only the supervisor holds the write end, so the report ends when it exits.
        os.close(status_write)
    return process, _TreeSupervisor(status_read)


def _start_in_job(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None,
    env: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[str], WindowsJob | None]:
    job = WindowsJob.create()
    try:
        process = _popen(
            command,
            cwd=cwd,
            input_text=input_text,
            env=env,
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


def _terminate_process_tree(
    process: subprocess.Popen[str], containment: _Containment | None
) -> bool:
    """Kill a command and the descendants it launched, such as test runners behind npm/Gradle.

    Returns False when some descendant is not known to have been stopped. A supervised command
    kills its tree asynchronously; ``_stop_confirmed`` reports its outcome once it has exited.
    """
    if isinstance(containment, _TreeSupervisor):
        containment.request_stop(process)
        return True
    if containment is not None:
        stopped = containment.terminate(wait_seconds=_POST_KILL_DRAIN_SECONDS)
    elif sys.platform == "win32":
        stopped = _taskkill_process_tree(process.pid)
    else:
        stopped = True
        try:
            # The group outlives its leader, so this also reaches orphaned descendants.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
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


def _stop_confirmed(process: subprocess.Popen[str], containment: _Containment | None) -> bool:
    """Whether a terminated command's tree is known to have stopped, once it has been reaped.

    A supervised command relies on its supervisor's report and a POSIX process group on no member
    remaining; Windows commands report their outcome from ``_terminate_process_tree``.
    """
    if isinstance(containment, _TreeSupervisor):
        return containment.stopped(process)
    if containment is None:
        return _process_group_emptied(process.pid)
    return True


def _process_group_emptied(pgid: int) -> bool:
    """Wait, with a bound, until no member of a killed POSIX process group remains.

    Killed members that are not the reaped leader are reparented to init, which reaps them. A
    descendant that left the group with ``setsid`` is not a member, so it is only detected when it
    still holds an output pipe; the Linux supervisor exists to close that gap.
    """
    if sys.platform == "win32":
        return True
    deadline = time.monotonic() + _POST_KILL_DRAIN_SECONDS
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # A member that cannot be signalled still exists.
            pass
        if time.monotonic() >= deadline:
            logger.warning("[command] Process group %s still has members after the kill", pgid)
            return False
        time.sleep(_PROCESS_GROUP_POLL_SECONDS)


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
    process, containment = _start_process(command, cwd=cwd, input_text=input_text, env=env)
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
        if isinstance(containment, _TreeSupervisor):
            containment.raise_spawn_error(process, command)
    except subprocess.TimeoutExpired:
        stopped = _terminate_process_tree(process, containment)
        partial_stdout, partial_stderr, drained = _drain_terminated_process(process)
        if not (stopped and drained and _stop_confirmed(process, containment)):
            raise _ProcessTreeSurvivedError from None
        raise subprocess.TimeoutExpired(
            list(command), timeout_seconds, output=partial_stdout, stderr=partial_stderr
        ) from None
    except BaseException:
        _terminate_process_tree(process, containment)
        _reap_interrupted_process(process)
        raise
    finally:
        if containment is not None:
            containment.close()
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _reap_interrupted_process(process: subprocess.Popen[str]) -> None:
    """Wait, with a bound, for an interrupted command to exit so it does not linger."""
    try:
        process.wait(timeout=_POST_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("[command] Interrupted process %s did not exit", process.pid)


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
