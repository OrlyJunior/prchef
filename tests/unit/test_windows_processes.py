from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.clients import command_client, windows_processes
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.clients.windows_processes import ProcessLocation


@pytest.mark.parametrize("pointer_size", [4, 8], ids=["32-bit", "64-bit"])
def test_remote_directory_uses_target_layout_and_utf16(pointer_size: int) -> None:
    directory = "C:\\retained\\validação\\nested\\"
    encoded = directory.encode("utf-16-le")
    # Large addresses also catch accidental 64-bit pointer truncation.
    peb = 0x100000000 if pointer_size == 8 else 0x1000
    parameters, text = peb + 0x1000, peb + 0x2000
    descriptor = (
        len(encoded).to_bytes(2, "little")
        + (len(encoded) + 2).to_bytes(2, "little")
        + bytes(pointer_size - 4)
        + text.to_bytes(pointer_size, "little")
    )
    parameters_offset, directory_offset = (0x20, 0x38) if pointer_size == 8 else (0x10, 0x24)
    memory = {
        (peb + parameters_offset, pointer_size): parameters.to_bytes(pointer_size, "little"),
        (parameters + directory_offset, pointer_size * 2): descriptor,
        (text, len(encoded)): encoded,
    }
    assert windows_processes._read_directory(
        lambda address, size: memory[address, size], peb, pointer_size,
    ) == directory


@pytest.mark.parametrize(("length", "maximum", "address"), [
    (0, 0, 0x3000), (3, 4, 0x3000), (6, 4, 0x3000), (4, 4, 0),
])
def test_invalid_remote_directory_is_unreadable(length: int, maximum: int, address: int) -> None:
    descriptor = (
        length.to_bytes(2, "little") + maximum.to_bytes(2, "little")
        + bytes(4) + address.to_bytes(8, "little")
    )
    memory = {(0x1020, 8): (0x2000).to_bytes(8, "little"), (0x2038, 16): descriptor}
    with pytest.raises(OSError, match="working directory is unavailable"):
        windows_processes._read_directory(lambda address, size: memory[address, size], 0x1000, 8)


@pytest.mark.parametrize("memory_access", [True, False], ids=["unreadable-cwd", "limited-rights"])
def test_image_fallback_closes_process_handle(
    monkeypatch: pytest.MonkeyPatch, memory_access: bool,
) -> None:
    access_requested = []
    closed = []

    def open_process(access: int, _inherit: bool, _pid: int) -> int:
        access_requested.append(access)
        return 42 if memory_access or access == 0x1000 else 0

    def unreadable(*_args: Any) -> str:
        raise OSError("Process memory is unreadable")

    kernel32 = SimpleNamespace(OpenProcess=open_process, CloseHandle=closed.append)
    monkeypatch.setattr(windows_processes, "_read_process_cwd", unreadable)
    monkeypatch.setattr(
        windows_processes, "_read_process_image", lambda *_args: "C:\\repo\\agent.exe",
    )
    assert windows_processes._inspect_process(kernel32, None, 7, "agent.exe") == ProcessLocation(
        7, "agent.exe", None, "C:\\repo\\agent.exe",
    )
    assert access_requested == ([0x0410] if memory_access else [0x0410, 0x1000])
    assert closed == [42]


def test_readable_cwd_does_not_use_image_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []
    kernel32 = SimpleNamespace(OpenProcess=lambda *_args: 42, CloseHandle=closed.append)
    monkeypatch.setattr(windows_processes, "_read_process_cwd", lambda *_args: "C:\\elsewhere")

    def unexpected(*_args: Any) -> None:
        pytest.fail("Image fallback must only be used when cwd cannot be read")

    monkeypatch.setattr(windows_processes, "_read_process_image", unexpected)
    assert windows_processes._inspect_process(kernel32, None, 7, "agent.exe").cwd == "C:\\elsewhere"
    assert closed == [42]


def test_exited_or_protected_process_is_tolerated() -> None:
    def unexpected(_handle: int) -> None:
        pytest.fail("Failed OpenProcess did not create a handle")

    kernel32 = SimpleNamespace(OpenProcess=lambda *_args: 0, CloseHandle=unexpected)
    assert windows_processes._inspect_process(kernel32, None, 7, "exited.exe") == ProcessLocation(
        7, "exited.exe", None, None,
    )


