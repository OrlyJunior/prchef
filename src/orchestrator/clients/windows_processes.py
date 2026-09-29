"""Read process locations without depending on jobs owned by the current app session."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_NO_MORE_FILES = 18
_PROCESS_BASIC_INFORMATION = 0
_PROCESS_WOW64_INFORMATION = 26


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


@dataclass(frozen=True)
class ProcessLocation:
    pid: int
    name: str
    cwd: str | None
    executable: str | None


def _load_api() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise OSError("Process location inspection is only available on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel32, name)
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
        function.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = (
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    )
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD,
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll")
    query_args = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.ULONG, wintypes.LPDWORD)
    ntdll.NtQueryInformationProcess.argtypes = query_args
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    return kernel32, ntdll


def _last_error() -> int:
    if sys.platform != "win32":
        return 0
    return ctypes.get_last_error()


def _read_memory(
    kernel32: Any, ntdll: Any, handle: int, address: int, size: int, *, wow64: bool,
) -> bytes:
    buffer = ctypes.create_string_buffer(size)
    if wow64:
        # A 32-bit inspector needs 64-bit addresses to read a native 64-bit process.
        read = ntdll.NtWow64ReadVirtualMemory64
        read.argtypes = (
            wintypes.HANDLE, ctypes.c_uint64, wintypes.LPVOID,
            ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
        )
        read.restype = ctypes.c_long
        count64 = ctypes.c_uint64()
        success = read(handle, address, buffer, size, ctypes.byref(count64)) == 0
        count = count64.value
    else:
        count_native = ctypes.c_size_t()
        success = bool(kernel32.ReadProcessMemory(
            handle, address, buffer, size, ctypes.byref(count_native),
        ))
        count = count_native.value
    if not success or count != size:
        raise OSError("Cannot read process memory")
    return buffer.raw


def _read_directory(read: Callable[[int, int], bytes], peb: int, pointer_size: int) -> str:
    # PEB.ProcessParameters and RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath.
    # These native layouts differ between 32- and 64-bit targets, not Python versions.
    parameters_offset, directory_offset = (0x20, 0x38) if pointer_size == 8 else (0x10, 0x24)
    parameters = int.from_bytes(read(peb + parameters_offset, pointer_size), "little")
    if not parameters:
        raise OSError("Process parameters are unavailable")
    descriptor = read(parameters + directory_offset, pointer_size * 2)
    length = int.from_bytes(descriptor[:2], "little")
    maximum = int.from_bytes(descriptor[2:4], "little")
    address = int.from_bytes(descriptor[pointer_size:], "little")
    if not address or not length or length % 2 or length > maximum:
        raise OSError("Process working directory is unavailable")
    return read(address, length).decode("utf-16-le")


def _wow64_peb(ntdll: Any, handle: int) -> int:
    peb = ctypes.c_size_t()
    if ntdll.NtQueryInformationProcess(
        handle, _PROCESS_WOW64_INFORMATION, ctypes.byref(peb), ctypes.sizeof(peb), None,
    ) != 0:
        raise OSError("Cannot query process architecture")
    return peb.value


def _read_process_cwd(kernel32: Any, ntdll: Any, handle: int) -> str:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    peb32 = _wow64_peb(ntdll, handle)
    wow64 = (
        pointer_size == 4 and not peb32
        and bool(_wow64_peb(ntdll, kernel32.GetCurrentProcess()))
    )
    if pointer_size == 8 and peb32:
        peb, pointer_size = peb32, 4
    else:
        query = ntdll.NtQueryInformationProcess
        if wow64:
            pointer_size = 8
            query = ntdll.NtWow64QueryInformationProcess64
            query.argtypes = ntdll.NtQueryInformationProcess.argtypes
            query.restype = ctypes.c_long
        # PROCESS_BASIC_INFORMATION has six pointer-sized slots; the second is the PEB.
        info = ctypes.create_string_buffer(6 * pointer_size)
        if query(handle, _PROCESS_BASIC_INFORMATION, info, ctypes.sizeof(info), None) != 0:
            raise OSError("Cannot query process parameters")
        peb = int.from_bytes(info.raw[pointer_size:2 * pointer_size], "little")
    if not peb:
        raise OSError("Process environment block is unavailable")
    return _read_directory(
        lambda address, size: _read_memory(kernel32, ntdll, handle, address, size, wow64=wow64),
        peb, pointer_size,
    )


def _read_process_image(kernel32: Any, handle: int) -> str | None:
    buffer = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buffer))
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value or None


def _inspect_process(kernel32: Any, ntdll: Any, pid: int, name: str) -> ProcessLocation:
    cwd = None
    executable = None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if handle:
        try:
            try:
                cwd = _read_process_cwd(kernel32, ntdll, handle)
            except (AttributeError, OSError, UnicodeError):
                pass  # Exited, protected, or unreadable native layout: use the image path.
            if cwd is None:
                executable = _read_process_image(kernel32, handle)
        finally:
            kernel32.CloseHandle(handle)
    else:
        # Querying the image requires fewer rights than reading another process's PEB.
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                executable = _read_process_image(kernel32, handle)
            finally:
                kernel32.CloseHandle(handle)
    return ProcessLocation(pid, name, cwd, executable)


def process_locations() -> list[ProcessLocation]:
    """Inspect all other processes, including descendants orphaned by an older session.

    Processes that exit during inspection, and protected system processes for which
    neither location can be queried, have no location. Snapshot/enumeration errors
    propagate so callers cannot silently report an idle worktree after a failed scan.
    """
    kernel32, ntdll = _load_api()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError("Cannot take a Windows process snapshot")
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        locations = []
        while found:
            pid = int(entry.th32ProcessID)
            if pid and pid != os.getpid():
                locations.append(_inspect_process(kernel32, ntdll, pid, entry.szExeFile))
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        if _last_error() != _ERROR_NO_MORE_FILES:
            raise OSError("Cannot enumerate Windows processes")
        return locations
    finally:
        kernel32.CloseHandle(snapshot)
