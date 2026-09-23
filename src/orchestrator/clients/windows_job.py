"""Windows Job Object containment for commands whose whole process tree must be killable.

``taskkill /T`` walks parent process IDs from a live root, so it cannot reach descendants once the
launched command (such as a ``.cmd`` shim) has exited. Processes started inside a job inherit it
regardless of their parent's lifetime, and terminating the job kills every one of them.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_EXIT_POLL_SECONDS = 0.05


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _load_api() -> tuple[Any, Any]:
    """Return typed kernel32 and ntdll bindings."""
    if sys.platform != "win32":
        raise OSError("Job objects are only available on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    )
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll")
    # Undocumented but long-stable; the only way to resume a whole process Popen started
    # suspended, since subprocess closes the primary thread handle.
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return kernel32, ntdll


def _last_error() -> str:
    if sys.platform != "win32":
        return "unavailable"
    return str(ctypes.WinError(ctypes.get_last_error()))


class WindowsJob:
    """A job object holding one launched command and every process it starts.

    The job deliberately has no kill-on-close limit: processes a command leaves running after it
    completes normally (such as a build daemon) keep today's behavior. Only ``terminate`` kills.
    """

    def __init__(self, kernel32: Any, ntdll: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._ntdll = ntdll
        self._handle: int | None = handle

    @classmethod
    def create(cls) -> WindowsJob | None:
        """Create an anonymous job, or return None when job objects are unavailable."""
        try:
            kernel32, ntdll = _load_api()
        except (AttributeError, OSError):
            logger.warning("[command] Job objects are unavailable", exc_info=True)
            return None
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("[command] Could not create a job object: %s", _last_error())
            return None
        return cls(kernel32, ntdll, handle)

    def adopt_suspended(self, pid: int) -> bool:
        """Place a process started with ``CREATE_SUSPENDED`` in this job, then let it run.

        Assigning before the process runs guarantees nothing it launches escapes the job. Returns
        whether it was assigned; the process is resumed either way. Raises OSError, after
        terminating the process, if it cannot be resumed.
        """
        access = _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME
        process_handle = self._kernel32.OpenProcess(access, False, pid)
        if not process_handle:
            raise OSError(f"Cannot open suspended process {pid}: {_last_error()}")
        try:
            assigned = bool(self._kernel32.AssignProcessToJobObject(self._handle, process_handle))
            if not assigned:
                logger.warning(
                    "[command] Could not assign process %s to a job object: %s",
                    pid,
                    _last_error(),
                )
            status = self._ntdll.NtResumeProcess(process_handle)
            if status != 0:
                self._kernel32.TerminateProcess(process_handle, 1)
                raise OSError(f"Cannot resume process {pid} (NTSTATUS {status & 0xFFFFFFFF:#x})")
            return assigned
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self, *, wait_seconds: float) -> bool:
        """Kill every process in the job and wait, bounded, until none remain.

        Returns whether the job was confirmed empty.
        """
        if self._handle is None:
            return False
        if not self._kernel32.TerminateJobObject(self._handle, 1):
            logger.warning("[command] Could not terminate job object: %s", _last_error())
            return False
        deadline = time.monotonic() + wait_seconds
        while True:
            active = self._active_processes()
            if active == 0:
                return True
            if active is None or time.monotonic() >= deadline:
                logger.warning(
                    "[command] Job object still had %s active process(es) after termination",
                    "an unknown number of" if active is None else active,
                )
                return False
            time.sleep(_EXIT_POLL_SECONDS)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def _active_processes(self) -> int | None:
        info = _BasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            logger.warning("[command] Could not query job object: %s", _last_error())
            return None
        return int(info.ActiveProcesses)
