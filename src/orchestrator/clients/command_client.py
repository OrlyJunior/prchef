from __future__ import annotations

import logging
import os
import selectors
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

from orchestrator.clients.exceptions import (
    CommandExecutionError,
    CommandInterruptedError,
    CommandProcessTreeUnconfirmedError,
    CommandTimeoutError,
)
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
_SUPERVISOR_COMPLETED = "completed"
_active_commands: dict[subprocess.Popen[str], _Containment | None] = {}
_retained_supervisors: dict[subprocess.Popen[str], _TreeSupervisor] = {}
_active_lock = threading.RLock()
_stopping = False


def interrupt_active_commands() -> None:
    """Stop every in-flight command when the owning app exits."""
    global _stopping
    with _active_lock:
        _stopping = True
        active = (*_active_commands.items(), *_retained_supervisors.items())
        _retained_supervisors.clear()
    for process, containment in active:
        _terminate_process_tree(process, containment)
    deadline = time.monotonic() + _POST_KILL_DRAIN_SECONDS
    for process, containment in active:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(_PROCESS_GROUP_POLL_SECONDS)
        if process.poll() is None:
            logger.warning("[command] Command %s did not stop during app exit", process.pid)
        if isinstance(containment, _TreeSupervisor):
            containment.close()
    if sys.platform == "win32":
        WindowsJob.close_retained()


