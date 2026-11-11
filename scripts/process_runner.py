"""Deadline-bound subprocess execution that also stops spawned descendants."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def _stop_posix_process_group(process: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    """Ask a subprocess tree to stop, then kill any members left after the grace period."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return

    deadline = time.monotonic() + grace_seconds
    while True:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(0.05)
    process.wait()


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return kernel32, handle


def run_bounded(
    command: list[str],
    cwd: Path,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> subprocess.CompletedProcess:
    """Run a command until timeout, terminating its complete process tree."""
    remaining = float(timeout)
    if remaining <= 0:
        return subprocess.CompletedProcess(command, 124)

    if os.name == "nt":
        kernel32, job = _windows_job()
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
                import ctypes
                error = ctypes.get_last_error()
                process.kill()
                process.wait()
                raise ctypes.WinError(error)
            try:
                returncode = process.wait(timeout=remaining)
                return subprocess.CompletedProcess(command, returncode)
            except subprocess.TimeoutExpired:
                if not kernel32.TerminateJobObject(job, 124):
                    error = __import__("ctypes").get_last_error()
                    raise __import__("ctypes").WinError(error)
                process.wait()
                return subprocess.CompletedProcess(command, 124)
            except KeyboardInterrupt:
                try:
                    sent = bool(kernel32.GenerateConsoleCtrlEvent(1, process.pid))
                except OSError:
                    sent = False
                if sent:
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    if not kernel32.TerminateJobObject(job, 130):
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    process.wait()
                raise
        finally:
            kernel32.CloseHandle(job)

    process = subprocess.Popen(
        command, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr,
        start_new_session=True,
    )
    try:
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(command, returncode)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return subprocess.CompletedProcess(command, 124)
    except KeyboardInterrupt:
        _stop_posix_process_group(process)
        raise
