"""Targeted Windows regression tests for Job Object assignment and race guards."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from unittest import mock

import pytest

from aos.process_utils import _WindowsJob, popen_headless, process_alive


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_windows_job_assign_successful():
    """Verify standard live child assignment to a Windows Job Object succeeds."""
    job = _WindowsJob()
    assert job.handle is not None

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        job.assign(proc)
        # Should have at least 1 active process in job
        assert job.active_processes() >= 1
    finally:
        job.terminate(0)
        proc.wait(timeout=5)
        job.close()

    assert not process_alive(proc.pid)
    assert job.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_windows_job_assign_exited_child_before_assignment():
    """Verify that if the child already exited before assignment, it returns cleanly."""
    job = _WindowsJob()
    assert job.handle is not None

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=5)
    assert proc.poll() is not None

    # Child already terminated; assign must return safely without error
    job.assign(proc)
    job.close()
    assert job.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_windows_job_assign_exited_child_during_assignment_race():
    """Verify that if AssignProcessToJobObject fails because child exited during call, it is treated as harmless."""
    job = _WindowsJob()
    assert job.handle is not None

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.01)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def fake_assign(job_handle, proc_handle):
        # Wait for proc to exit to simulate race
        proc.wait(timeout=5)
        ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
        return 0

    with mock.patch.object(job._kernel32, "AssignProcessToJobObject", side_effect=fake_assign):
        # Must recognize terminated state and return cleanly
        job.assign(proc)

    job.close()
    assert proc.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_windows_job_assign_live_child_failure_fails_closed():
    """Verify that if process is STILL RUNNING and assignment fails, it raises OSError with actual error."""
    job = _WindowsJob()
    assert job.handle is not None

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        def fake_assign_fail(job_handle, proc_handle):
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
            return 0

        with mock.patch.object(job._kernel32, "AssignProcessToJobObject", side_effect=fake_assign_fail):
            with pytest.raises(OSError) as exc_info:
                job.assign(proc)
            assert exc_info.value.winerror == 5 or exc_info.value.errno == 5 or "AssignProcessToJobObject failed" in str(exc_info.value)
    finally:
        proc.kill()
        proc.wait(timeout=5)
        job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_popen_headless_fails_closed_and_kills_live_child_on_assignment_error():
    """Verify popen_headless kills unassigned live child and cleans up job handle on error."""
    # We patch _WindowsJob.assign to simulate a failure where the process is still running
    orig_assign = _WindowsJob.assign
    spawned_pids = []

    def fake_assign(self, proc):
        spawned_pids.append(proc.pid)
        raise OSError(5, "Simulated assignment failure")

    with mock.patch.object(_WindowsJob, "assign", fake_assign):
        with pytest.raises(OSError):
            popen_headless([sys.executable, "-c", "import time; time.sleep(10)"])

    assert len(spawned_pids) == 1
    # Process must have been killed by popen_headless error handler
    pid = spawned_pids[0]
    time.sleep(0.1)
    assert not process_alive(pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object specific tests")
def test_windows_job_cleanup_idempotent():
    """Verify job.close() is idempotent and cleans up handle."""
    job = _WindowsJob()
    assert job.handle is not None
    job.close()
    assert job.handle is None
    # Subsequent close must not raise
    job.close()
    assert job.handle is None


def test_posix_preservation():
    """Verify non-Windows / POSIX semantics are safely preserved."""
    with mock.patch("os.name", "posix"):
        job = _WindowsJob()
        assert job.handle is None
        # Operations on unassigned job must be safe no-ops
        job.assign(mock.MagicMock())
        job.terminate(1)
        assert job.active_processes() == 0
        job.close()