@pytest.mark.parametrize("enumeration_error", [18, 5])
def test_snapshot_ignores_self_and_closes_handle_even_on_enumeration_failure(
    monkeypatch: pytest.MonkeyPatch, enumeration_error: int,
) -> None:
    closed = []
    inspected = []
    pids = iter([(os.getpid(), "orchestrator.exe"), (7, "agent.exe")])

    def next_process(_snapshot: int, pointer: Any) -> bool:
        try:
            pid, name = next(pids)
        except StopIteration:
            return False
        entry = ctypes.cast(pointer, ctypes.POINTER(windows_processes._ProcessEntry)).contents
        entry.th32ProcessID = pid
        entry.szExeFile = name
        return True

    def inspect(_kernel: Any, _ntdll: Any, pid: int, name: str) -> ProcessLocation:
        inspected.append(pid)
        return ProcessLocation(pid, name, "C:\\repo", None)

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=lambda *_args: 42,
        Process32FirstW=next_process, Process32NextW=next_process, CloseHandle=closed.append,
    )
    monkeypatch.setattr(windows_processes, "_load_api", lambda: (kernel32, None))
    monkeypatch.setattr(windows_processes, "_inspect_process", inspect)
    monkeypatch.setattr(windows_processes, "_last_error", lambda: enumeration_error)
    if enumeration_error == 18:
        assert windows_processes.process_locations() == [
            ProcessLocation(7, "agent.exe", "C:\\repo", None),
        ]
    else:
        with pytest.raises(OSError, match="enumerate Windows processes"):
            windows_processes.process_locations()
    assert inspected == [7]
    assert closed == [42]


@pytest.mark.parametrize("location", ["root", "nested", "executable"])
def test_windows_resume_refuses_busy_worktree_with_actionable_process_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str,
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    cwd = str(root / "nested") if location == "nested" else str(root)
    process = ProcessLocation(
        31415, "agent.exe", None if location == "executable" else cwd, str(root / "agent.exe"),
    )
    monkeypatch.setattr(command_client.sys, "platform", "win32")
    monkeypatch.setattr(command_client, "process_locations", lambda: [process])
    with pytest.raises(CommandExecutionError) as error:
        command_client.assert_worktree_idle(root)
    message = str(error.value)
    assert "31415 (agent.exe)" in message
    assert "still using the retained worktree" in message
    assert "taskkill /PID 31415 /T /F" in message
    assert "before retrying resume" in message


def test_windows_resume_ignores_self_siblings_and_unrelated_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    processes = [
        ProcessLocation(os.getpid(), "orchestrator.exe", str(root), None),
        ProcessLocation(7, "sibling.exe", str(tmp_path / "retained-other"), None),
        ProcessLocation(8, "unreadable.exe", None, str(tmp_path / "elsewhere" / "agent.exe")),
        ProcessLocation(9, "exited.exe", None, None),
        ProcessLocation(10, "elsewhere.exe", str(tmp_path), str(root / "agent.exe")),
    ]
    monkeypatch.setattr(command_client.sys, "platform", "win32")
    monkeypatch.setattr(command_client, "process_locations", lambda: processes)
    command_client.assert_worktree_idle(root)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows process inspection")
