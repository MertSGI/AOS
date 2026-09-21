"""Owned, bounded background process execution.

All production subprocesses flow through this module.  On Windows each child is
assigned to a kill-on-close Job Object so a timeout, cancellation, or owner
shutdown terminates the complete descendant tree (including ``git.exe`` and
``conhost.exe``), not merely the immediate launcher process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


def background_python_executable(executable: Optional[str] = None) -> str:
    """Return the console-less interpreter for long-lived Windows daemons.

    Bounded commands that need captured stdout/stderr continue to use
    python.exe through run_headless(). Runtime, supervisor, panel and worker
    processes use pythonw.exe when the sibling executable exists.
    """
    value = Path(executable or sys.executable)

    if os.name == "nt":
        if value.name.lower() == "pythonw.exe":
            return str(value)

        if value.name.lower() == "python.exe":
            candidate = value.with_name("pythonw.exe")
            if candidate.is_file():
                return str(candidate)

    return str(value)


def get_headless_creationflags(*, detached: bool = False) -> int:
    """Return flags that prevent console-window allocation on Windows."""
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    if detached:
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    return flags


def get_headless_startupinfo() -> Optional[Any]:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def process_alive(pid: Any) -> bool:
    """Check liveness without invoking an external command."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(value, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x100000 | 0x001000, False, value)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def terminate_process_tree(pid: Any, *, exit_code: int = 1) -> None:
    """Terminate one explicitly identified process tree using OS APIs only."""
    try:
        root = int(pid)
    except (TypeError, ValueError):
        return
    if root <= 0 or root == os.getpid():
        return
    if os.name != "nt":
        try:
            os.killpg(root, 15)
        except OSError:
            try:
                os.kill(root, 15)
            except OSError:
                pass
        return
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    children: Dict[int, list[int]] = {}
    if snapshot not in (0, -1):
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
    order: list[int] = []
    stack = [root]
    while stack:
        current = stack.pop()
        order.append(current)
        stack.extend(children.get(current, []))
    for value in reversed(order):
        handle = kernel32.OpenProcess(0x0001 | 0x100000, False, value)
        if handle:
            try:
                kernel32.TerminateProcess(handle, int(exit_code))
                kernel32.WaitForSingleObject(handle, 5000)
            finally:
                kernel32.CloseHandle(handle)


def process_tree_snapshot(root_pids: Sequence[int]) -> list[Dict[str, Any]]:
    """Capture PID/PPID/name evidence without launching a diagnostic CLI."""
    roots = {int(pid) for pid in root_pids if int(pid) > 0}
    if os.name != "nt":
        return [{"pid": pid, "parent_pid": None, "name": None} for pid in sorted(roots)]
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    rows: list[Dict[str, Any]] = []
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (0, -1):
        return rows
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append({
                "pid": int(entry.th32ProcessID),
                "parent_pid": int(entry.th32ParentProcessID),
                "name": str(entry.szExeFile),
            })
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["parent_pid"] in selected and row["pid"] not in selected:
                selected.add(row["pid"])
                changed = True
    return [row for row in rows if row["pid"] in selected]


