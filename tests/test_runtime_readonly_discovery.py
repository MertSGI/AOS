import subprocess
from pathlib import Path

from aos import planning_kernel


def test_missing_optional_readonly_executable_is_degradable(monkeypatch, tmp_path: Path):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(planning_kernel, "run_headless", missing)
    code, out, err = planning_kernel._run_readonly(["gh", "run", "list"], tmp_path)
    assert code == 127
    assert out == ""
    assert "find the file" in err.lower() or "no such file" in err.lower() or "belirtilen dosyayı" in err.lower()


def test_ci_discovery_does_not_crash_when_gh_is_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        planning_kernel,
        "_run_readonly",
        lambda cmd, cwd, timeout=30: (127, "", "executable missing"),
    )
    assert planning_kernel._ci_state("MertSGI/Randapp-main", "a" * 40, tmp_path) == []
