"""Bounded canonical execution-base reconciliation for Runtime V1."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from aos.process_utils import run_headless

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

PHASE7_NODE2_R2_ACCEPTED_SHA = "1bc7cfddd4c07b448956521acc86a125bcdea80d"
PHASE7_NODE2_R1_ACCEPTED_SHA = "814e3ca0c09c3a484e20869f1a47a3545259f6db"
PHASE7_NODE2_R3_BOUND_STATUS = "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY"
PHASE7_NODE2_R2_GATE = "P7N2-DISCOVERY_MARKETPLACE_R2"
PHASE7_NODE2_R2_GATE_ALIASES = frozenset(
    {PHASE7_NODE2_R2_GATE, "P7N2-DISCOVERY-MARKETPLACE_R2"}
)


class CanonicalReconciliationError(RuntimeError):
    pass


def _run(cmd: Iterable[Any], *, cwd: Path, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    proc = run_headless(
        [str(x) for x in cmd],
        cwd=str(cwd),
        timeout=timeout,
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
    # 1. Prefer structured acceptance receipts if present
    try:
        from aos.acceptance_receipt import read_latest_acceptance_receipt
        receipt = read_latest_acceptance_receipt(control)
        if receipt and receipt.candidate_sha:
            sha = receipt.candidate_sha.lower()
            exists = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=product_workspace, check=False).returncode == 0
            if exists:
                slug = receipt.slice_id.replace("/", "-").replace(" ", "-").lower()
                rel_path = f"docs/project-control/acceptance-receipt-{slug}.json"
                return sha, rel_path, 999
    except Exception:
        pass

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


def _phase7_node2_r3_frontier(state: Mapping[str, Any], accepted_r2_sha: str) -> Dict[str, Any]:
    """Build the complete post-R2 operational frontier without rewriting R1 history."""
    value = copy.deepcopy(dict(state))

    next_product_action = value.get("next_product_action")
    accepted_chain = value.get("phase7_accepted_execution_chain")
    node2_contract = value.get("phase7_node2_contract")
    if not isinstance(next_product_action, dict):
        raise CanonicalReconciliationError("next_product_action is missing or invalid")
    if not isinstance(next_product_action.get("canonical_capability_order"), list):
        raise CanonicalReconciliationError("canonical_capability_order is missing or invalid")
    if not isinstance(accepted_chain, dict):
        raise CanonicalReconciliationError("phase7_accepted_execution_chain is missing or invalid")
    if not isinstance(node2_contract, dict):
        raise CanonicalReconciliationError("phase7_node2_contract is missing or invalid")
    delivery_slices = node2_contract.get("delivery_slices")
    if not isinstance(delivery_slices, dict):
        raise CanonicalReconciliationError("phase7_node2_contract.delivery_slices is missing or invalid")

    value["current_status"] = PHASE7_NODE2_R3_BOUND_STATUS
    value["current_milestone"] = "Program V2 Phase 7 — Node 2 Discovery Marketplace R3"
    value["next_action"] = (
        f"Implement Phase 7 Node 2 R3 — Customer-facing Discovery / Portfolio UI "
        f"from accepted R2 execution base {accepted_r2_sha} under DECISION-022. "
        "Consume accepted R1 server-authoritative Discovery Marketplace RPCs and R2 application service contracts. "
        "Production remains NO_GO."
    )
    value["next_action_execution_base_sha"] = accepted_r2_sha

    next_product_action["phase"] = "PHASE_7"
    next_product_action["node"] = "NODE_2_DISCOVERY_MARKETPLACE"
    next_product_action["slice"] = "R3_CUSTOMER_FACING_DISCOVERY_PORTFOLIO_UI"
    next_product_action["execution_base_sha"] = accepted_r2_sha

    candidate_release = value.get("candidate_release")
    if not isinstance(candidate_release, dict):
        raise CanonicalReconciliationError("candidate_release is missing or invalid")
    candidate_release["accepted_product_sha"] = accepted_r2_sha
    candidate_release["next_action_execution_base_sha"] = accepted_r2_sha

    accepted_chain["node2_r2"] = accepted_r2_sha
    node2_contract["status"] = "ACCEPTED_PROVEN"
    delivery_slices["R1"] = "ACCEPTED_PROVEN"
    delivery_slices["R2"] = "ACCEPTED_PROVEN"
    delivery_slices["R3"] = "BOUND_READY_FOR_IMPLEMENTATION"
    node2_contract["ui_v2"] = "RELEASED_ACTIVE"
    return value


def reconcile_phase7_node2_r2_frontier(
    state: Mapping[str, Any],
    *,
    project_id: str,
    latest_accepted_product_sha: str,
) -> Tuple[str, Dict[str, Any]]:
    """Repair only the exact, independently corroborated accepted-R2 partial frontier."""
    accepted_sha = PHASE7_NODE2_R2_ACCEPTED_SHA
    failures = []

    if project_id.strip().lower() != "lari":
        failures.append("project_id")
    if str(state.get("current_status") or "") != PHASE7_NODE2_R3_BOUND_STATUS:
        failures.append("current_status")
    if latest_accepted_product_sha.strip().lower() != accepted_sha:
        failures.append("latest_accepted_product_sha")
    if str(state.get("next_action_execution_base_sha") or "").strip().lower() != PHASE7_NODE2_R1_ACCEPTED_SHA:
        failures.append("next_action_execution_base_sha")

    candidate_release = state.get("candidate_release")
    if not isinstance(candidate_release, Mapping):
        failures.append("candidate_release")
    else:
        if str(candidate_release.get("accepted_product_sha") or "").strip().lower() != accepted_sha:
            failures.append("candidate_release.accepted_product_sha")
        if str(candidate_release.get("next_action_execution_base_sha") or "").strip().lower() != accepted_sha:
            failures.append("candidate_release.next_action_execution_base_sha")

    matching_gates = [
        gate
        for gate in (state.get("accepted_gates") or [])
        if isinstance(gate, Mapping)
        and str(gate.get("gate") or "").upper() in PHASE7_NODE2_R2_GATE_ALIASES
    ]
    if len(matching_gates) != 1:
        failures.append("accepted_gates.R2")
    else:
        gate = matching_gates[0]
        if str(gate.get("status") or "").upper() != "CLOSED_PROVEN":
            failures.append("accepted_gates.R2.status")
        if str(gate.get("tested_sha") or "").strip().lower() != accepted_sha:
            failures.append("accepted_gates.R2.tested_sha")

    next_action = str(state.get("next_action") or "")
    next_action_upper = next_action.upper()
    if "PHASE 7 NODE 2 R3" not in next_action_upper:
        failures.append("next_action.R3")
    if "CUSTOMER-FACING DISCOVERY / PORTFOLIO UI" not in next_action_upper:
        failures.append("next_action.customer_ui")
    if accepted_sha not in next_action.lower():
        failures.append("next_action.execution_base_sha")

    if failures:
        raise CanonicalReconciliationError(
            "Phase 7 Node 2 R2 frontier evidence mismatch: " + ", ".join(failures)
        )

    return "RECONCILED_PHASE7_NODE2_R2_FRONTIER", _phase7_node2_r3_frontier(state, accepted_sha)


def _ensure_control_clone(repository: str, control_ref: str, runtime_dir: Path) -> Tuple[Path, str]:
    reconciliation_root = (runtime_dir / "canonical-reconciliation").resolve()
    control = (reconciliation_root / "control").resolve()
    reconciliation_root.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{repository}.git"

    # This checkout is AOS-owned disposable scratch state, never the product
    # workspace or a user checkout. Re-materialize it for every bounded
    # reconciliation so a prior interrupted attempt cannot influence the next
    # fresh canonical read.
    if control.parent != reconciliation_root:
        raise CanonicalReconciliationError("Canonical scratch path escaped reconciliation root")
    if control.exists():
        shutil.rmtree(control)

    # `git clone --no-checkout` intentionally leaves the worktree empty. Such a
    # repository can appear dirty before checkout because tracked files are not
    # present. Therefore cleanliness is checked ONLY after the exact fetched
    # control SHA has been checked out.
    _run(["git", "clone", "--no-checkout", repo_url, str(control)], cwd=reconciliation_root, timeout=900)
    _git(["fetch", "origin", control_ref], cwd=control, timeout=900)
    remote_sha = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if not HEX40.fullmatch(remote_sha):
        raise CanonicalReconciliationError(f"Invalid fresh control SHA: {remote_sha!r}")

    _git(["checkout", "--detach", remote_sha], cwd=control)
    dirty = (_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=control).stdout or "").strip()
    if dirty:
        raise CanonicalReconciliationError(
            f"Canonical scratch checkout is dirty after exact checkout: {dirty[:1000]}"
        )
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
    except CanonicalReconciliationError as bind_exc:
        try:
            action, updated = reconcile_phase7_node2_r2_frontier(
                state,
                project_id=project_id,
                latest_accepted_product_sha=base_sha,
            )
        except CanonicalReconciliationError as frontier_exc:
            result = {
                "status": "HUMAN_REQUIRED",
                "reason": "CANONICAL_EXECUTION_BASE_CONFLICT",
                "detail": f"{bind_exc}; {frontier_exc}"[:1500],
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
    commit_message = (
        "docs(control-plane): reconcile accepted R2 operational frontier"
        if action == "RECONCILED_PHASE7_NODE2_R2_FRONTIER"
        else "docs(control-plane): bind autonomous execution base from accepted evidence"
    )
    _git(
        ["-c", "user.name=AOS Runtime", "-c", "user.email=aos-runtime@users.noreply.github.com",
         "commit", "-m", commit_message],
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
        "reason": (
            "PHASE7_NODE2_R2_FRONTIER_RECONCILED"
            if action == "RECONCILED_PHASE7_NODE2_R2_FRONTIER"
            else "MISSING_EXECUTION_BASE_BOUND_FROM_ACCEPTED_EVIDENCE"
        ),
        "control_sha_before": remote_before,
        "control_sha_after": new_control_sha,
        "accepted_execution_base_sha": base_sha,
        "evidence_source": evidence_source,
        "evidence_number": evidence_number,
        "state_path": expected_path,
        "push_mode": "FAST_FORWARD_NO_FORCE",
        "frontier_injected": action == "RECONCILED_PHASE7_NODE2_R2_FRONTIER",
        "run_plan_injected": False,
        "production": "NO_GO",
    }
    _atomic_json(runtime_dir / "canonical-repair.json", result)
    return result


def validate_canonical_coherence(state: Mapping[str, Any]) -> None:
    """Validate semantic agreement among canonical fields in STATE.json.

    Prevents contradictions such as status=R2 while next_action describes R1 work.
    """
    status = str(state.get("current_status") or "").upper()
    milestone = str(state.get("current_milestone") or "").upper()
    next_action = str(state.get("next_action") or "").upper()
    execution_base = str(state.get("canonical_refs", {}).get("program_v2_canonical_base", {}).get("sha") or "")

    # Contradiction: status is R2 or R3 but next_action still says R1
    if "R2" in status or "R3" in status or "R2" in milestone:
        if "IMPLEMENT PHASE 7 NODE 2 R1" in next_action or "NODE 2 R1" in next_action:
            raise CanonicalReconciliationError(
                f"Canonical semantic contradiction: status '{status}' contradicts next_action '{next_action[:60]}...'"
            )

    if "R3" in status:
        if "IMPLEMENT PHASE 7 NODE 2 R2" in next_action:
            raise CanonicalReconciliationError(
                f"Canonical semantic contradiction: status '{status}' contradicts next_action '{next_action[:60]}...'"
            )


def record_slice_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_evidence: Mapping[str, Any],
    slice_id: str,
    acceptance_receipt: Any,
) -> Dict[str, Any]:
    """Execute atomic forward-only canonical control-plane acceptance transition."""
    from aos.acceptance_receipt import write_acceptance_receipt

    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")

    control, control_sha = _ensure_control_clone(repository, control_ref, runtime_dir)
    state_path = find_state_json(control, project_id)
    current_state = _read_json(state_path)

    # 1. Validate semantic coherence before update
    validate_canonical_coherence(current_state)

    # 2. Build the complete operational frontier atomically. Historical R1
    # authority fields are retained by the narrowly scoped helper.
    updated_state = _phase7_node2_r3_frontier(current_state, candidate_sha)

    # Add to accepted_gates
    accepted_gates = list(updated_state.get("accepted_gates") or [])
    gate_record = {
        "gate": f"P7N2-{slice_id.upper()}",
        "status": "CLOSED_PROVEN",
        "evidence_level": "E2_EXECUTABLE_EXACT_SHA_CI",
        "tested_sha": candidate_sha,
        "run_ids": [str(ci_evidence.get("run_id") or "")],
        "closed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reopen_condition": "Failed contract verification or CI regression",
    }
    accepted_gates.append(gate_record)
    updated_state["accepted_gates"] = accepted_gates
    updated_state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    # 3. Write structured acceptance receipt
    write_acceptance_receipt(control, acceptance_receipt)

    # 4. Update capability registry if present
    registry_path = control / "docs" / "project-control" / "PROGRAM_V2_CAPABILITY_REGISTRY.json"
    if registry_path.is_file():
        try:
            reg = _read_json(registry_path)
            for item in reg.get("current_live_commercial_registry", []):
                if item.get("key") == "discovery_marketplace":
                    item["program_maturity"] = "REAL_CODE_NOT_LIVE_VERIFIED"
                    item["accepted_r2_sha"] = candidate_sha
                    item["delivery_slice"] = "R2_ACCEPTED_R3_UI_READY"
            _atomic_json(registry_path, reg)
            _git(["add", "--", str(registry_path)], cwd=control)
        except Exception:
            pass

    _atomic_json(state_path, updated_state)
    _git(["add", "--", str(state_path)], cwd=control)
    receipt_files = list((control / "docs" / "project-control").glob("acceptance-receipt-*.json"))
    for rf in receipt_files:
        _git(["add", "--", str(rf)], cwd=control)

    # 5. Verify only expected files changed
    diff_names = (_git(["diff", "--name-only", "--cached"], cwd=control).stdout or "").splitlines()
    for f in diff_names:
        if not f.startswith("docs/project-control/"):
            raise CanonicalReconciliationError(f"Unexpected file staged for canonical acceptance: {f}")

    # 6. Check for concurrent remote drift
    _git(["fetch", "origin", control_ref], cwd=control, timeout=60)
    remote_now = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    local_base = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip()
    if remote_now != local_base:
        raise CanonicalReconciliationError(
            f"Concurrent control drift detected: local {local_base} != remote {remote_now}"
        )

    # 7. Forward-only commit and fast-forward push
    msg = f"control(lari): reconcile Phase 7 Node 2 {slice_id.upper()} acceptance and bind R3"
    _git(
        ["-c", "user.name=AOS Canonical Reconciler", "-c", "user.email=aos-reconciler@users.noreply.github.com",
         "commit", "-m", msg],
        cwd=control,
    )
    new_control_sha = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip()
    _git(["push", "origin", f"HEAD:{control_ref}"], cwd=control, timeout=300)

    # 8. Verify remote SHA matches
    _git(["fetch", "origin", control_ref], cwd=control, timeout=60)
    remote_after = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if remote_after != new_control_sha:
        raise CanonicalReconciliationError(
            f"Remote SHA verification failed after push: expected {new_control_sha}, got {remote_after}"
        )

    # 9. Evaluate downstream dependency gates (e.g. UI V2 continue-61be4ab1af53cfa646d773ce)
    try:
        from aos.cross_lane_coordinator import evaluate_downstream_gates
        from aos.runtime_admission import CommandAdmissionStore
        admission_store = CommandAdmissionStore(runtime_dir.parent.parent if runtime_dir.name == "project-runtime" else runtime_dir)
        evaluate_downstream_gates(control, admission_store)
    except Exception:
        pass

    return {
        "status": "ACCEPTED",
        "slice_id": slice_id,
        "candidate_sha": candidate_sha,
        "control_transition_sha": new_control_sha,
        "next_status": updated_state["current_status"],
    }
