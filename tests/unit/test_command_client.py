from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.clients import codex_client, command_client
from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import (
    CommandExecutionError,
    CommandProcessTreeUnconfirmedError,
    CommandTimeoutError,
)
from orchestrator.config.project_config import CodexCliProviderSettings


def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="completed", stderr="")


def test_non_windows_command_is_passed_to_subprocess_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched: list[list[str]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: False)
    monkeypatch.setattr(
        command_client.shutil,
        "which",
        lambda *args, **kwargs: pytest.fail("non-Windows should not resolve shims"),
    )
    monkeypatch.setattr(
        command_client,
        "_run_process",
        lambda command, **kwargs: (launched.append(command), _completed(command))[1],
    )

    configured = ["tool", "argument with spaces", "&literal"]
    result = CommandClient().run(configured, cwd=tmp_path)

    assert launched == [configured]
    assert result.command == configured


@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_windows_native_shim_is_resolved_without_shell_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []
    lookups: list[tuple[str, str | None]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: True)

    def fake_which(candidate: str, *, path: str | None = None) -> str | None:
        lookups.append((candidate, path))
        if candidate == f"codex{suffix}":
            return f"C:\\tools\\codex{suffix}"
        return None

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launched.append((command, kwargs))
        return _completed(command)

    monkeypatch.setattr(command_client.shutil, "which", fake_which)
    monkeypatch.setattr(command_client, "_run_process", fake_run)

    configured = ["codex", "exec", "argument with spaces", "&literal"]
    result = CommandClient().run(
        configured,
        cwd=tmp_path,
        env={"Path": "C:\\tools", "PATHEXT": ".CMD;.BAT"},
    )

    assert launched == [
        (
            [f"C:\\tools\\codex{suffix}", "exec", "argument with spaces", "&literal"],
            {
                "cwd": tmp_path,
                "input_text": None,
                "timeout_seconds": 900,
                "env": {"Path": "C:\\tools", "PATHEXT": ".CMD;.BAT"},
            },
        )
    ]
    assert lookups[-1][0] == f"codex{suffix}"
    assert str(tmp_path) in (lookups[-1][1] or "")
    assert result.command == configured