def assert_worktree_idle(workspace: Path) -> None:
    """Refuse recovery while a prior command still occupies its worktree."""
    if not workspace.exists():
        return
    root = workspace.resolve()
    if sys.platform == "darwin":
        try:
            inspected = subprocess.run(
                ["lsof", "-nP", "-t", "+D", str(root)],
                capture_output=True, text=True, timeout=_TASKKILL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandExecutionError(
                "Cannot inspect retained worktree processes; retry after lsof is available"
            ) from exc
        if inspected.returncode not in (0, 1) or inspected.stderr.strip():
            raise CommandExecutionError("Cannot verify retained worktree is idle")
        active = {
            int(line) for line in inspected.stdout.splitlines() if line.isdecimal()
        } - {os.getpid()}
        if active:
            raise CommandExecutionError(
                f"Process {min(active)} is still using the retained worktree; "
                "wait for it to stop before retrying"
            )
        return
    if sys.platform != "linux":
        return
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except (OSError, RuntimeError):
            continue
        if cwd == root or cwd.is_relative_to(root):
            raise CommandExecutionError(
                f"Process {entry.name} is still using the retained worktree; "
                "wait for it to stop before retrying"
            )


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
    """The orchestrator's side of a POSIX command launched under ``process_supervisor.py``."""

    def __init__(self, status_fd: int) -> None:
        self._status_fd: int | None = status_fd
        self._status: list[str] | None = None
        self._status_buffer = bytearray()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._completed_code: int | None = None

    @property
    def completed_code(self) -> int | None:
        return self._completed_code

    def communicate(
        self, process: subprocess.Popen[str], input_text: str | None, timeout: float,
    ) -> tuple[str, str]:
        """Collect output until the command completes, even if its supervisor stays alive."""
        deadline = time.monotonic() + timeout
        pending_input = memoryview(input_text.encode("utf-8") if input_text else b"")
        with selectors.DefaultSelector() as selector:
            streams = ((process.stdout, self._stdout), (process.stderr, self._stderr))
            for stream, target in streams:
                if stream is not None and not stream.closed:
                    selector.register(stream, selectors.EVENT_READ, target)
            if self._status_fd is not None:
                selector.register(self._status_fd, selectors.EVENT_READ, "status")
            if process.stdin is not None and not process.stdin.closed:
                if pending_input:
                    # Windows stubs omit this POSIX API; supervisors run only on POSIX.
                    getattr(os, "set_blocking")(process.stdin.fileno(), False)  # noqa: B009
                    selector.register(process.stdin, selectors.EVENT_WRITE, "input")
                else:
                    process.stdin.close()
            while True:
                process.poll()
                outputs_closed = all(
                    stream is None or stream.closed for stream, _ in streams
                )
                if outputs_closed and (
                    self._completed_code is not None
                    or process.returncode is not None
                ):
                    return (self._stdout.decode("utf-8", errors="replace"),
                            self._stderr.decode("utf-8", errors="replace"))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        process.args, timeout, output=bytes(self._stdout),
                        stderr=bytes(self._stderr),
                    )
                for key, _ in selector.select(min(remaining, _PROCESS_GROUP_POLL_SECONDS)):
                    if key.data == "input":
                        try:
                            written = os.write(key.fd, pending_input[:65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            written = len(pending_input)
                        pending_input = pending_input[written:]
                        if not pending_input:
                            selector.unregister(key.fileobj)
                            if process.stdin is not None:
                                process.stdin.close()
                    elif key.data == "status":
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fd)
                            self.close()
                        else:
                            self._status_buffer.extend(chunk)
                            while b"\n" in self._status_buffer:
                                line, _, rest = self._status_buffer.partition(b"\n")
                                self._status_buffer = bytearray(rest)
                                decoded = line.decode("ascii", errors="replace")
                                if self._status is None:
                                    self._status = []
                                self._status.append(decoded)
                                prefix, _, value = decoded.partition(" ")
                                if prefix == _SUPERVISOR_COMPLETED:
                                    self._completed_code = int(value)
                    else:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            key.data.extend(chunk)
                        else:
                            selector.unregister(key.fileobj)
                            stream = process.stdout if key.data is self._stdout else process.stderr
                            if stream is not None:
                                stream.close()

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
        sys.platform in {"linux", "darwin"}
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

    Windows commands start suspended inside a job object. POSIX commands run under
    ``process_supervisor.py``. Linux uses a child subreaper; macOS tracks descendants
    and checks retained worktrees before recovery.
    """
    if sys.platform == "win32":
        return _start_in_job(command, cwd=cwd, input_text=input_text, env=env)
    if not _can_supervise(command):
        return _popen(command, cwd=cwd, input_text=input_text, env=env), None
    status_read, status_write = os.pipe()
    try:
        process = _popen(
            [sys.executable, "-I", "-S", str(_SUPERVISOR_SCRIPT),
             str(status_write), str(os.getpid()), *command],
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
    if job is None:
        raise OSError("Windows process containment is unavailable")
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
                created_job.close()
                process.communicate()
                raise OSError("Could not contain the launched process tree")
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
        _close_unread_pipes(process)
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
    with _active_lock:
        for prior, supervisor in tuple(_retained_supervisors.items()):
            if prior.poll() is not None:
                supervisor.close()
                _retained_supervisors.pop(prior)
        if _stopping:
            _terminate_process_tree(process, containment)
            _reap_interrupted_process(process)
            if containment is not None:
                containment.close()
            raise CommandInterruptedError("Application is closing")
        _active_commands[process] = containment
    try:
        if isinstance(containment, _TreeSupervisor):
            stdout, stderr = containment.communicate(process, input_text, timeout_seconds)
        else:
            stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
        if _stopping:
            raise CommandInterruptedError("Application is closing")
        if isinstance(containment, _TreeSupervisor):
            containment.raise_spawn_error(process, command)
        command_returncode = (
            containment.completed_code
            if isinstance(containment, _TreeSupervisor)
            and containment.completed_code is not None else process.returncode
        )
    except subprocess.TimeoutExpired:
        stopped = _terminate_process_tree(process, containment)
        partial_stdout: bytes | str | None
        partial_stderr: bytes | str | None
        if isinstance(containment, _TreeSupervisor):
            try:
                partial_stdout, partial_stderr = containment.communicate(
                    process, None, _POST_KILL_DRAIN_SECONDS
                )
                drained = True
            except subprocess.TimeoutExpired as exc:
                partial_stdout, partial_stderr, drained = exc.stdout, exc.stderr, False
        else:
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
        with _active_lock:
            _active_commands.pop(process, None)
            if (
                isinstance(containment, _TreeSupervisor)
                and containment.completed_code is not None
                and process.poll() is None
                and not _stopping
            ):
                _retained_supervisors[process] = containment
                containment = None
        if containment is not None:
            containment.close()
    return subprocess.CompletedProcess(list(command), command_returncode, stdout, stderr)


def _reap_interrupted_process(process: subprocess.Popen[str]) -> None:
    """Wait, with a bound, for an interrupted command to exit so it does not linger."""
    _close_unread_pipes(process)
    try:
        process.wait(timeout=_POST_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("[command] Interrupted process %s did not exit", process.pid)


def _close_unread_pipes(process: subprocess.Popen[str]) -> None:
    """Close a stopped command's pipes before an unread wait, as ``Popen.__exit__`` does.

    Otherwise a Linux supervisor relaying already-produced output into a full pipe would block
    and outlive the wait; with the read ends closed its writes fail, so it finishes stopping the
    tree and exits. Windows is skipped: an interrupted ``communicate`` may leave reader threads
    there, which a close would wait on, and its job kill involves no relaying.
    """
    if _is_windows():
        return
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            # Closing stdin flushes pending input, which the stopped command cannot accept.
            pass


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
            raise CommandProcessTreeUnconfirmedError(
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