def test_native_executable_path_is_available_with_limited_query_rights() -> None:
    kernel32, _ = windows_processes._load_api()
    handle = kernel32.OpenProcess(0x1000, False, os.getpid())
    assert handle
    try:
        executable = windows_processes._read_process_image(kernel32, handle)
        assert executable is not None
        assert Path(executable).resolve() == Path(sys.executable).resolve()
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.parametrize(
    ("host_size", "target_size", "host_wow64"),
    [(8, 8, False), (8, 4, False), (4, 4, False), (4, 4, True), (4, 8, True)],
)
def test_process_directory_selects_native_or_wow64_memory_reader(
    monkeypatch: pytest.MonkeyPatch, host_size: int, target_size: int, host_wow64: bool,
) -> None:
    peb = 0x100000000 if target_size == 8 else 0x1000
    target_wow64 = target_size == 4 and (host_size == 8 or host_wow64)
    wow64_reader = host_size == 4 and target_size == 8
    queries = []

    def query(_handle: int, info_class: int, info: Any, size: int, _length: Any) -> int:
        queries.append("native")
        assert info_class == 0
        assert size == 6 * target_size
        contents = bytes(target_size) + peb.to_bytes(target_size, "little") + bytes(4 * target_size)
        ctypes.memmove(info, contents, size)
        return 0

    def query64(*args: Any) -> int:
        result = query(*args)
        queries[-1] = "wow64"
        return result

    def wow64_peb(_ntdll: Any, handle: int) -> int:
        return int(host_wow64) if handle == 99 else (peb if target_wow64 else 0)

    def directory(read: Any, actual_peb: int, pointer_size: int) -> str:
        assert actual_peb == peb
        assert pointer_size == target_size
        assert read(peb, 8) == b"observed"
        return "C:\\repo"

    def read_memory(
        _kernel: Any, _ntdll: Any, handle: int, address: int, size: int, *, wow64: bool,
    ) -> bytes:
        assert (handle, address, size, wow64) == (7, peb, 8, wow64_reader)
        return b"observed"

    kernel32 = SimpleNamespace(GetCurrentProcess=lambda: 99)
    ntdll = SimpleNamespace(
        NtQueryInformationProcess=query, NtWow64QueryInformationProcess64=query64,
    )
    query.argtypes = ()  # type: ignore[attr-defined]
    original_sizeof = ctypes.sizeof

    def host_sizeof(value: Any) -> int:
        return host_size if value is ctypes.c_void_p else original_sizeof(value)

    monkeypatch.setattr(
        ctypes, "sizeof", host_sizeof,
    )
    monkeypatch.setattr(windows_processes, "_wow64_peb", wow64_peb)
    monkeypatch.setattr(windows_processes, "_read_directory", directory)
    monkeypatch.setattr(windows_processes, "_read_memory", read_memory)
    assert windows_processes._read_process_cwd(kernel32, ntdll, 7) == "C:\\repo"
    assert queries == ([] if host_size == 8 and target_wow64 else [
        "wow64" if wow64_reader else "native",
    ])


def test_windows_resume_refuses_failed_process_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def failed_scan() -> list[ProcessLocation]:
        raise OSError("Snapshot failed")

    monkeypatch.setattr(command_client.sys, "platform", "win32")
    monkeypatch.setattr(command_client, "process_locations", failed_scan)
    with pytest.raises(CommandExecutionError, match="Cannot inspect.*permissions and retry"):
        command_client.assert_worktree_idle(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows process inspection")
@pytest.mark.parametrize("nested", [False, True], ids=["worktree-root", "worktree-subdirectory"])
def test_windows_resume_detects_uncontained_live_validation_process(
    tmp_path: Path, nested: bool,
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    cwd = root / "validação" if nested else root
    cwd.mkdir(exist_ok=True)
    # Popen bypasses WindowsJob to model a descendant surviving an older app session.
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "import sys; print('ready'); sys.stdin.readline()"],
        cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(CommandExecutionError, match=rf"Process {process.pid} \(python.exe\)"):
            command_client.assert_worktree_idle(root)
    finally:
        process.communicate("stop\n", timeout=10)
    command_client.assert_worktree_idle(root)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows process inspection")
def test_windows_resume_detects_orphan_left_by_a_previous_session(tmp_path: Path) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    ready = root / "ready"
    child_script = (
        "import sys, time; from pathlib import Path; "
        "Path(sys.argv[1]).touch(); time.sleep(300)"
    )
    owner_script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid)"
    )
    # The previous owner exits without any current-session job object or PID bookkeeping.
    owner = subprocess.run(
        [sys.executable, "-c", owner_script, child_script, str(ready)],
        cwd=root, capture_output=True, text=True, timeout=10, check=True,
    )
    child_pid = int(owner.stdout.strip())
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "Orphaned child did not start"
        with pytest.raises(CommandExecutionError, match=rf"Process {child_pid} \(python.exe\)"):
            command_client.assert_worktree_idle(root)
    finally:
        os.kill(child_pid, signal.SIGTERM)
    # Wait for termination to be visible in the process snapshot before retrying.
    deadline = time.monotonic() + 10
    while True:
        try:
            command_client.assert_worktree_idle(root)
            break
        except CommandExecutionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