class _WindowsJob:
    """Minimal kill-on-close Job Object wrapper."""

    def __init__(self) -> None:
        self.handle: Any = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._kernel32 = kernel32
        self.handle = handle

    def assign(self, process: subprocess.Popen[Any]) -> None:
        if self.handle is None:
            return
        import ctypes
        if not self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):  # type: ignore[attr-defined]
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self, code: int = 1) -> None:
        if self.handle is not None:
            self._kernel32.TerminateJobObject(self.handle, code)

    def active_processes(self) -> int:
        if self.handle is None:
            return 0
        import ctypes
        from ctypes import wintypes

        class BASIC_ACCOUNTING(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        query = self._kernel32.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
        query.restype = wintypes.BOOL
        info = BASIC_ACCOUNTING()
        if not query(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return int(info.ActiveProcesses)

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class OwnedProcess:
    """A ``Popen``-compatible child with explicit descendant-tree ownership."""

    def __init__(self, process: subprocess.Popen[Any], job: _WindowsJob) -> None:
        self._process = process
        self._job = job
        self._closed = False
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def tree_active(self) -> bool:
        if os.name == "nt":
            return self._job.active_processes() > 0
        return self.poll() is None

    def communicate(self, *args: Any, **kwargs: Any) -> Any:
        return self._process.communicate(*args, **kwargs)

    def wait(self, timeout: Optional[float] = None) -> int:
        code = self._process.wait(timeout=timeout)
        self._close_job()
        return code

    def terminate_tree(self, exit_code: int = 1) -> None:
        with self._lock:
            if self._closed:
                return
            if os.name == "nt":
                self._job.terminate(exit_code)
            else:
                try:
                    os.killpg(self._process.pid, 15)
                except (OSError, ProcessLookupError):
                    self._process.terminate()

    def terminate(self) -> None:
        self.terminate_tree(1)

    def kill(self) -> None:
        self.terminate_tree(1)

    def _close_job(self) -> None:
        with self._lock:
            if not self._closed:
                self._job.close()
                self._closed = True

    def close(self) -> None:
        if self.poll() is None:
            self.terminate_tree()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._close_job()


def popen_headless(
    cmd: Sequence[str],
    *,
    detached: bool = False,
    cwd: Optional[str | os.PathLike[str]] = None,
    env: Optional[Dict[str, str]] = None,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    close_fds: bool = True,
    shell: bool = False,
    **kwargs: Any,
) -> OwnedProcess:
    """Spawn a headless child whose full tree belongs to the returned owner."""
    creationflags = kwargs.pop("creationflags", 0) | get_headless_creationflags(detached=detached)
    startupinfo = kwargs.pop("startupinfo", None) or get_headless_startupinfo()
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    job = _WindowsJob()
    process = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
        close_fds=close_fds, shell=shell, creationflags=creationflags,
        startupinfo=startupinfo, **kwargs,
    )
    try:
        job.assign(process)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        finally:
            job.close()
        raise
    return OwnedProcess(process, job)


def run_headless(
    cmd: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[str | os.PathLike[str]] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = False,
    text: bool = True,
    capture_output: bool = True,
    shell: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a bounded command; timeout always tears down its descendant tree."""
    if timeout is None or float(timeout) <= 0:
        raise ValueError("A positive explicit timeout is required")
    input_value = kwargs.pop("input", None)
    if input_value is not None:
        kwargs["stdin"] = subprocess.PIPE
    stdout = subprocess.PIPE if capture_output else kwargs.pop("stdout", None)
    stderr = subprocess.PIPE if capture_output else kwargs.pop("stderr", None)
    proc = popen_headless(
        cmd, cwd=cwd, env=env, stdin=kwargs.pop("stdin", subprocess.DEVNULL),
        stdout=stdout, stderr=stderr, shell=shell, text=text, **kwargs,
    )
    try:
        out, err = proc.communicate(input=input_value, timeout=float(timeout))
    except subprocess.TimeoutExpired as exc:
        proc.terminate_tree()
        out, err = proc.communicate()
        proc._close_job()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out or exc.output, stderr=err or exc.stderr) from None
    code = proc.returncode
    proc._close_job()
    result = subprocess.CompletedProcess(cmd, int(code or 0), out, err)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=out, stderr=err)
    return result


def launch_startup_authority(startup_script: os.PathLike[str] | str) -> None:
    """Launch the one top-level user startup authority without a console.

    The supervisor itself is deliberately not placed in a deployer's Job
    Object: it is the long-lived owner which places runtime, panel, and their
    descendants in owned jobs.  This is the sole production exception to
    per-command Job ownership and remains centralized here.
    """
    path = os.fspath(startup_script)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    subprocess.Popen(
        [path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
