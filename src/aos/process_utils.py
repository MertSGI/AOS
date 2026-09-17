"""Process execution helpers for headless, windowless operation.

Ensures that background tasks, supervisor workers, git queries, and CLI tools
run completely headless without creating transient console windows or stealing focus
on Windows, while preserving full diagnostic output and exit codes.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, Optional, Sequence


def get_headless_creationflags(*, detached: bool = False) -> int:
    """Return creationflags that suppress console window allocation on Windows."""
    if os.name != "nt":
        return 0
    flags = 0
    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    flags |= create_no_window
    if detached:
        detached_proc = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        new_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        flags |= (detached_proc | new_group)
    return flags


def get_headless_startupinfo() -> Optional[Any]:
    """Return STARTUPINFO configured with STARTF_USESHOWWINDOW and SW_HIDE on Windows."""
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return si


def run_headless(
    cmd: Sequence[str],
    *,
    cwd: Optional[str | os.PathLike[str]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    check: bool = False,
    text: bool = True,
    capture_output: bool = True,
    shell: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Execute a command synchronously with zero visible console window on Windows."""
    creationflags = kwargs.pop("creationflags", 0)
    creationflags |= get_headless_creationflags(detached=False)
    startupinfo = kwargs.pop("startupinfo", None) or get_headless_startupinfo()

    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        text=text,
        capture_output=capture_output,
        shell=shell,
        creationflags=creationflags,
        startupinfo=startupinfo,
        **kwargs,
    )


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
) -> subprocess.Popen[Any]:
    """Spawn a child process asynchronously with zero visible console window on Windows."""
    creationflags = kwargs.pop("creationflags", 0)
    creationflags |= get_headless_creationflags(detached=detached)
    startupinfo = kwargs.pop("startupinfo", None) or get_headless_startupinfo()

    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        close_fds=close_fds,
        shell=shell,
        creationflags=creationflags,
        startupinfo=startupinfo,
        **kwargs,
    )
