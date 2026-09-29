"""POSIX process-tree supervisor that ``CommandClient`` runs each command under.

Run as ``python -I -S process_supervisor.py STATUS_FD PARENT_PID COMMAND...``.
The orchestrator never imports this module, so it uses only the standard library.

A process group cannot contain a command: any descendant can leave it with ``setsid``. On Linux
the supervisor registers as a child subreaper, so orphans remain its descendants. On macOS it
tracks observed descendant PIDs and checks for processes still using the worktree on recovery.

The command's output is relayed through the supervisor's own pipes, and the supervisor exits with
the command's own status once the command has exited and every holder of those pipes has closed
them. A completed command keeps background descendants owned until the app exits.
On SIGTERM the supervisor kills every descendant, relays output already produced,
and writes ``stopped`` to STATUS_FD only once no descendant remains.
"""

from __future__ import annotations

import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import NoReturn

# Windows type stubs omit POSIX APIs used by this standalone POSIX-only script.
# ruff: noqa: B009

assert sys.platform in {"linux", "darwin"}

# Status lines written to STATUS_FD; CommandClient reads them after the supervisor exits.
STOPPED = "stopped"
SPAWN_ERROR = "spawn-error"
COMPLETED = "completed"

_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1
# Stays below CommandClient's post-kill wait, so the outcome is reported before it stops waiting.
_STOP_DEADLINE_SECONDS = 5.0
_KILL_POLL_SECONDS = 0.01
_CHUNK_BYTES = 65536
_SPAWN_FAILED_EXIT_CODE = 127

_stop_requested = False


class _StopRequested(Exception):
    """Raised from the SIGTERM handler, interrupting relaying even during a blocked write."""


def _request_stop(signum: int, frame: FrameType | None) -> None:
    global _stop_requested
    if not _stop_requested:
        _stop_requested = True
        raise _StopRequested


def _wake(signum: int, frame: FrameType | None) -> None:
    """SIGCHLD handler; delivery writes to the wakeup fd, which ends the relay loop's wait."""


def _become_subreaper() -> bool:
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(
            ctypes.c_int(_PR_SET_CHILD_SUBREAPER),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
    except (AttributeError, ImportError, OSError):
        return False
    return int(result) == 0


def _watch_parent(parent_pid: int) -> None:
    """Stop owned descendants even if the app is killed without cleanup."""
    if sys.platform == "darwin":
        if os.getppid() != parent_pid:
            raise _StopRequested
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot configure parent-death signal")
    if os.getppid() != parent_pid:
        raise _StopRequested


def _report(status_fd: int, message: str) -> None:
    try:
        os.write(status_fd, f"{message}\n".encode("ascii"))
    except OSError:
        pass


def _descendants(root: int) -> list[int]:
    """Every process currently descending from ``root``."""
    children: dict[int, list[int]] = {}
    if sys.platform == "darwin":
        listed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False
        )
        if listed.returncode != 0:
            return []
        for line in listed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and all(field.isdecimal() for field in fields):
                children.setdefault(int(fields[1]), []).append(int(fields[0]))
    else:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as stat_file:
                    stat = stat_file.read()
            except OSError:
                continue
            # The command name is parenthesized and may itself contain spaces or parentheses.
            fields = stat[stat.rfind(b")") + 2 :].split()
            if len(fields) < 2:
                continue
            children.setdefault(int(fields[1]), []).append(int(entry))
    found: list[int] = []
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


class _Relay:
    """Copies one of the command's output pipes to the matching supervisor output."""

    def __init__(self, source: int, target: int) -> None:
        self.source = source
        self.target = target
        self.closed = False
        self._pending = bytearray()

    def pump(self) -> bool:
        """Relay one read; return False once the source ended or the target was closed."""
        data = os.read(self.source, _CHUNK_BYTES)
        if not data:
            return False
        self._pending += data
        return self.flush()

    def flush(self) -> bool:
        while self._pending:
            try:
                written = os.write(self.target, self._pending)
            except BrokenPipeError:
                # The orchestrator stopped reading. Closing the source lets the command see a
                # closed pipe, as it would when run directly.
                self._pending.clear()
                return False
            del self._pending[:written]
        return True

    def drain(self) -> None:
        """Relay what the stopped command already wrote, without waiting for more."""
        if self.closed:
            return
        getattr(os, "set_blocking")(self.source, False)
        try:
            while self.flush() and self.pump():
                pass
        except BlockingIOError:
            pass
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.source)


