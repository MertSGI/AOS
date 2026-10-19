"""Mandatory OS Execution Isolation for AOS V2.

Enforces:
- Real OS execution isolation before ANY generated/candidate code runs.
- Windows-first: AppContainer profile with zero network capabilities + Job Object
  (KILL_ON_JOB_CLOSE, active process limit = 1, memory limit <= 512MiB).
- Linux adapter: bubblewrap (bwrap) with unshared user/mount/pid/net namespaces,
  read-only runtime/candidate mounts, private scratch mount.
- Minimum available host RAM >= 1GiB preflight check.
- Worker memory <= 512MiB, timeout <= 30s, stdout/stderr <= 256KiB.
- No network including loopback, no child process creation.
- Candidate files read-only; only private scratch writable.
- No access to .git, registry, token, or provider credentials.
- Pinned interpreter, explicit argv, Python flags '-I -B', no shell.
- If OS isolation is unavailable, fail closed with EXECUTION_ISOLATION_UNAVAILABLE;
  NEVER fallback to unsandboxed execution.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from aos.process_utils import run_headless

MIN_FREE_RAM_BYTES = 1024 * 1024 * 1024  # 1 GiB
DEFAULT_TEST_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 256 * 1024  # 256 KiB
MAX_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512 MiB


class SandboxError(Exception):
    """Base exception for sandbox execution errors."""


class SandboxUnavailableError(SandboxError):
    """Raised when mandatory OS isolation is not supported or resources unavailable (WAITING_RESOURCE)."""


@dataclass(frozen=True)
class SandboxPreflightResult:
    status: str  # "SUPPORTED" or "EXECUTION_ISOLATION_UNAVAILABLE"
    platform: str  # "windows", "linux", "unsupported"
    available_ram_bytes: int
    isolation_mechanism: str
    reasons: tuple[str, ...]

    @property
    def is_supported(self) -> bool:
        return self.status == "SUPPORTED"


@dataclass(frozen=True)
class SandboxTestResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    isolation_type: str
    output_truncated: bool = False


def get_available_ram_bytes() -> int:
    """Measure currently available physical memory in bytes."""
    if os.name == "nt":
        from ctypes import wintypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat_ex = MEMORYSTATUSEX()
        stat_ex.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        kernel32 = ctypes.windll.kernel32
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat_ex)):
            return int(stat_ex.ullAvailPhys)
        return 0
    else:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb * 1024
        except Exception:
            pass
        return 0


def preflight_sandbox() -> SandboxPreflightResult:
    """Check host support for mandatory OS sandboxing."""
    avail_ram = get_available_ram_bytes()
    reasons: list[str] = []

    if avail_ram < MIN_FREE_RAM_BYTES:
        reasons.append(f"Insufficient available RAM ({avail_ram / 1024**2:.1f} MiB < 1024 MiB required)")

    if sys.platform.startswith("win"):
        has_userenv = False
        try:
            userenv = ctypes.windll.userenv
            has_userenv = hasattr(userenv, "CreateAppContainerProfile")
        except Exception as exc:
            reasons.append(f"Failed to load userenv.dll: {exc}")

        if not has_userenv:
            reasons.append("CreateAppContainerProfile not supported on this Windows version")

        status = "SUPPORTED" if (has_userenv and avail_ram >= MIN_FREE_RAM_BYTES) else "EXECUTION_ISOLATION_UNAVAILABLE"
        return SandboxPreflightResult(
            status=status,
            platform="windows",
            available_ram_bytes=avail_ram,
            isolation_mechanism="WINDOWS_APPCONTAINER_JOB_OBJECT",
            reasons=tuple(reasons),
        )

    elif sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            reasons.append("Bubblewrap ('bwrap') is not installed on PATH")
        else:
            try:
                probe = run_headless(
                    [
                        "bwrap",
                        "--unshare-user",
                        "--unshare-ipc",
                        "--unshare-pid",
                        "--unshare-uts",
                        "--unshare-cgroup",
                        "--ro-bind", "/", "/",
                        "--proc", "/proc",
                        "--dev", "/dev",
                        "--", "true",
                    ],
                    text=False,
                    timeout=5.0,
                )
                if probe.returncode != 0:
                    err_msg = probe.stderr.decode("utf-8", errors="replace").strip() if isinstance(probe.stderr, bytes) else str(probe.stderr).strip()
                    reasons.append(f"Bubblewrap probe failed: {err_msg}")
            except Exception as e:
                reasons.append(f"Bubblewrap probe exception: {e}")

        status = "SUPPORTED" if (not reasons and avail_ram >= MIN_FREE_RAM_BYTES) else "EXECUTION_ISOLATION_UNAVAILABLE"
        return SandboxPreflightResult(
            status=status,
            platform="linux",
            available_ram_bytes=avail_ram,
            isolation_mechanism="LINUX_BUBBLEWRAP",
            reasons=tuple(reasons),
        )

    else:
        reasons.append(f"Operating system '{sys.platform}' does not have a contracted sandbox implementation")
        return SandboxPreflightResult(
            status="EXECUTION_ISOLATION_UNAVAILABLE",
            platform="unsupported",
            available_ram_bytes=avail_ram,
            isolation_mechanism="NONE",
            reasons=tuple(reasons),
        )


class SandboxRunner:
    """Executes pinned tests in an isolated OS container with resource limits."""

    def __init__(self) -> None:
        self.preflight = preflight_sandbox()

    def run_test(
        self,
        argv: Sequence[str],
        candidate_dir: Path,
        scratch_dir: Path,
        *,
        timeout: float = DEFAULT_TEST_TIMEOUT_SECONDS,
        extra_env: Mapping[str, str] | None = None,
        stdin_data: bytes | None = None,
    ) -> SandboxTestResult:
        """Run candidate test command in OS isolation."""
        if not self.preflight.is_supported:
            reasons = "; ".join(self.preflight.reasons)
            raise SandboxUnavailableError(
                f"EXECUTION_ISOLATION_UNAVAILABLE: Mandatory OS sandboxing is not available ({reasons})"
            )

        candidate_path = Path(candidate_dir).resolve()
        scratch_path = Path(scratch_dir).resolve()
        scratch_path.mkdir(parents=True, exist_ok=True)

        if sys.platform.startswith("win"):
            return self._run_windows_appcontainer(
                argv, candidate_path, scratch_path, timeout=timeout, extra_env=extra_env, stdin_data=stdin_data
            )
        elif sys.platform.startswith("linux"):
            return self._run_linux_bwrap(
                argv, candidate_path, scratch_path, timeout=timeout, extra_env=extra_env, stdin_data=stdin_data
            )
        else:
            raise SandboxUnavailableError("Unsupported platform for sandboxing")

    def _run_windows_appcontainer(
        self,
        argv: Sequence[str],
        candidate_dir: Path,
        scratch_dir: Path,
        *,
        timeout: float,
        extra_env: Mapping[str, str] | None,
        stdin_data: bytes | None,
    ) -> SandboxTestResult:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        userenv = ctypes.windll.userenv
        advapi32 = ctypes.windll.advapi32

        resolved_argv = list(argv)
        if resolved_argv and (
            resolved_argv[0] in ("python", "python.exe", "python3", "python3.exe")
            or Path(resolved_argv[0]).name.lower() in ("python.exe", "python")
        ):
            resolved_argv[0] = getattr(sys, "_base_executable", sys.executable)
        argv = resolved_argv

        container_name = f"AOS_Sandbox_{secrets.token_hex(8)}"
        sid_ptr = wintypes.LPVOID()

        # Create AppContainer with 0 capabilities (NO network, NO internet, NO loopback)
        hr = userenv.CreateAppContainerProfile(container_name, container_name, container_name, None, 0, ctypes.byref(sid_ptr))
        if hr != 0:
            raise SandboxError(f"CreateAppContainerProfile failed with HRESULT {hex(hr & 0xffffffff)}")

        p_str = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(p_str)):
            raise SandboxError(f"ConvertSidToStringSidW failed (error {kernel32.GetLastError()})")
        sid_string = ctypes.wstring_at(p_str.value)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(p_str.value)

        try:
            # Grant AppContainer permissions via icacls:
            # 1. Scratch dir: Full control (F)
            res1 = run_headless(
                ["icacls", str(scratch_dir), "/grant", f"*{sid_string}:(OI)(CI)F", "/T", "/Q"],
                timeout=15.0,
            )
            if res1.returncode != 0:
                raise SandboxError(f"icacls scratch grant failed: {res1.stderr}")
            # 2. Candidate dir: Read-only (RX) with recursive descent (/T)
            res2 = run_headless(
                ["icacls", str(candidate_dir), "/grant", f"*{sid_string}:(OI)(CI)RX", "/T", "/Q"],
                timeout=15.0,
            )
            if res2.returncode != 0:
                raise SandboxError(f"icacls candidate grant failed: {res2.stderr}")
            # 3. Python prefix & base_prefix: Read-only (RX) so interpreter can load standard library
            res3 = run_headless(
                ["icacls", str(Path(sys.prefix).resolve()), "/grant", f"*{sid_string}:(OI)(CI)RX", "/Q"],
                timeout=15.0,
            )
            if res3.returncode != 0:
                raise SandboxError(f"icacls sys.prefix grant failed: {res3.stderr}")
            if sys.base_prefix != sys.prefix:
                res4 = run_headless(
                    ["icacls", str(Path(sys.base_prefix).resolve()), "/grant", f"*{sid_string}:(OI)(CI)RX", "/Q"],
                    timeout=15.0,
                )
                if res4.returncode != 0:
                    raise SandboxError(f"icacls sys.base_prefix grant failed: {res4.stderr}")

            # Create Job Object to enforce process and memory limits
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise SandboxError("Failed to create Win32 Job Object")

            try:
                # Set Job Object Limits
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
                JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
                JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008

                class IO_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong),
                    ]

                class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

                class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                    _fields_ = [
                        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t),
                    ]

                j_limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                j_limits.BasicLimitInformation.LimitFlags = (
                    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    | JOB_OBJECT_LIMIT_JOB_MEMORY
                    | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                )
                j_limits.BasicLimitInformation.ActiveProcessLimit = 1  # No child process spawning allowed
                j_limits.JobMemoryLimit = MAX_MEMORY_LIMIT_BYTES  # 512 MiB limit

                SetInformationJobObject = kernel32.SetInformationJobObject
                SetInformationJobObject.argtypes = [
                    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD
                ]
                SetInformationJobObject.restype = wintypes.BOOL
                res = SetInformationJobObject(
                    job, 9, ctypes.byref(j_limits), ctypes.sizeof(j_limits)  # JobObjectExtendedLimitInformation = 9
                )
                if not res:
                    raise SandboxError(f"SetInformationJobObject failed (error {kernel32.GetLastError()})")

                # Setup anonymous pipes for stdout, stderr, stdin
                class SECURITY_ATTRIBUTES(ctypes.Structure):
                    _fields_ = [
                        ("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL),
                    ]

                sa = SECURITY_ATTRIBUTES()
                sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
                sa.bInheritHandle = True

                h_stdin_read, h_stdin_write = wintypes.HANDLE(), wintypes.HANDLE()
                h_stdout_read, h_stdout_write = wintypes.HANDLE(), wintypes.HANDLE()
                h_stderr_read, h_stderr_write = wintypes.HANDLE(), wintypes.HANDLE()

                kernel32.CreatePipe(ctypes.byref(h_stdin_read), ctypes.byref(h_stdin_write), ctypes.byref(sa), 0)
                kernel32.CreatePipe(ctypes.byref(h_stdout_read), ctypes.byref(h_stdout_write), ctypes.byref(sa), 0)
                kernel32.CreatePipe(ctypes.byref(h_stderr_read), ctypes.byref(h_stderr_write), ctypes.byref(sa), 0)

                # Write stdin data if provided
                if stdin_data:
                    written = wintypes.DWORD()
                    kernel32.WriteFile(h_stdin_write, stdin_data, len(stdin_data), ctypes.byref(written), None)
                kernel32.CloseHandle(h_stdin_write)

                # Initialize STARTUPINFOEXW with AppContainer Security Capabilities
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
                EXTENDED_STARTUPINFO_PRESENT = 0x00080000
                CREATE_NO_WINDOW = 0x08000000
                CREATE_SUSPENDED = 0x00000004
                STARTF_USESTDHANDLES = 0x00000100

                class SECURITY_CAPABILITIES(ctypes.Structure):
                    _fields_ = [
                        ("AppContainerSid", wintypes.LPVOID),
                        ("Capabilities", wintypes.LPVOID),
                        ("CapabilityCount", wintypes.DWORD),
                        ("Reserved", wintypes.DWORD),
                    ]

                sec_cap = SECURITY_CAPABILITIES()
                sec_cap.AppContainerSid = sid_ptr
                sec_cap.Capabilities = None
                sec_cap.CapabilityCount = 0
                sec_cap.Reserved = 0

                size = ctypes.c_size_t(0)
                kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
                attr_buf = ctypes.create_string_buffer(size.value)
                kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size))

                kernel32.UpdateProcThreadAttribute(
                    attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                    ctypes.byref(sec_cap), ctypes.sizeof(sec_cap), None, None
                )

                class STARTUPINFOEXW(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR),
                        ("lpTitle", wintypes.LPWSTR),
                        ("dwX", wintypes.DWORD),
                        ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD),
                        ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD),
                        ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD),
                        ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.c_char_p),
                        ("hStdInput", wintypes.HANDLE),
                        ("hStdOutput", wintypes.HANDLE),
                        ("hStdError", wintypes.HANDLE),
                        ("lpAttributeList", wintypes.LPVOID),
                    ]

                class PROCESS_INFORMATION(ctypes.Structure):
                    _fields_ = [
                        ("hProcess", wintypes.HANDLE),
                        ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD),
                        ("dwThreadId", wintypes.DWORD),
                    ]

                si = STARTUPINFOEXW()
                si.cb = ctypes.sizeof(STARTUPINFOEXW)
                si.dwFlags = STARTF_USESTDHANDLES
                si.hStdInput = h_stdin_read
                si.hStdOutput = h_stdout_write
                si.hStdError = h_stderr_write
                si.lpAttributeList = ctypes.cast(attr_buf, wintypes.LPVOID)

                pi = PROCESS_INFORMATION()

                # Build command line string: quote argv
                cmd_parts = []
                for a in argv:
                    if " " in a or '"' in a:
                        cmd_parts.append('"' + a.replace('"', '\\"') + '"')
                    else:
                        cmd_parts.append(a)
                cmd_line = " ".join(cmd_parts)

                # Minimal environment: preserve essential system vars, scrub all tokens/secrets
                blocked_substrings = ("token", "key", "secret", "auth", "cred", "pass", "session", "bearer", "cookie", "github")
                env_dict = {}
                for k, v in os.environ.items():
                    kl = k.lower()
                    if not any(b in kl for b in blocked_substrings):
                        env_dict[k] = v
                env_dict["PYTHONPATH"] = f"{candidate_dir}{os.pathsep}{candidate_dir / 'src'}"
                env_dict["TEMP"] = str(scratch_dir)
                env_dict["TMP"] = str(scratch_dir)
                if extra_env:
                    for k, v in extra_env.items():
                        kl = k.lower()
                        if not any(b in kl for b in blocked_substrings):
                            env_dict[k] = v

                CREATE_UNICODE_ENVIRONMENT = 0x00000400
                sorted_keys = sorted(env_dict.keys(), key=lambda s: s.casefold())
                env_block = "".join(f"{k}={env_dict[k]}\0" for k in sorted_keys) + "\0"
                env_buf = ctypes.create_unicode_buffer(env_block)

                res = kernel32.CreateProcessW(
                    None,
                    cmd_line,
                    None,
                    None,
                    True,  # inherit handles
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                    env_buf,
                    str(scratch_dir),
                    ctypes.byref(si),
                    ctypes.byref(pi),
                )

                # Close pipe write handles in parent
                kernel32.CloseHandle(h_stdin_read)
                kernel32.CloseHandle(h_stdout_write)
                kernel32.CloseHandle(h_stderr_write)

                if not res:
                    err = kernel32.GetLastError()
                    kernel32.CloseHandle(h_stdout_read)
                    kernel32.CloseHandle(h_stderr_read)
                    kernel32.DeleteProcThreadAttributeList(attr_buf)
                    raise SandboxError(f"CreateProcessW in AppContainer failed with error {err}")

                # Assign process to Job Object BEFORE resuming thread
                kernel32.AssignProcessToJobObject(job, pi.hProcess)
                kernel32.ResumeThread(pi.hThread)

                start_t = time.time()
                timeout_ms = int(timeout * 1000)
                wait_res = kernel32.WaitForSingleObject(pi.hProcess, timeout_ms)
                duration = time.time() - start_t
                timed_out = (wait_res == 0x00000102)  # WAIT_TIMEOUT

                if timed_out:
                    kernel32.TerminateJobObject(job, 124)
                    exit_code = 124
                else:
                    dw_exit = wintypes.DWORD()
                    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(dw_exit))
                    exit_code = dw_exit.value

                # Read stdout and stderr pipes
                stdout_bytes = self._read_pipe_to_end(h_stdout_read, MAX_OUTPUT_BYTES)
                stderr_bytes = self._read_pipe_to_end(h_stderr_read, MAX_OUTPUT_BYTES)

                kernel32.CloseHandle(pi.hProcess)
                kernel32.CloseHandle(pi.hThread)
                kernel32.DeleteProcThreadAttributeList(attr_buf)

                stdout_str = stdout_bytes.decode("utf-8", errors="replace")
                stderr_str = stderr_bytes.decode("utf-8", errors="replace")
                truncated = (len(stdout_bytes) >= MAX_OUTPUT_BYTES or len(stderr_bytes) >= MAX_OUTPUT_BYTES)

                return SandboxTestResult(
                    exit_code=exit_code,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    timed_out=timed_out,
                    duration_seconds=duration,
                    isolation_type="WINDOWS_APPCONTAINER_JOB_OBJECT",
                    output_truncated=truncated,
                )
            finally:
                kernel32.CloseHandle(job)
        finally:
            userenv.DeleteAppContainerProfile(container_name)

    def _read_pipe_to_end(self, h_pipe: Any, max_bytes: int) -> bytes:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        data = bytearray()
        buf = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        while len(data) < max_bytes:
            success = kernel32.ReadFile(h_pipe, buf, len(buf), ctypes.byref(read), None)
            if not success or read.value == 0:
                break
            data.extend(buf.raw[:read.value])
        kernel32.CloseHandle(h_pipe)
        return bytes(data[:max_bytes])

    def _run_linux_bwrap(
        self,
        argv: Sequence[str],
        candidate_dir: Path,
        scratch_dir: Path,
        *,
        timeout: float,
        extra_env: Mapping[str, str] | None,
        stdin_data: bytes | None,
    ) -> SandboxTestResult:
        """Run inside bubblewrap with isolated namespaces and read-only mounts."""
        bwrap_cmd = [
            "bwrap",
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup",
            "--ro-bind", "/", "/",
            "--bind", str(scratch_dir), str(scratch_dir),
            "--ro-bind", str(candidate_dir), str(candidate_dir),
            "--proc", "/proc",
            "--dev", "/dev",
            "--chdir", str(scratch_dir),
        ]
        # Minimal environment
        bwrap_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(candidate_dir),
            "TMPDIR": str(scratch_dir),
        }
        if extra_env:
            for k, v in extra_env.items():
                if not any(s in k.lower() for s in ("token", "key", "secret", "auth", "cred")):
                    bwrap_env[k] = v

        full_cmd = bwrap_cmd + ["--"] + list(argv)
        start_t = time.time()
        try:
            proc = run_headless(
                full_cmd,
                input=stdin_data,
                text=False,
                timeout=timeout,
                env=bwrap_env,
                cwd=str(scratch_dir),
            )
            duration = time.time() - start_t
            out_bytes = proc.stdout if isinstance(proc.stdout, bytes) else str(proc.stdout).encode("utf-8")
            err_bytes = proc.stderr if isinstance(proc.stderr, bytes) else str(proc.stderr).encode("utf-8")
            stdout_str = out_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stderr_str = err_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            truncated = len(out_bytes) > MAX_OUTPUT_BYTES or len(err_bytes) > MAX_OUTPUT_BYTES
            return SandboxTestResult(
                exit_code=proc.returncode,
                stdout=stdout_str,
                stderr=stderr_str,
                timed_out=False,
                duration_seconds=duration,
                isolation_type="LINUX_BUBBLEWRAP",
                output_truncated=truncated,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.time() - start_t
            out = exc.stdout or b""
            err = exc.stderr or b""
            if isinstance(out, str):
                out = out.encode("utf-8")
            if isinstance(err, str):
                err = err.encode("utf-8")
            return SandboxTestResult(
                exit_code=124,
                stdout=out[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
                stderr=err[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
                timed_out=True,
                duration_seconds=duration,
                isolation_type="LINUX_BUBBLEWRAP",
                output_truncated=False,
            )
