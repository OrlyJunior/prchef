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
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_EXIT_POLL_SECONDS = 0.05
_retained_jobs: list[WindowsJob] = []


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


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
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
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

    Closing its retained handle ends descendants, including those left by a
    command that already returned. Handles remain owned until app shutdown.
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
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(handle)
            logger.warning("[command] Could not configure job close containment: %s", _last_error())
            return None
        return cls(kernel32, ntdll, handle)

    def adopt_suspended(self, pid: int) -> bool:
        """Place a process started with ``CREATE_SUSPENDED`` in this job, then let it run.

        Assigning before the process runs guarantees nothing it launches escapes the job. Returns
        whether it was assigned; an unassigned process is stopped before it runs.
        Raises OSError after terminating a process that cannot be resumed.
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
                self._kernel32.TerminateProcess(process_handle, 1)
                return False
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
            # Retain only jobs that still own a background process. Closing an
            # empty job now avoids one handle per completed command.
            if self._active_processes() == 0:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
            elif self not in _retained_jobs:
                _retained_jobs.append(self)

    @staticmethod
    def close_retained() -> None:
        while _retained_jobs:
            job = _retained_jobs.pop()
            if job._handle is not None:
                if job._active_processes() != 0:
                    job.terminate(wait_seconds=10.0)
                job._kernel32.CloseHandle(job._handle)
                job._handle = None

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
