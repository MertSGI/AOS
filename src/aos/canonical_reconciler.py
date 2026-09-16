"""Bounded canonical execution-base reconciliation for Runtime V1."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

HEX40 = re.compile(r"\b[0-9a-fA-F]{40}\b")
EVIDENCE_NUM = re.compile(r"\bEV[-_ ]?(\d{1,5})\b", re.I)
PRODUCT_SHA_LINE = re.compile(
    r"(?i)\b(?:product(?:_|\s|-)*(?:sha|head|commit(?:_sha)?)|"
    r"candidate(?:_|\s|-)*(?:sha|head)|executable(?:_|\s|-)*sha)\b"
    r"[^0-9a-fA-F]{0,40}([0-9a-fA-F]{40})\b"
)
_ACCEPTED_MARKERS = (
    "=PASS", "=ACCEPTED", "STATUS=ACCEPTED", "CONCLUSION=SUCCESS",
    '"STATUS": "ACCEPTED"', '"CONCLUSION": "SUCCESS"',
)


class CanonicalReconciliationError(RuntimeError):
    pass


def _run(cmd: Iterable[Any], *, cwd: Path, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(x) for x in cmd],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        shell=False,
    )
    if check and proc.returncode != 0:
        raise CanonicalReconciliationError(
            f"Command failed ({proc.returncode}): {' '.join(str(x) for x in cmd)} :: {(proc.stderr or '')[:1000]}"
        )
    return proc


def _git(args: Iterable[Any], *, cwd: Path, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, check=check, timeout=timeout)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _tracked_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def derive_latest_accepted_product_sha(control: Path, product_workspace: Path) -> Tuple[str, str, int]:
    files = [x.strip() for x in (_git(["ls-files"], cwd=control).stdout or "").splitlines() if x.strip()]
    candidates = []
    for rel in files:
        upper_path = rel.upper()
        if not any(token in upper_path for token in ("EVID", "EV-", "EV_", "ACCEPT", "STATE", "DECISION")):
            continue
        text = _tracked_text(control, rel)
        if not text:
            continue
        upper = text.upper()
        if not any(marker in upper for marker in _ACCEPTED_MARKERS):
            continue

        # Evidence files may contain multiple EV blocks. Associate each SHA
        # with the EV number in force at the line where the SHA appears instead
        # of assigning the file-wide maximum EV to every SHA.
        path_evs = [int(x) for x in EVIDENCE_NUM.findall(rel)]
        current_ev = max(path_evs) if path_evs else 0

        for line in text.splitlines():
            line_evs = [int(x) for x in EVIDENCE_NUM.findall(line)]
            if line_evs:
                current_ev = max(line_evs)

            explicit_line = PRODUCT_SHA_LINE.findall(line)
            if explicit_line:
                if not current_ev:
                    continue
                for sha in explicit_line:
                    sha = sha.lower()
                    exists = _git(
                        ["cat-file", "-e", f"{sha}^{{commit}}"],
                        cwd=product_workspace,
                        check=False,
                    ).returncode == 0
                    if exists:
                        candidates.append((current_ev, 2, sha, rel))
                continue

            # Fallback SHAs are considered only inside a numbered evidence
            # context and rank below explicit PRODUCT_SHA/CANDIDATE_SHA lines.
            if current_ev:
                for sha in HEX40.findall(line):
                    sha = sha.lower()
                    exists = _git(
                        ["cat-file", "-e", f"{sha}^{{commit}}"],
                        cwd=product_workspace,
                        check=False,
                    ).returncode == 0
                    if exists:
                        candidates.append((current_ev, 1, sha, rel))

    if not candidates:
        raise CanonicalReconciliationError(
            "Could not derive an accepted product execution-base SHA from canonical evidence"
        )

    candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    top_ev = candidates[0][0]
    top_rank = candidates[0][1]
    top = [row for row in candidates if row[0] == top_ev and row[1] == top_rank]
    distinct = sorted({row[2] for row in top})
    if len(distinct) != 1:
        raise CanonicalReconciliationError(
            f"Ambiguous latest accepted product SHA candidates at EV-{top_ev}: {distinct}"
        )
    sha = distinct[0]
    source = next(row[3] for row in top if row[2] == sha)
    return sha, source, top_ev


def find_state_json(control: Path, project_id: str) -> Path:
    candidates = []
    proc = _git(["ls-files", "*STATE*.json"], cwd=control, check=False)
    for rel in (proc.stdout or "").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        value = _read_json(control / rel)
        if not value:
            continue
        score = 0
        if "current_status" in value:
            score += 3
        if "current_milestone" in value:
            score += 3
        if "next_action" in value or "canonical_next_action" in value:
            score += 3
        if "canonical_refs" in value:
            score += 2
        if str(value.get("project_id", "")).lower() == str(project_id).lower():
            score += 5
        candidates.append((score, rel))
    if not candidates:
        raise CanonicalReconciliationError("Could not locate canonical STATE JSON")
    candidates.sort(reverse=True)
    if candidates[0][0] < 6:
        raise CanonicalReconciliationError(f"No sufficiently canonical STATE JSON candidate: {candidates[:5]}")
    return control / candidates[0][1]


def bind_missing_execution_base(state: Mapping[str, Any], base_sha: str) -> Tuple[str, Dict[str, Any]]:
    value = dict(state)
    existing = value.get("next_action_execution_base_sha")
    if existing:
        existing_sha = str(existing).strip().lower()
        if existing_sha == base_sha.lower():
            return "ALREADY_CURRENT", value
        raise CanonicalReconciliationError(
            f"Existing next_action_execution_base_sha conflicts with accepted evidence: {existing_sha} != {base_sha}"
        )
    value["next_action_execution_base_sha"] = base_sha
    if "updated_at" in value:
        value["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return "BOUND_MISSING_POINTER", value


def _ensure_control_clone(repository: str, control_ref: str, runtime_dir: Path) -> Tuple[Path, str]:
    reconciliation_root = runtime_dir / "canonical-reconciliation"
    control = reconciliation_root / "control"
    reconciliation_root.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{repository}.git"

    if not (control / ".git").is_dir():
        if control.exists():
            shutil.rmtree(control)
        _run(["git", "clone", "--no-checkout", repo_url, str(control)], cwd=reconciliation_root, timeout=900)

    _git(["remote", "set-url", "origin", repo_url], cwd=control)
    if (_git(["status", "--porcelain=v1"], cwd=control).stdout or "").strip():
        raise CanonicalReconciliationError("Canonical reconciliation control clone is dirty")

    _git(["fetch", "origin", control_ref], cwd=control, timeout=900)
    remote_sha = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if not HEX40.fullmatch(remote_sha):
        raise CanonicalReconciliationError(f"Invalid fresh control SHA: {remote_sha!r}")
    _git(["checkout", "--detach", remote_sha], cwd=control)
    if (_git(["status", "--porcelain=v1"], cwd=control).stdout or "").strip():
        raise CanonicalReconciliationError("Control checkout became dirty")
    return control, remote_sha


def reconcile_missing_execution_base(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
) -> Dict[str, Any]:
    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")

    if project_id.lower() != "lari":
        return {
            "status": "HUMAN_REQUIRED",
            "reason": "CANONICAL_RECONCILIATION_NOT_AUTHORIZED_FOR_PROJECT",
            "project_id": project_id,
        }
    if not repository or not control_ref:
        return {"status": "HUMAN_REQUIRED", "reason": "CANONICAL_RECONCILIATION_DESCRIPTOR_INCOMPLETE"}

    _git(["fetch", "origin", "--prune"], cwd=product_workspace, timeout=900)
    control, remote_before = _ensure_control_clone(repository, control_ref, runtime_dir)
    base_sha, evidence_source, evidence_number = derive_latest_accepted_product_sha(control, product_workspace)
    state_path = find_state_json(control, project_id)
    state = _read_json(state_path)

    try:
        action, updated = bind_missing_execution_base(state, base_sha)
    except CanonicalReconciliationError as exc:
        result = {
            "status": "HUMAN_REQUIRED",
            "reason": "CANONICAL_EXECUTION_BASE_CONFLICT",
            "detail": str(exc)[:1500],
            "control_sha_before": remote_before,
            "accepted_execution_base_sha": base_sha,
            "evidence_source": evidence_source,
            "evidence_number": evidence_number,
            "state_path": state_path.relative_to(control).as_posix(),
            "production": "NO_GO",
        }
        _atomic_json(runtime_dir / "canonical-repair.json", result)
        return result

    if action == "ALREADY_CURRENT":
        result = {
            "status": "NO_CHANGE",
            "reason": "CANONICAL_EXECUTION_BASE_ALREADY_CURRENT",
            "control_sha_before": remote_before,
            "accepted_execution_base_sha": base_sha,
            "evidence_source": evidence_source,
            "evidence_number": evidence_number,
            "state_path": state_path.relative_to(control).as_posix(),
            "production": "NO_GO",
        }
        _atomic_json(runtime_dir / "canonical-repair.json", result)
        return result

    _atomic_json(state_path, updated)
    _git(["diff", "--check"], cwd=control)
    changed = [
        x.strip().replace("\\", "/")
        for x in (_git(["diff", "--name-only"], cwd=control).stdout or "").splitlines()
        if x.strip()
    ]
    expected_path = state_path.relative_to(control).as_posix()
    if changed != [expected_path]:
        raise CanonicalReconciliationError(f"Unexpected canonical repair changed files: {changed}")

    _git(["fetch", "origin", control_ref], cwd=control, timeout=900)
    remote_now = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if remote_now != remote_before:
        result = {
            "status": "HUMAN_REQUIRED",
            "reason": "CANONICAL_CONTROL_DRIFT_DURING_REPAIR",
            "control_sha_before": remote_before,
            "control_sha_now": remote_now,
            "accepted_execution_base_sha": base_sha,
            "evidence_source": evidence_source,
            "state_path": expected_path,
            "production": "NO_GO",
        }
        _atomic_json(runtime_dir / "canonical-repair.json", result)
        return result

    _git(["add", "--", expected_path], cwd=control)
    _git(
        ["-c", "user.name=AOS Runtime", "-c", "user.email=aos-runtime@users.noreply.github.com",
         "commit", "-m", "docs(control-plane): bind autonomous execution base from accepted evidence"],
        cwd=control,
    )
    new_control_sha = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip()
    _git(["push", "origin", f"HEAD:{control_ref}"], cwd=control, timeout=900)

    _git(["fetch", "origin", control_ref], cwd=control, timeout=900)
    remote_after = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if remote_after != new_control_sha:
        raise CanonicalReconciliationError(
            f"Canonical repair push verification failed: expected {new_control_sha}, observed {remote_after}"
        )

    result = {
        "status": "APPLIED",
        "reason": "MISSING_EXECUTION_BASE_BOUND_FROM_ACCEPTED_EVIDENCE",
        "control_sha_before": remote_before,
        "control_sha_after": new_control_sha,
        "accepted_execution_base_sha": base_sha,
        "evidence_source": evidence_source,
        "evidence_number": evidence_number,
        "state_path": expected_path,
        "push_mode": "FAST_FORWARD_NO_FORCE",
        "frontier_injected": False,
        "run_plan_injected": False,
        "production": "NO_GO",
    }
    _atomic_json(runtime_dir / "canonical-repair.json", result)
    return result