@pytest.mark.skipif(os.name != "nt", reason="Windows command shims are Windows-specific")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_windows_native_shim_can_run_from_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    command_name = f"orchestrator-test-shim-{suffix.removeprefix('.')}"
    shim = tmp_path / f"{command_name}{suffix}"
    shim.write_text("@echo off\r\necho %~1\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ.get("PATH", ""))))

    result = CommandClient().run(
        [command_name, "argument with spaces"], cwd=tmp_path, timeout_seconds=10
    )

    assert result.success
    assert result.stdout.strip() == "argument with spaces"


def test_launch_diagnostics_log_resolved_command_without_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_logger = logging.getLogger(command_client.__name__)
    previous_propagation = target_logger.propagate
    previous_disabled = target_logger.disabled
    target_logger.addHandler(caplog.handler)
    target_logger.propagate = False
    target_logger.disabled = False
    try:
        monkeypatch.setattr(command_client, "_is_windows", lambda: True)
        monkeypatch.setattr(
            command_client.shutil,
            "which",
            lambda candidate, path=None: "C:\\npm\\codex.cmd",
        )
        monkeypatch.setattr(
            command_client,
            "_run_process",
            lambda command, **kwargs: _completed(command),
        )

        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            CommandClient().run(
                ["codex", "exec", "--prompt", "prompt-argument", "argument with spaces"],
                cwd=tmp_path,
                input_text="private stdin prompt",
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation
        target_logger.disabled = previous_disabled

    messages = [record.getMessage() for record in caplog.records]
    assert "[command] Resolved executable: C:\\npm\\codex.cmd" in messages
    assert f"[command] Working directory: {tmp_path.resolve()}" in messages
    assert (
        "[command] Arguments: exec --prompt [REDACTED] argument with spaces" in messages
    )
    assert "private stdin prompt" not in caplog.text


def test_file_not_found_includes_safe_os_error_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_logger = logging.getLogger(command_client.__name__)
    previous_propagation = target_logger.propagate
    previous_disabled = target_logger.disabled
    target_logger.addHandler(caplog.handler)
    target_logger.propagate = False
    target_logger.disabled = False
    try:
        error = FileNotFoundError(2, "No such file", "missing-tool")
        error.winerror = 2  # type: ignore[attr-defined]

        def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise error

        monkeypatch.setattr(command_client, "_run_process", missing)

        with caplog.at_level(logging.INFO, logger=target_logger.name):
            with pytest.raises(CommandExecutionError, match="Executable not found: missing-tool"):
                CommandClient().run(["missing-tool"], cwd=tmp_path)
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation
        target_logger.disabled = previous_disabled

    assert "filename='missing-tool'" in caplog.text
    assert "errno=2" in caplog.text
    assert "winerror=2" in caplog.text


def test_missing_executable_keeps_existing_error_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched: list[list[str]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: True)
    monkeypatch.setattr(command_client.shutil, "which", lambda *args, **kwargs: None)

    def missing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        raise FileNotFoundError

    monkeypatch.setattr(command_client, "_run_process", missing)

    with pytest.raises(CommandExecutionError, match="Executable not found: missing-tool"):
        CommandClient().run(["missing-tool", "argument with spaces"], cwd=tmp_path)

    assert launched == [["missing-tool", "argument with spaces"]]


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_stdout", "expected_stderr"),
    [
        (b"collected 3 items\n", b"still running", "collected 3 items\n", "still running"),
        (b"bad \xff byte", None, "bad � byte", ""),
        ("text output", "text error", "text output", "text error"),
        (None, None, "", ""),
    ],
)
def test_timeout_carries_partial_output_and_configured_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    def hang(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout_seconds"], output=stdout, stderr=stderr
        )

    monkeypatch.setattr(command_client, "_is_windows", lambda: False)
    monkeypatch.setattr(command_client, "_run_process", hang)

    with pytest.raises(CommandTimeoutError) as raised:
        CommandClient().run(["tool", "check"], cwd=tmp_path, timeout_seconds=30)

    error = raised.value
    assert isinstance(error, CommandExecutionError)
    assert str(error) == "Command timed out after 30s: tool check"
    assert error.timeout_seconds == 30
    assert error.stdout == expected_stdout
    assert error.stderr == expected_stderr
    assert error.duration_seconds >= 0
    assert isinstance(error.__cause__, subprocess.TimeoutExpired)


def test_timeout_remains_catchable_as_command_execution_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def hang(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout_seconds"])

    monkeypatch.setattr(command_client, "_is_windows", lambda: False)
    monkeypatch.setattr(command_client, "_run_process", hang)

    with pytest.raises(CommandExecutionError, match="Command timed out after 900s: tool"):
        CommandClient().run(["tool"], cwd=tmp_path)


class _ScriptedStream:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def close(self) -> None:
        self._events.append(f"close:{self._name}")


class _ScriptedProcess:
    """Popen stand-in whose communicate calls follow a script of results or exceptions."""

    pid = 4321

    def __init__(self, events: list[str], *outcomes: object) -> None:
        self.events = events
        self._outcomes = list(outcomes)
        self.returncode: int | None = None
        self.stdin = None
        self.stdout = _ScriptedStream(events, "stdout")
        self.stderr = _ScriptedStream(events, "stderr")

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[Any, Any]:
        self.events.append(f"communicate:{timeout}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = -9
        return outcome  # type: ignore[return-value]

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(f"wait:{timeout}")
        self.returncode = -9
        return self.returncode


def _install_scripted_process(
    monkeypatch: pytest.MonkeyPatch,
    process: _ScriptedProcess,
    events: list[str],
    *,
    tree_stopped: bool = True,
) -> None:
    def terminate(terminated: _ScriptedProcess, job: object) -> bool:
        events.append(f"terminate:{terminated.pid}")
        return tree_stopped

    monkeypatch.setattr(command_client, "_is_windows", lambda: False)
    monkeypatch.setattr(
        command_client, "_start_process", lambda command, **kwargs: (process, None)
    )
    monkeypatch.setattr(command_client, "_terminate_process_tree", terminate)
    # The scripted pid is not a real process group.
    monkeypatch.setattr(command_client, "_process_group_emptied", lambda pgid: True)


def test_timeout_terminates_the_process_tree_before_collecting_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    process = _ScriptedProcess(
        events,
        subprocess.TimeoutExpired(["tool"], 30),
        ("collected 3 items\n", "hung in test_worker"),
    )
    _install_scripted_process(monkeypatch, process, events)

    with pytest.raises(CommandTimeoutError) as raised:
        CommandClient().run(["tool", "check"], cwd=tmp_path, timeout_seconds=30)

    drain = command_client._POST_KILL_DRAIN_SECONDS
    assert events == ["communicate:30", "terminate:4321", f"communicate:{drain}"]
    assert raised.value.stdout == "collected 3 items\n"
    assert raised.value.stderr == "hung in test_worker"
    assert process.returncode is not None


def test_timeout_is_not_correctable_when_an_escaped_descendant_keeps_output_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    drain = command_client._POST_KILL_DRAIN_SECONDS
    process = _ScriptedProcess(
        events,
        subprocess.TimeoutExpired(["tool"], 30),
        subprocess.TimeoutExpired(["tool"], drain, output=b"partial \xff", stderr=None),
    )
    _install_scripted_process(monkeypatch, process, events)

    with pytest.raises(CommandProcessTreeUnconfirmedError) as raised:
        CommandClient().run(["tool"], cwd=tmp_path, timeout_seconds=30)

    assert not isinstance(raised.value, CommandTimeoutError)
    assert str(raised.value) == (
        "Command timed out after 30s and its process tree could not be confirmed "
        "terminated: tool"
    )
    assert events == [
        "communicate:30",
        "terminate:4321",
        f"communicate:{drain}",
        "close:stdout",
        "close:stderr",
        f"wait:{drain}",
    ]


def test_timeout_is_not_correctable_when_the_process_tree_kill_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    process = _ScriptedProcess(
        events,
        subprocess.TimeoutExpired(["tool"], 30),
        ("partial", ""),
    )
    _install_scripted_process(monkeypatch, process, events, tree_stopped=False)

    with pytest.raises(CommandProcessTreeUnconfirmedError) as raised:
        CommandClient().run(["tool"], cwd=tmp_path, timeout_seconds=30)

    assert not isinstance(raised.value, CommandTimeoutError)
    assert "could not be confirmed terminated" in str(raised.value)


def test_interrupted_command_terminates_the_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    process = _ScriptedProcess(events, KeyboardInterrupt())
    _install_scripted_process(monkeypatch, process, events)

    with pytest.raises(KeyboardInterrupt):
        CommandClient().run(["tool"], cwd=tmp_path, timeout_seconds=30)

    drain = command_client._POST_KILL_DRAIN_SECONDS
    # The unread output pipes are closed before waiting, so nothing can block writing to them.
    assert events == [
        "communicate:30",
        "terminate:4321",
        "close:stdout",
        "close:stderr",
        f"wait:{drain}",
    ]
    assert process.returncode is not None


_NOISY_SCRIPT = """\
import sys

print("ready", flush=True)
chunk = "x" * 65536
while True:
    sys.stdout.write(chunk)
    sys.stdout.flush()
"""


def _unread_pipe_bytes(fd: int) -> int:
    import fcntl
    import termios

    pending = bytearray(4)
    fcntl.ioctl(fd, termios.FIONREAD, pending)
    return int.from_bytes(pending, sys.byteorder)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor only")
def test_interrupted_cleanup_lets_a_supervisor_blocked_on_full_output_exit(
    tmp_path: Path,
) -> None:
    noisy = tmp_path / "noisy.py"
    noisy.write_text(_NOISY_SCRIPT, encoding="utf-8")

    process, containment = command_client._start_process(
        [sys.executable, str(noisy)], cwd=tmp_path, input_text=None, env=None
    )
    try:
        assert isinstance(containment, command_client._TreeSupervisor)
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        stdout_fd = process.stdout.fileno()
        # The writer is endless and the pipe is no longer drained. Once output arrives, allow it
        # to back up; Linux pipes need not expose their full nominal capacity through FIONREAD.
        deadline = time.monotonic() + 60
        while _unread_pipe_bytes(stdout_fd) == 0:
            assert time.monotonic() < deadline, "the output pipe stayed empty"
            time.sleep(0.01)
        time.sleep(0.1)

        command_client._terminate_process_tree(process, containment)
        command_client._reap_interrupted_process(process)

        # The supervisor exited within the bounded wait rather than staying blocked on the pipe.
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if containment is not None:
            containment.close()


_LAUNCHER_SCRIPT = """\
import subprocess
import sys
import time

subprocess.Popen(
    [sys.executable, sys.argv[1]], stdout=sys.stdout, stderr=sys.stderr
)
time.sleep(600)
"""

_EXITING_LAUNCHER_SCRIPT = """\
import subprocess
import sys

subprocess.Popen(
    [sys.executable, sys.argv[1]], stdout=sys.stdout, stderr=sys.stderr
)
"""

_DESCENDANT_SCRIPT = """\
import time

print("ready", flush=True)
time.sleep(600)
"""


@pytest.mark.parametrize(
    "launcher_script",
    [_LAUNCHER_SCRIPT, _EXITING_LAUNCHER_SCRIPT],
    ids=["running-launcher", "exited-launcher"],
)
def test_terminating_the_process_tree_stops_descendants_holding_output(
    tmp_path: Path, launcher_script: str
) -> None:
    launcher = tmp_path / "launcher.py"
    descendant = tmp_path / "descendant.py"
    launcher.write_text(launcher_script, encoding="utf-8")
    descendant.write_text(_DESCENDANT_SCRIPT, encoding="utf-8")

    process, job = command_client._start_process(
        [sys.executable, str(launcher), str(descendant)],
        cwd=tmp_path,
        input_text=None,
        env=None,
    )
    try:
        assert process.stdout is not None
        # Wait until the descendant is running and holds the inherited output pipe.
        assert process.stdout.readline().strip() == "ready"
        if launcher_script is _EXITING_LAUNCHER_SCRIPT and not isinstance(
            job, command_client._TreeSupervisor
        ):
            # Like a .cmd shim that returned while its child still runs: only the descendant is
            # left, so the kill cannot reach it through the launcher. A Linux supervisor outlives
            # the launcher; its orphan case is covered by the session-escape test below.
            process.wait(timeout=60)

        assert command_client._terminate_process_tree(process, job)

        # Output reaches EOF only after every process holding the pipe has exited; the bound only
        # keeps a regression from hanging the suite.
        process.communicate(timeout=60)
        assert command_client._stop_confirmed(process, job)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if job is not None:
            job.close()

    assert process.returncode is not None


# Each launcher starts a descendant in a new session, beyond the reach of a process-group kill,
# and prints "ready <pid>" once it runs.
_REDIRECTED_SESSION_ESCAPE_SCRIPT = """\
import subprocess
import sys
import time

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print("ready", descendant.pid, flush=True)
time.sleep(600)
"""

_ORPHANED_SESSION_ESCAPE_SCRIPT = """\
import subprocess
import sys

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
print("ready", descendant.pid, flush=True)
"""

_DETACHING_LAUNCHER_SCRIPT = """\
import subprocess
import sys

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print("ready", descendant.pid, flush=True)
"""


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return int(stat[stat.rfind(b")") + 2 :].split()[1])


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor only")
@pytest.mark.parametrize(
    "launcher_script",
    [_REDIRECTED_SESSION_ESCAPE_SCRIPT, _ORPHANED_SESSION_ESCAPE_SCRIPT],
    ids=["hung-launcher-redirected-descendant", "exited-launcher-orphaned-descendant"],
)
def test_timeout_confirms_only_after_stopping_descendants_that_left_the_session(
    tmp_path: Path, launcher_script: str
) -> None:
    launcher = tmp_path / "launcher.py"
    launcher.write_text(launcher_script, encoding="utf-8")

    process, containment = command_client._start_process(
        [sys.executable, str(launcher)], cwd=tmp_path, input_text=None, env=None
    )
    descendant: int | None = None
    try:
        assert isinstance(containment, command_client._TreeSupervisor)
        assert process.stdout is not None
        ready, pid = process.stdout.readline().split()
        assert ready == "ready"
        descendant = int(pid)
        if launcher_script is _ORPHANED_SESSION_ESCAPE_SCRIPT:
            # Wait for the launcher to exit: its orphan is then reparented to the supervisor
            # rather than to init. The bound only keeps a regression from hanging the suite.
            deadline = time.monotonic() + 60
            while _parent_pid(descendant) != process.pid:
                assert time.monotonic() < deadline, "orphan was not kept by the supervisor"
                time.sleep(0.01)

        assert command_client._terminate_process_tree(process, containment)
        _, _, drained = command_client._drain_terminated_process(process)

        assert drained
        assert command_client._stop_confirmed(process, containment)
        # The supervisor confirms only once it reaped every descendant, so the pid is gone.
        assert not _process_exists(descendant)
    finally:
        if descendant is not None and _process_exists(descendant):
            os.kill(descendant, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
            process.wait()
        if containment is not None:
            containment.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.parametrize(
    "launcher_script",
    [_LAUNCHER_SCRIPT, _EXITING_LAUNCHER_SCRIPT],
    ids=["running-launcher", "exited-launcher"],
)
def test_process_group_timeout_is_confirmed_without_a_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launcher_script: str
) -> None:
    # The path used on macOS, and on Linux whenever the supervisor is unavailable.
    monkeypatch.setattr(command_client, "_can_supervise", lambda command: False)
    launcher = tmp_path / "launcher.py"
    descendant = tmp_path / "descendant.py"
    launcher.write_text(launcher_script, encoding="utf-8")
    descendant.write_text(_DESCENDANT_SCRIPT, encoding="utf-8")

    process, containment = command_client._start_process(
        [sys.executable, str(launcher), str(descendant)],
        cwd=tmp_path,
        input_text=None,
        env=None,
    )
    try:
        assert containment is None
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        if launcher_script is _EXITING_LAUNCHER_SCRIPT:
            # Only the descendant is left in the group, holding the output pipe.
            process.wait(timeout=60)

        assert command_client._terminate_process_tree(process, containment)
        _, _, drained = command_client._drain_terminated_process(process)

        assert drained
        assert command_client._stop_confirmed(process, containment)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


class _GroupLeader:
    pid = 4321

    def kill(self) -> None:
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
def test_process_group_kill_is_confirmed_once_no_member_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    members_left = [1]

    def killpg(pgid: int, signum: int) -> None:
        signals.append((pgid, signum))
        if signum == 0:
            if not members_left:
                raise ProcessLookupError
            members_left.pop()

    monkeypatch.setattr(command_client.os, "killpg", killpg)
    monkeypatch.setattr(command_client.time, "sleep", lambda seconds: None)
    process: Any = _GroupLeader()

    assert command_client._terminate_process_tree(process, None)
    assert command_client._stop_confirmed(process, None)
    assert signals == [(4321, signal.SIGKILL), (4321, 0), (4321, 0)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.parametrize("probe_error", [None, PermissionError], ids=["member", "foreign-member"])
def test_process_group_with_a_surviving_member_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, probe_error: type[OSError] | None
) -> None:
    def killpg(pgid: int, signum: int) -> None:
        if signum == 0 and probe_error is not None:
            raise probe_error

    monkeypatch.setattr(command_client.os, "killpg", killpg)
    # No waiting: the first probe that still finds a member decides.
    monkeypatch.setattr(command_client, "_POST_KILL_DRAIN_SECONDS", 0.0)
    monkeypatch.setattr(
        command_client.time, "sleep", lambda seconds: pytest.fail("waited past the bound")
    )

    process: Any = _GroupLeader()

    assert not command_client._stop_confirmed(process, None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
def test_process_group_that_cannot_be_killed_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killpg(pgid: int, signum: int) -> None:
        raise PermissionError

    monkeypatch.setattr(command_client.os, "killpg", killpg)
    process: Any = _GroupLeader()

    assert not command_client._terminate_process_tree(process, None)


@pytest.mark.parametrize(
    ("report", "returncode", "confirmed"),
    [
        (b"stopped\n", -9, True),
        (b"", -9, False),
        (b"stopped\n", None, False),
    ],
    ids=["reported", "not-reported", "still-running"],
)
def test_supervised_timeout_is_confirmed_only_by_the_supervisor_report(
    report: bytes, returncode: int | None, confirmed: bool
) -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, report)
    os.close(write_end)
    supervisor = command_client._TreeSupervisor(read_end)
    process: Any = SimpleNamespace(returncode=returncode)
    try:
        assert command_client._stop_confirmed(process, supervisor) is confirmed
    finally:
        supervisor.close()


def test_completed_command_keeps_its_exit_code_and_both_streams(tmp_path: Path) -> None:
    script = (
        "import sys; data = sys.stdin.read(); print(data.upper()); "
        "print('warning', file=sys.stderr); sys.exit(3)"
    )

    result = CommandClient().run(
        [sys.executable, "-c", script], cwd=tmp_path, input_text="checked", timeout_seconds=60
    )

    assert result.exit_code == 3
    assert result.stdout == "CHECKED\n"
    assert result.stderr == "warning\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_command_killed_by_a_signal_keeps_its_signal_status(tmp_path: Path) -> None:
    script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    result = CommandClient().run([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=60)

    assert result.exit_code == -signal.SIGKILL


def test_missing_executable_is_reported_as_before(tmp_path: Path) -> None:
    with pytest.raises(CommandExecutionError, match="Executable not found: orchestrator-no-such"):
        CommandClient().run(["orchestrator-no-such-tool"], cwd=tmp_path, timeout_seconds=60)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions only")
def test_background_process_that_released_output_does_not_delay_completion(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "launcher.py"
    launcher.write_text(_DETACHING_LAUNCHER_SCRIPT, encoding="utf-8")

    result = CommandClient().run([sys.executable, str(launcher)], cwd=tmp_path, timeout_seconds=60)

    ready, pid = result.stdout.split()
    background = int(pid)
    try:
        assert result.exit_code == 0
        assert ready == "ready"
        # Like a build daemon, it is left running exactly as when the command ran directly.
        assert _process_exists(background)
    finally:
        os.kill(background, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "win32", reason="Job objects are Windows-specific")
def test_windows_commands_are_contained_in_a_job_object(tmp_path: Path) -> None:
    process, job = command_client._start_process(
        [sys.executable, "-c", "print('ran')"], cwd=tmp_path, input_text=None, env=None
    )
    try:
        stdout, _ = process.communicate(timeout=60)
    finally:
        if job is not None:
            job.close()

    assert job is not None
    assert stdout.strip() == "ran"


class RecordingCommandClient:
    def __init__(
        self, *, exit_code: int = 0, stdout: str = "completed", stderr: str = ""
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((command, kwargs))
        return CommandResult(
            command=command,
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
            duration_seconds=0.1,
        )


def test_codex_client_delegates_its_configured_command_to_command_client(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert command[:5] == ["codex", "exec", "--json", "--sandbox", "workspace-write"]
    assert command[-1] == "-"
    assert "--approve-for-me" not in command
    assert "--output-schema" not in command
    assert kwargs["input_text"] == "private prompt"


def test_codex_client_materializes_and_cleans_up_output_schema(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    schema_paths: list[Path] = []

    class SchemaRecordingCommandClient(RecordingCommandClient):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            schema_path = Path(command[command.index("--output-schema") + 1])
            schema_paths.append(schema_path)
            assert schema_path.is_relative_to(tmp_path) is False
            assert json.loads(schema_path.read_text(encoding="utf-8")) == schema
            return super().run(command, **kwargs)

    commands = SchemaRecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path, output_schema=schema)

    command, _ = commands.calls[0]
    assert "--json" in command
    assert "--output-schema" in command
    assert len(schema_paths) == 1
    assert not schema_paths[0].exists()


def test_codex_client_keeps_jsonl_parsing_with_output_schema(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout="\n".join(
            (
                '{"type":"item.completed","item":{"type":"file_change"}}',
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"{\\"plan\\": \\"ready\\"}"}}',
                '{"type":"turn.completed"}',
            )
        )
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement(
        "Return the plan as JSON",
        tmp_path,
        output_schema={
            "type": "object",
            "properties": {"plan": {"type": "string"}},
        },
    )

    command, _ = commands.calls[0]
    assert "--json" in command
    assert "--output-schema" in command
    assert result.success
    assert result.summary == '{"plan": "ready"}'


def test_codex_client_cleans_up_output_schema_after_execution_error(tmp_path: Path) -> None:
    schema_path: Path | None = None

    class FailingCommandClient:
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            nonlocal schema_path
            schema_path = Path(command[command.index("--output-schema") + 1])
            assert schema_path.exists()
            raise RuntimeError("Codex did not start")

    client = CodexClient(
        FailingCommandClient(),  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    with pytest.raises(RuntimeError, match="Codex did not start"):
        client.implement("private prompt", tmp_path, output_schema={"type": "object"})

    assert schema_path is not None
    assert not schema_path.exists()


def test_codex_client_approve_for_me_replaces_explicit_sandbox(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            "{output_file}",
            "-",
        ],
        timeout_seconds=30,
        approval_policy="approve-for-me",
    )

    client.implement("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert command[:3] == ["codex", "exec", "--approve-for-me"]
    assert "--sandbox" not in command
    assert "--output-last-message" in command
    assert "danger-full-access" not in command
    assert command[-1] == "-"
    assert kwargs["input_text"] == "private prompt"


def test_codex_client_passes_configured_model_and_reasoning_effort(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
        model="model-one",
        reasoning_effort="high",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command[2:7] == [
        "--json",
        "--model",
        "model-one",
        "--config",
        'model_reasoning_effort="high"',
    ]
    assert command.count("--model") == 1


@pytest.mark.parametrize("model", ["model-one", "model-two"])
def test_codex_client_changes_only_the_selected_model_argument(
    tmp_path: Path, model: str
) -> None:
    commands = RecordingCommandClient(exit_code=1, stderr="configured model unavailable")
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "{output_file}", "-"],
        timeout_seconds=30,
        model=model,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert commands.calls[0][0][3:5] == ["--model", model]
    assert len(commands.calls) == 1


def test_codex_client_preserves_custom_matching_runtime_flags(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        [
            "codex",
            "exec",
            "--model",
            "model-one",
            "--config=model_reasoning_effort='high'",
            "{output_file}",
            "-",
        ],
        timeout_seconds=30,
        model="model-one",
        reasoning_effort="high",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command.count("--model") == 1
    assert command.count("--config=model_reasoning_effort='high'") == 1


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {"model": "model-one", "command": ["codex", "exec", "--model", "model-two"]},
            "conflicting model flag",
        ),
        (
            {
                "reasoning_effort": "high",
                "command": ["codex", "exec", "-c", "model_reasoning_effort=low"],
            },
            "conflicting model_reasoning_effort",
        ),
        (
            {"model": "model-one", "command": ["codex", "exec", "--model"]},
            "model flag without a value",
        ),
        (
            {
                "reasoning_effort": "high",
                "command": ["codex", "exec", "--config", "model_reasoning_effort="],
            },
            "model_reasoning_effort without a value",
        ),
    ],
)
def test_codex_custom_command_rejects_conflicting_runtime_settings(
    settings: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CodexCliProviderSettings.model_validate({"type": "codex-cli", **settings})


@pytest.mark.parametrize("field", ["model", "reasoning_effort"])
def test_codex_empty_runtime_settings_fail_clearly(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        CodexCliProviderSettings.model_validate(
            {"type": "codex-cli", "command": ["codex", "exec", "-"], field: "  "}
        )


def test_codex_client_approve_for_me_preserves_model_and_reasoning(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
        approval_policy="approve-for-me",
        model="model-one",
        reasoning_effort="medium",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command[:8] == [
        "codex",
        "exec",
        "--approve-for-me",
        "--json",
        "--model",
        "model-one",
        "--config",
        'model_reasoning_effort="medium"',
    ]
    assert "--sandbox" not in command


def test_codex_runtime_settings_do_not_embed_a_hard_coded_model_or_leak_to_services() -> None:
    client_source = Path(codex_client.__file__).read_text(encoding="utf-8")
    services = Path(__file__).parents[2] / "src" / "orchestrator" / "services"

    assert "gpt-" not in client_source.casefold()
    for filename in ("spec_service.py", "implementation_service.py", "review_service.py"):
        source = (services / filename).read_text(encoding="utf-8")
        assert "reasoning_effort" not in source


def test_codex_client_ignores_sandbox_fixture_text_in_free_form_output(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout="Creation failed because sandbox access to the requested file path was denied."
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert result.success
    assert result.summary == (
        "Creation failed because sandbox access to the requested file path was denied."
    )
    assert result.error_message is None
    assert "danger-full-access" not in commands.calls[0][0]


def test_codex_client_ignores_policy_denied_text_in_successful_command_output(
    tmp_path: Path,
) -> None:
    stdout = "\n".join(
        (
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"status":"completed","stdout":"codex_sandboxing policy_denied: '
            'Failed to write file output.txt"}}',
            '{"type":"item.completed","item":{"type":"file_change","status":"completed"}}',
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"Implementation completed."}}',
            '{"type":"turn.completed"}',
        )
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert result.success
    assert result.summary == "Implementation completed."
    assert result.error_message is None
    assert not result.workspace_changes_may_exist


def test_codex_client_surfaces_structured_sandbox_denial(tmp_path: Path) -> None:
    stdout = (
        '{"type":"item.failed","item":{"type":"command_execution",'
        '"status":"failed","error":{"code":"policy_denied",'
        '"message":"write access was denied","operation":"file-write-create",'
        '"path":"C:/restricted/output.txt"}}}'
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.workspace_changes_may_exist
    assert result.error_message is not None
    assert "structured sandbox denial" in result.error_message
    assert "operation=file-write-create" in result.error_message
    assert "path=C:/restricted/output.txt" in result.error_message
    assert "workspace write denial" not in result.error_message
    assert "exit 0" in result.error_message


def test_codex_client_surfaces_structured_sandbox_denial_for_command(
    tmp_path: Path,
) -> None:
    stdout = (
        '{"type":"turn.failed","error":{"code":"policy_denied",'
        '"details":{"operation":"process-exec","command":["git", "push", "origin", "main"]}}}'
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert (
        "sandbox denial for operation=process-exec, command=git push origin main"
        in result.error_message
    )
    assert "workspace write denial" not in result.error_message


def test_codex_client_uses_workspace_message_for_general_structured_sandbox_denial(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(
        stdout='{"type":"error","error":{"code":"policy_denied","message":"write denied"}}'
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert "structured workspace write denial" in result.error_message


def test_codex_client_surfaces_structured_terminal_failure(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout=(
            '{"type":"turn.failed","error":{"code":"execution_error",'
            '"message":"The requested implementation could not be completed."}}'
        )
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.workspace_changes_may_exist
    assert result.error_message is not None
    assert "structured execution failure" in result.error_message
    assert "execution_error" in result.error_message


def test_codex_client_does_not_add_json_twice(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--json", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path)

    assert commands.calls[0][0].count("--json") == 1


def test_codex_client_preserves_non_sandbox_execution_errors(tmp_path: Path) -> None:
    commands = RecordingCommandClient(exit_code=1, stderr="permission denied: output.txt")
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert not result.workspace_changes_may_exist
    assert result.error_message == "permission denied: output.txt"
