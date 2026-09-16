import subprocess
from pathlib import Path

import pytest

from aos.canonical_reconciler import (
    CanonicalReconciliationError,
    bind_missing_execution_base,
    derive_latest_accepted_product_sha,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True)
    return (proc.stdout or "").strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid", "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_bind_missing_execution_base_is_narrow():
    status, updated = bind_missing_execution_base({"project_id": "lari"}, "a" * 40)
    assert status == "BOUND_MISSING_POINTER"
    assert updated["next_action_execution_base_sha"] == "a" * 40
    assert "next_action" not in updated


def test_bind_missing_execution_base_refuses_conflict():
    with pytest.raises(CanonicalReconciliationError):
        bind_missing_execution_base({"next_action_execution_base_sha": "b" * 40}, "a" * 40)


def test_latest_accepted_product_sha_is_unique_latest_evidence(tmp_path: Path):
    product = tmp_path / "product"
    product.mkdir()
    _git(product, "init")
    sha1 = _commit(product, "one.txt", "1")
    sha2 = _commit(product, "two.txt", "2")

    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init")
    (control / "EVIDENCE.txt").write_text(
        f"EV-092 STATUS=ACCEPTED PRODUCT_SHA={sha1}\n"
        f"EV-093 STATUS=ACCEPTED PRODUCT_SHA={sha2}\n",
        encoding="utf-8",
    )
    evidence_text = (control / "EVIDENCE.txt").read_text(encoding="utf-8")
    assert evidence_text.splitlines()[0].startswith("EV-092 ")
    assert evidence_text.splitlines()[1].startswith("EV-093 ")
    assert "\\nEV-093" not in evidence_text

    _git(control, "add", "EVIDENCE.txt")
    _git(control, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid", "commit", "-m", "evidence")

    sha, source, ev = derive_latest_accepted_product_sha(control, product)
    assert sha == sha2
    assert source == "EVIDENCE.txt"
    assert ev == 93


def test_evidence_heading_applies_to_following_sha_line(tmp_path: Path):
    product = tmp_path / "product"
    product.mkdir()
    _git(product, "init")
    sha1 = _commit(product, "one.txt", "1")
    sha2 = _commit(product, "two.txt", "2")

    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init")
    (control / "EVIDENCE.txt").write_text(
        f"EV-092 STATUS=ACCEPTED\nPRODUCT_SHA={sha1}\n"
        f"EV-093 STATUS=ACCEPTED\nPRODUCT_SHA={sha2}\n",
        encoding="utf-8",
    )
    _git(control, "add", "EVIDENCE.txt")
    _git(control, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid", "commit", "-m", "evidence")

    sha, source, ev = derive_latest_accepted_product_sha(control, product)
    assert sha == sha2
    assert source == "EVIDENCE.txt"
    assert ev == 93


def test_same_latest_evidence_with_two_product_shas_remains_ambiguous(tmp_path: Path):
    product = tmp_path / "product"
    product.mkdir()
    _git(product, "init")
    sha1 = _commit(product, "one.txt", "1")
    sha2 = _commit(product, "two.txt", "2")

    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init")
    (control / "EV-093-EVIDENCE.txt").write_text(
        f"STATUS=ACCEPTED PRODUCT_SHA={sha1}\n"
        f"STATUS=ACCEPTED PRODUCT_SHA={sha2}\n",
        encoding="utf-8",
    )
    _git(control, "add", "EV-093-EVIDENCE.txt")
    _git(control, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid", "commit", "-m", "evidence")

    with pytest.raises(CanonicalReconciliationError):
        derive_latest_accepted_product_sha(control, product)


def test_fresh_no_checkout_clone_is_checked_only_after_exact_checkout(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _commit(source, "STATE.json", '{"current_status":"ACTIVE","current_milestone":"TEST","next_action":"continue"}')
    _git(source, "branch", "control/test")
    expected = _git(source, "rev-parse", "control/test")

    real_run = cr._run

    def redirected_run(cmd, *, cwd, check=True, timeout=300):
        cmd = list(cmd)
        if len(cmd) >= 5 and cmd[:3] == ["git", "clone", "--no-checkout"]:
            cmd[3] = str(source)
        return real_run(cmd, cwd=cwd, check=check, timeout=timeout)

    monkeypatch.setattr(cr, "_run", redirected_run)

    runtime_dir = tmp_path / "runtime"
    scratch = runtime_dir / "canonical-reconciliation" / "control"
    scratch.mkdir(parents=True)
    (scratch / "stale.txt").write_text("stale", encoding="utf-8")

    control, observed = cr._ensure_control_clone(
        "example/example",
        "control/test",
        runtime_dir,
    )

    assert observed == expected
    assert control == scratch.resolve()
    assert not (control / "stale.txt").exists()
    assert _git(control, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(control, "rev-parse", "HEAD") == expected