class _Supervisor:
    def __init__(self, status_fd: int, command: list[str]) -> None:
        self._status_fd = status_fd
        self._command = command
        self._contained = False
        self._command_pid: int | None = None
        self._returncode: int | None = None
        self._relays: list[_Relay] = []
        self._parent_pid: int | None = None
        self._known_descendants: set[int] = set()

    def run(self) -> int:
        """Run the command to completion and return its status, in ``Popen.returncode`` form."""
        os.set_inheritable(self._status_fd, False)
        try:
            os.fstat(0)
        except OSError:
            # Occupy a closed stdin so no pipe below is created as fd 0. It is not inheritable, so
            # the command still starts with stdin closed.
            os.open(os.devnull, os.O_RDONLY)
        self._contained = _become_subreaper()
        wake_read, wake_write = os.pipe()
        getattr(os, "set_blocking")(wake_write, False)
        signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
        signal.signal(getattr(signal, "SIGCHLD"), _wake)

        out_read, out_write = os.pipe()
        err_read, err_write = os.pipe()
        self._relays = [_Relay(out_read, 1), _Relay(err_read, 2)]
        try:
            command = subprocess.Popen(
                self._command, stdout=out_write, stderr=err_write, start_new_session=True
            )
        except OSError as exc:
            _report(self._status_fd, f"{SPAWN_ERROR} {exc.errno or 0}")
            return _SPAWN_FAILED_EXIT_CODE
        finally:
            os.close(out_write)
            os.close(err_write)
        self._command_pid = command.pid
        # Drop this copy of stdin so that, as when run directly, input nobody reads fails fast.
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)

        selector = selectors.DefaultSelector()
        selector.register(wake_read, selectors.EVENT_READ)
        for relay in self._relays:
            selector.register(relay.source, selectors.EVENT_READ, relay)
        while True:
            self._reap()
            if sys.platform == "darwin":
                if os.getppid() != self._parent_pid:
                    raise _StopRequested
                self._known_descendants.update(_descendants(os.getpid()))
                self._live_known_descendants()
            if self._returncode is not None and all(relay.closed for relay in self._relays):
                _report(self._status_fd, f"{COMPLETED} {self._returncode}")
                if not self._reap() and not self._live_known_descendants():
                    return self._returncode
                # The command has finished, but detached services remain in this
                # subreaper. Keep owning them until the application exits.
                while True:
                    if not self._reap() and not self._live_known_descendants():
                        return self._returncode
                    if os.getppid() != self._parent_pid:
                        raise _StopRequested
                    time.sleep(_KILL_POLL_SECONDS)
            for key, _ in selector.select(timeout=0.05 if sys.platform == "darwin" else None):
                ready = key.data
                if ready is None:
                    os.read(wake_read, 4096)
                elif not ready.pump():
                    selector.unregister(ready.source)
                    ready.close()

    def stop(self) -> int:
        """Kill every descendant, then report whether none survived."""
        stopped = self._kill_descendants() and self._contained
        if stopped:
            _report(self._status_fd, STOPPED)
        for relay in self._relays:
            relay.drain()
        return -int(getattr(signal, "SIGKILL"))

    def _kill_descendants(self) -> bool:
        """SIGKILL every descendant; return True once no child, and so no descendant, remains."""
        if self._command_pid is not None and self._returncode is None:
            # Fast path for the command's own group, whose id is pinned while it is unreaped.
            try:
                getattr(os, "killpg")(
                    self._command_pid, getattr(signal, "SIGKILL")
                )
            except OSError:
                pass
        deadline = time.monotonic() + _STOP_DEADLINE_SECONDS
        while True:
            self._known_descendants.update(_descendants(os.getpid()))
            for pid in self._known_descendants:
                try:
                    os.kill(pid, getattr(signal, "SIGKILL"))
                except OSError:
                    pass
            if not self._reap() and not self._live_known_descendants():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_KILL_POLL_SECONDS)

    def _live_known_descendants(self) -> bool:
        if sys.platform != "darwin":
            return False
        living: set[int] = set()
        for pid in self._known_descendants:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            living.add(pid)
        self._known_descendants = living
        return bool(living)

    def _reap(self) -> bool:
        """Reap exited children, including reparented orphans; return whether any remain."""
        while True:
            try:
                pid, status = os.waitpid(-1, getattr(os, "WNOHANG"))
            except ChildProcessError:
                return False
            if pid == 0:
                return True
            if pid == self._command_pid:
                self._returncode = os.waitstatus_to_exitcode(status)


def _exit_with(returncode: int) -> NoReturn:
    """Exit with the command's status, re-raising a fatal signal so that it reads the same."""
    if returncode < 0:
        signum = -returncode
        try:
            # The command's crash must not also leave a core dump of the supervisor.
            _, hard_limit = getattr(resource, "getrlimit")(
                getattr(resource, "RLIMIT_CORE")
            )
            getattr(resource, "setrlimit")(
                getattr(resource, "RLIMIT_CORE"), (0, hard_limit)
            )
        except (OSError, ValueError):
            pass
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (OSError, ValueError):
            pass  # SIGKILL and SIGSTOP always keep their default action.
        getattr(signal, "pthread_sigmask")(
            getattr(signal, "SIG_UNBLOCK"), {signum}
        )
        os.kill(os.getpid(), signum)
        returncode = 128 + signum  # Only reached when the signal's default action is to ignore.
    os._exit(returncode)


def main(argv: list[str]) -> NoReturn:
    supervisor = _Supervisor(int(argv[1]), argv[3:])
    supervisor._parent_pid = int(argv[2])
    try:
        signal.signal(signal.SIGTERM, _request_stop)
        _watch_parent(int(argv[2]))
        returncode = supervisor.run()
        # A stop request arriving after completion has nothing left to stop.
        getattr(signal, "pthread_sigmask")(
            getattr(signal, "SIG_BLOCK"), {signal.SIGTERM}
        )
    except _StopRequested:
        returncode = supervisor.stop()
    _exit_with(returncode)


if __name__ == "__main__":
    main(sys.argv)
