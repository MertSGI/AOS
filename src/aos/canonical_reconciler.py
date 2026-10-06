"""Bounded canonical execution-base reconciliation for Runtime V1."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import urllib.request
import urllib.error
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
PHASE7_NODE2_R3_ACCEPTED_SHA = "e6b30e0708aa2eb1597caa3155b3c3b3b3e9f0d6"
PHASE7_NODE2_R3_CONTROL_BASE_SHA = "8cb93243bf4097d4db1c40c532472ea260ad7813"
PHASE7_NODE2_R3_BRANCH = "feature/phase7-node2-discovery-marketplace-r3-review-20261004"
PHASE7_NODE2_R3_CI_RUN_ID = 37231913934
PHASE7_NODE2_R3_CONTROLLER_DECISION = "LARI-P7-N2-R3-ACCEPT-N3-PREBIND-20261005-01"
PHASE7_NODE2_R3_GATE = "P7N2-DISCOVERY-MARKETPLACE_R3"
PHASE7_NODE3_PREBIND_STATUS = "PHASE_7_NODE_3_FAVORITES_REBOOKING_PREBIND_REQUIRED"
PHASE7_NODE2_R3_ALLOWED_CONTROL_FILES = frozenset(
    {
        "docs/project-control/STATE.json",
        "docs/project-control/DECISIONS.md",
        "docs/project-control/PROGRAM_V2_CAPABILITY_REGISTRY.json",
        "docs/project-control/acceptance-receipt-discovery_marketplace_r3.json",
    }
)

PHASE7_NODE3_R1_ACCEPTED_SHA = "d52492b9de18070733c1a565fb691f68f564ba69"
PHASE7_NODE3_R1_EXECUTION_BASE_SHA = PHASE7_NODE2_R3_ACCEPTED_SHA
PHASE7_NODE3_R1_CONTROL_BASE_SHA = "e1dbec33f0b52af5cc53497dd3b44bf36063868a"
PHASE7_NODE3_R1_BRANCH = "aos/phase7-node3-favorites-fast-rebooking-r1"
PHASE7_NODE3_R1_CI_RUN_ID = 37417054448
PHASE7_NODE3_R1_CONTROLLER_AUTHORITY = "LARI-P7-N3-R1-ACCEPT-R2-AUTHORIZE-20261006-01"
PHASE7_NODE3_R1_GATE = "P7N3-FAVORITES-REBOOKING_R1"
PHASE7_NODE3_R2_AUTHORIZED_STATUS = (
    "PHASE_7_NODE_3_FAVORITES_REBOOKING_R2_PRODUCT_INTEGRATION_AUTHORIZED"
)
PHASE7_NODE3_R1_IMPLEMENTATION_RECEIPT_ID = (
    "b6a0fe6c4aa74f37f35b31917d128e8a35ebef9a7260abacdc36eb34af2fcbdb"
)
PHASE7_NODE3_R1_VERIFICATION_RECEIPT_ID = (
    "f1de50713911e73bc1b3a6af2aff27aa7847507cbaf6c256d0e37d2792e32359"
)
PHASE7_NODE3_R1_ALLOWED_CONTROL_FILES = frozenset(
    {
        "docs/project-control/STATE.json",
        "docs/project-control/DECISIONS.md",
        "docs/project-control/PROGRAM_V2_CAPABILITY_REGISTRY.json",
        "docs/project-control/acceptance-receipt-favorites_rebooking_r1.json",
    }
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


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _tracked_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def derive_latest_accepted_product_sha(
    control: Path,
    product_workspace: Path,
    project_id: Optional[str] = None,
    lane: Optional[str] = None,
) -> Tuple[str, str, int]:
    # 1. Inspect structured acceptance receipts if present
    from aos.acceptance_receipt import read_all_acceptance_receipts

    receipts = read_all_acceptance_receipts(
        control,
        project_id=project_id,
        lane=lane,
        require_accepted=False,
    )
    if receipts:
        # Reject non-accepted receipts from becoming the frontier
        accepted_receipts = [r for r in receipts if r.acceptance_result == "ACCEPTED"]
        if not accepted_receipts:
            raise CanonicalReconciliationError(
                "No valid ACCEPTED structured acceptance receipts found; all candidates are REJECTED or invalid"
            )

        # Check for wrong-project / wrong-lane if bound
        if project_id is not None:
            for r in accepted_receipts:
                if r.project_id != project_id:
                    raise CanonicalReconciliationError(
                        f"Receipt project_id mismatch: expected {project_id}, got {r.project_id}"
                    )
        if lane is not None:
            for r in accepted_receipts:
                if r.lane != lane:
                    raise CanonicalReconciliationError(
                        f"Receipt lane mismatch: expected {lane}, got {r.lane}"
                    )

        # Verify candidate exists in product workspace
        valid_candidates = []
        for r in accepted_receipts:
            sha = r.candidate_sha.lower()
            exists = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=product_workspace, check=False).returncode == 0
            if exists:
                valid_candidates.append(r)

        if not valid_candidates:
            raise CanonicalReconciliationError(
                "None of the ACCEPTED receipt candidate SHAs exist in the product workspace"
            )

        # Linear succession check:
        # Timestamp alone is never authority.
        # Find terminal candidate(s) that are not the execution_base_sha of any other valid candidate receipt.
        execution_bases = {r.execution_base_sha.lower() for r in valid_candidates}
        terminals = [r for r in valid_candidates if r.candidate_sha.lower() not in execution_bases]

        if len(terminals) == 1:
            terminal = terminals[0]
            sha = terminal.candidate_sha.lower()
            slug = terminal.slice_id.replace("/", "-").replace(" ", "-").lower()
            rel_path = f"docs/project-control/acceptance-receipt-{slug}.json"
            return sha, rel_path, 999
        elif len(terminals) > 1:
            # Ambiguity => AMBIGUOUS_ACCEPTED_FRONTIER / fail closed
            candidates_str = sorted({t.candidate_sha.lower() for t in terminals})
            raise CanonicalReconciliationError(
                f"AMBIGUOUS_ACCEPTED_FRONTIER: Multiple terminal accepted receipt candidates: {candidates_str}"
            )
        else:
            # Cycle or ambiguity
            raise CanonicalReconciliationError(
                "AMBIGUOUS_ACCEPTED_FRONTIER: Cyclic or ambiguous receipt succession chain"
            )


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
    base_sha, evidence_source, evidence_number = derive_latest_accepted_product_sha(
        control, product_workspace, project_id
    )
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


def _read_github_actions_run(repository: str, run_id: int) -> Dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aos-canonical-reconciler",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_headers = exc.headers or {}
        if exc.code == 401:
            classification = "GITHUB_AUTHENTICATION_FAILED"
        elif exc.code == 429 or (
            exc.code == 403
            and (
                str(response_headers.get("X-RateLimit-Remaining") or "") == "0"
                or bool(response_headers.get("Retry-After"))
            )
        ):
            classification = "GITHUB_RATE_LIMITED"
        elif exc.code == 403:
            classification = "GITHUB_FORBIDDEN"
        else:
            classification = f"GITHUB_HTTP_{exc.code}"
        raise CanonicalReconciliationError(
            f"Could not independently read GitHub Actions run {run_id}: {classification}"
        ) from exc
    except Exception as exc:
        raise CanonicalReconciliationError(
            f"Could not independently read GitHub Actions run {run_id}: "
            f"GITHUB_TRANSPORT_{type(exc).__name__.upper()}"
        ) from exc
    if not isinstance(value, dict):
        raise CanonicalReconciliationError(f"Invalid GitHub Actions run response for {run_id}")
    return value


def _assert_canonical_acceptance_kcp_coverage(
    descriptor: Mapping[str, Any],
    *,
    candidate_sha: str,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Fail closed before a canonical control checkout can be mutated."""
    from aos.knowledge.accepted_work import (
        AcceptedWorkCoverageError,
        assert_accepted_work_receipted,
    )
    from aos.knowledge.hooks import resolve_knowledge_ledger

    project_id = str(descriptor.get("project_id") or "")
    if not project_id:
        raise CanonicalReconciliationError("KCP acceptance project identity is missing")
    ledger = resolve_knowledge_ledger(
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )
    if ledger is None:
        raise CanonicalReconciliationError("KCP runtime home not configured")
    try:
        return assert_accepted_work_receipted(
            ledger,
            project_id=project_id,
            result_sha=candidate_sha.lower(),
        )
    except AcceptedWorkCoverageError as exc:
        raise CanonicalReconciliationError(str(exc)) from exc
    except Exception as exc:
        raise CanonicalReconciliationError(
            f"KCP_ACCEPTED_WORK_COVERAGE_UNAVAILABLE:{project_id}:{candidate_sha.lower()}:"
            f"{type(exc).__name__}"
        ) from exc


def _remote_branch_sha(product_workspace: Path, branch: str) -> str:
    proc = _git(
        ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=product_workspace,
        timeout=120,
    )
    rows = [line.split() for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) < 2 or not HEX40.fullmatch(rows[0][0]):
        raise CanonicalReconciliationError(f"Could not resolve one exact remote head for {branch}")
    return rows[0][0].lower()


def _phase7_node2_r3_accepted_state(
    state: Mapping[str, Any],
    *,
    candidate_sha: str,
    execution_base_sha: str,
    ci_run_id: int,
    ci_conclusion: str,
    controller_authority: str,
) -> Dict[str, Any]:
    """Build only the authorized Node 2 R3 acceptance -> Node 3 prebind frontier."""
    failures = []
    if candidate_sha.lower() != PHASE7_NODE2_R3_ACCEPTED_SHA:
        failures.append("candidate_sha")
    if execution_base_sha.lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
        failures.append("execution_base_sha")
    if int(ci_run_id) != PHASE7_NODE2_R3_CI_RUN_ID:
        failures.append("ci_run_id")
    if ci_conclusion.lower() != "success":
        failures.append("ci_conclusion")
    if controller_authority != PHASE7_NODE2_R3_CONTROLLER_DECISION:
        failures.append("controller_authority")
    if str(state.get("current_status") or "") != PHASE7_NODE2_R3_BOUND_STATUS:
        failures.append("current_status")
    if str(state.get("production_status") or "") != "NO_GO":
        failures.append("production_status")
    if str(state.get("next_action_execution_base_sha") or "").lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
        failures.append("next_action_execution_base_sha")

    candidate_release = state.get("candidate_release")
    if not isinstance(candidate_release, Mapping):
        failures.append("candidate_release")
    else:
        if str(candidate_release.get("accepted_product_sha") or "").lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
            failures.append("candidate_release.accepted_product_sha")
        if str(candidate_release.get("next_action_execution_base_sha") or "").lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
            failures.append("candidate_release.next_action_execution_base_sha")

    chain = state.get("phase7_accepted_execution_chain")
    if not isinstance(chain, Mapping):
        failures.append("phase7_accepted_execution_chain")
    else:
        if str(chain.get("node2_r1") or "").lower() != PHASE7_NODE2_R1_ACCEPTED_SHA:
            failures.append("phase7_accepted_execution_chain.node2_r1")
        if str(chain.get("node2_r2") or "").lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
            failures.append("phase7_accepted_execution_chain.node2_r2")
        if chain.get("node2_r3"):
            failures.append("phase7_accepted_execution_chain.node2_r3")

    contract = state.get("phase7_node2_contract")
    delivery_slices = contract.get("delivery_slices") if isinstance(contract, Mapping) else None
    if not isinstance(contract, Mapping) or not isinstance(delivery_slices, Mapping):
        failures.append("phase7_node2_contract")
    else:
        if str(contract.get("execution_base_sha") or "").lower() != PHASE7_NODE2_R1_ACCEPTED_SHA:
            failures.append("phase7_node2_contract.execution_base_sha")
        r1_authority = contract.get("r1_server_authority")
        if not isinstance(r1_authority, Mapping) or str(r1_authority.get("product_sha") or "").lower() != PHASE7_NODE2_R1_ACCEPTED_SHA:
            failures.append("phase7_node2_contract.r1_server_authority.product_sha")
        if delivery_slices.get("R2") != "ACCEPTED_PROVEN":
            failures.append("phase7_node2_contract.delivery_slices.R2")
        if delivery_slices.get("R3") != "BOUND_READY_FOR_IMPLEMENTATION":
            failures.append("phase7_node2_contract.delivery_slices.R3")
        if contract.get("ui_v2") != "RELEASED_ACTIVE":
            failures.append("phase7_node2_contract.ui_v2")
        if contract.get("node3") != "NOT_IN_SCOPE":
            failures.append("phase7_node2_contract.node3")

    next_product_action = state.get("next_product_action")
    if not isinstance(next_product_action, Mapping):
        failures.append("next_product_action")
    else:
        expected = {
            "phase": "PHASE_7",
            "node": "NODE_2_DISCOVERY_MARKETPLACE",
            "slice": "R3_CUSTOMER_FACING_DISCOVERY_PORTFOLIO_UI",
            "execution_base_sha": PHASE7_NODE2_R2_ACCEPTED_SHA,
        }
        for key, expected_value in expected.items():
            if next_product_action.get(key) != expected_value:
                failures.append(f"next_product_action.{key}")

    gates = state.get("accepted_gates")
    if not isinstance(gates, list):
        failures.append("accepted_gates")
    elif any(
        isinstance(gate, Mapping)
        and str(gate.get("gate") or "").upper() == PHASE7_NODE2_R3_GATE
        for gate in gates
    ):
        failures.append("accepted_gates.R3_duplicate")

    if failures:
        raise CanonicalReconciliationError(
            "Phase 7 Node 2 R3 acceptance precondition mismatch: " + ", ".join(failures)
        )

    value = copy.deepcopy(dict(state))
    value["current_status"] = PHASE7_NODE3_PREBIND_STATUS
    value["current_milestone"] = "Program V2 Phase 7 — Node 3 Favorites & Fast Rebooking Prebind"
    value["next_action"] = (
        "Research and bind the Phase 7 Node 3 Favorites & Fast Rebooking execution contract "
        f"from accepted Node 2 R3 base {PHASE7_NODE2_R3_ACCEPTED_SHA} before any implementation. "
        "Node 3 implementation is NOT authorized. Production remains NO_GO."
    )
    value["next_action_execution_base_sha"] = PHASE7_NODE2_R3_ACCEPTED_SHA
    value["candidate_release"]["accepted_product_sha"] = PHASE7_NODE2_R3_ACCEPTED_SHA
    value["candidate_release"]["next_action_execution_base_sha"] = PHASE7_NODE2_R3_ACCEPTED_SHA
    value["phase7_accepted_execution_chain"]["node2_r3"] = PHASE7_NODE2_R3_ACCEPTED_SHA
    value["phase7_node2_contract"]["delivery_slices"]["R3"] = "ACCEPTED_PROVEN"
    value["next_product_action"].update(
        {
            "phase": "PHASE_7",
            "node": "NODE_3_FAVORITES_FAST_REBOOKING",
            "slice": "PREBIND_REQUIRED",
            "execution_base_sha": PHASE7_NODE2_R3_ACCEPTED_SHA,
        }
    )
    value["accepted_gates"].append(
        {
            "gate": PHASE7_NODE2_R3_GATE,
            "status": "CLOSED_PROVEN",
            "evidence_level": "E2_EXECUTABLE_EXACT_SHA_CI",
            "tested_sha": PHASE7_NODE2_R3_ACCEPTED_SHA,
            "run_ids": [str(PHASE7_NODE2_R3_CI_RUN_ID)],
            "closed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reopen_condition": "Failed exact-SHA R3 contract verification or CI regression",
        }
    )
    value["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return value


def _phase7_node2_r3_accepted_registry(registry: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(registry))
    capabilities = value.get("additional_program_capabilities")
    if not isinstance(capabilities, list):
        raise CanonicalReconciliationError("additional_program_capabilities is missing or invalid")
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in capabilities:
        if isinstance(item, dict) and item.get("key") in {
            "verified_reviews", "discovery_marketplace", "favorites_rebooking"
        }:
            key = str(item["key"])
            if key in by_key:
                raise CanonicalReconciliationError(f"Duplicate capability registry key: {key}")
            by_key[key] = item
    required = {"verified_reviews", "discovery_marketplace", "favorites_rebooking"}
    if set(by_key) != required:
        raise CanonicalReconciliationError(f"Missing Phase 7 capability registry keys: {sorted(required - set(by_key))}")
    favorites = by_key["favorites_rebooking"]
    if favorites.get("source_state") != "PLANNED" or favorites.get("program_maturity") != "PLANNED":
        raise CanonicalReconciliationError("favorites_rebooking is not still PLANNED")

    for key in ("verified_reviews", "discovery_marketplace"):
        by_key[key]["source_state"] = "LIVE_ACCEPTANCE_ONLY"
        by_key[key]["program_maturity"] = "REAL_CODE_NOT_LIVE_VERIFIED"
    discovery = by_key["discovery_marketplace"]
    discovery["accepted_r3_sha"] = PHASE7_NODE2_R3_ACCEPTED_SHA
    discovery["accepted_r3_ci_run_id"] = PHASE7_NODE2_R3_CI_RUN_ID
    discovery["delivery_slice"] = "R3_ACCEPTED_NODE3_PREBIND"
    return value


def _decision_023_text(existing: str) -> str:
    if "DECISION-023" in existing:
        raise CanonicalReconciliationError("DECISION-023 already exists")
    block = (
        "## DECISION-023: Phase 7 Node 2 R3 Functional Acceptance and Node 3 Prebind Boundary\n"
        "- **Status**: ACCEPTED\n"
        f"- **Authority**: `{PHASE7_NODE2_R3_CONTROLLER_DECISION}`.\n"
        f"- **Decision**: Phase 7 Node 2 R3 functional Discovery / Portfolio UI is accepted at exact product SHA `{PHASE7_NODE2_R3_ACCEPTED_SHA}` from execution base `{PHASE7_NODE2_R2_ACCEPTED_SHA}`, with exact-SHA GitHub Actions Run `{PHASE7_NODE2_R3_CI_RUN_ID}` completed successfully. DECISION-022 remains the Node 2 server-authoritative architecture authority. This functional acceptance does not close broader UI-V2 visual or productization work; real-browser, visual, and productization evidence remains separate. Node 3 Favorites & Fast Rebooking is downstream, its implementation is not authorized, and Controller PREBIND is required first. Production remains `NO_GO`.\n"
    )
    return existing.rstrip() + "\n\n" + block


def _phase7_node3_r1_accepted_state(
    state: Mapping[str, Any],
    *,
    candidate_sha: str,
    execution_base_sha: str,
    ci_run_id: int,
    ci_conclusion: str,
    controller_authority: str,
) -> Dict[str, Any]:
    """Build only the authorized Node 3 R1 acceptance -> R2 frontier."""
    failures = []
    if candidate_sha.lower() != PHASE7_NODE3_R1_ACCEPTED_SHA:
        failures.append("candidate_sha")
    if execution_base_sha.lower() != PHASE7_NODE3_R1_EXECUTION_BASE_SHA:
        failures.append("execution_base_sha")
    if int(ci_run_id) != PHASE7_NODE3_R1_CI_RUN_ID:
        failures.append("ci_run_id")
    if ci_conclusion.lower() != "success":
        failures.append("ci_conclusion")
    if controller_authority != PHASE7_NODE3_R1_CONTROLLER_AUTHORITY:
        failures.append("controller_authority")
    if str(state.get("current_status") or "") != PHASE7_NODE3_PREBIND_STATUS:
        failures.append("current_status")
    if str(state.get("production_status") or "") != "NO_GO":
        failures.append("production_status")
    if str(state.get("next_action_execution_base_sha") or "").lower() != PHASE7_NODE3_R1_EXECUTION_BASE_SHA:
        failures.append("next_action_execution_base_sha")

    candidate_release = state.get("candidate_release")
    if not isinstance(candidate_release, Mapping):
        failures.append("candidate_release")
    else:
        if str(candidate_release.get("accepted_product_sha") or "").lower() != PHASE7_NODE3_R1_EXECUTION_BASE_SHA:
            failures.append("candidate_release.accepted_product_sha")
        if str(candidate_release.get("next_action_execution_base_sha") or "").lower() != PHASE7_NODE3_R1_EXECUTION_BASE_SHA:
            failures.append("candidate_release.next_action_execution_base_sha")

    chain = state.get("phase7_accepted_execution_chain")
    expected_chain = {
        "node2_r1": PHASE7_NODE2_R1_ACCEPTED_SHA,
        "node2_r2": PHASE7_NODE2_R2_ACCEPTED_SHA,
        "node2_r3": PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
    }
    if not isinstance(chain, Mapping):
        failures.append("phase7_accepted_execution_chain")
    else:
        for key, expected in expected_chain.items():
            if str(chain.get(key) or "").lower() != expected:
                failures.append(f"phase7_accepted_execution_chain.{key}")
        if chain.get("node3_r1"):
            failures.append("phase7_accepted_execution_chain.node3_r1")

    node2_contract = state.get("phase7_node2_contract")
    node2_slices = node2_contract.get("delivery_slices") if isinstance(node2_contract, Mapping) else None
    if not isinstance(node2_contract, Mapping) or not isinstance(node2_slices, Mapping):
        failures.append("phase7_node2_contract")
    else:
        if node2_slices.get("R1") != "ACCEPTED_PROVEN":
            failures.append("phase7_node2_contract.delivery_slices.R1")
        if node2_slices.get("R2") != "ACCEPTED_PROVEN":
            failures.append("phase7_node2_contract.delivery_slices.R2")
        if node2_slices.get("R3") != "ACCEPTED_PROVEN":
            failures.append("phase7_node2_contract.delivery_slices.R3")
        if node2_contract.get("ui_v2") != "RELEASED_ACTIVE":
            failures.append("phase7_node2_contract.ui_v2")

    next_product_action = state.get("next_product_action")
    if not isinstance(next_product_action, Mapping):
        failures.append("next_product_action")
    else:
        expected_action = {
            "phase": "PHASE_7",
            "node": "NODE_3_FAVORITES_FAST_REBOOKING",
            "slice": "PREBIND_REQUIRED",
            "execution_base_sha": PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        }
        for key, expected in expected_action.items():
            if next_product_action.get(key) != expected:
                failures.append(f"next_product_action.{key}")

    gates = state.get("accepted_gates")
    if not isinstance(gates, list):
        failures.append("accepted_gates")
    elif any(
        isinstance(gate, Mapping)
        and str(gate.get("gate") or "").upper() == PHASE7_NODE3_R1_GATE
        for gate in gates
    ):
        failures.append("accepted_gates.Node3_R1_duplicate")

    if state.get("phase7_node3_contract"):
        failures.append("phase7_node3_contract_duplicate")
    if failures:
        raise CanonicalReconciliationError(
            "Phase 7 Node 3 R1 acceptance precondition mismatch: " + ", ".join(failures)
        )

    value = copy.deepcopy(dict(state))
    value["current_status"] = PHASE7_NODE3_R2_AUTHORIZED_STATUS
    value["current_milestone"] = (
        "Program V2 Phase 7 — Node 3 Favorites & Fast Rebooking R2 Product Integration"
    )
    value["next_action"] = (
        "Implement bounded Phase 7 Node 3 Favorites & Fast Rebooking R2 Product Integration "
        f"from accepted R1 execution base {PHASE7_NODE3_R1_ACCEPTED_SHA}. Fast rebooking must use "
        "the existing appointment manage-token surface and the R1 current-truth seed; the user must "
        "select current availability through evaluate_booking_slot and final creation must use "
        "create_public_booking, with no direct appointment insert or historical price/duration authority. "
        "Favorites must use the R1 RPCs and real Supabase authenticated identity; lari_customer_auth, "
        "browser localStorage, raw email, and raw phone are not authorization. If an authenticated customer "
        "session is unavailable, hold only that favorites-auth surface; OTP, magic-link, or a new customer-auth "
        "architecture is not authorized. UI-V2 remains an independent lane. Production remains NO_GO."
    )
    value["next_action_execution_base_sha"] = PHASE7_NODE3_R1_ACCEPTED_SHA
    value["candidate_release"]["accepted_product_sha"] = PHASE7_NODE3_R1_ACCEPTED_SHA
    value["candidate_release"]["next_action_execution_base_sha"] = PHASE7_NODE3_R1_ACCEPTED_SHA
    value["phase7_accepted_execution_chain"]["node3_r1"] = PHASE7_NODE3_R1_ACCEPTED_SHA
    value["phase7_node3_contract"] = {
        "contract_id": "LARI-P7-N3-FAVORITES-REBOOKING-R1",
        "authority": "DECISION-024",
        "status": "R1_ACCEPTED_PROVEN",
        "execution_base_sha": PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        "r1_server_foundation": {
            "status": "ACCEPTED_PROVEN",
            "product_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
            "ci_run_id": PHASE7_NODE3_R1_CI_RUN_ID,
        },
        "architecture": {
            "favorites_model": "AUTH_UID_RELATION_TO_CANONICAL_TENANT",
            "duplicate_business_truth_allowed": False,
            "duplicate_customer_truth_allowed": False,
            "favorites_identity_authority": "SUPABASE_AUTH_UID",
            "localstorage_customer_auth_authority_allowed": False,
            "raw_email_phone_authority_allowed": False,
            "fast_rebooking_model": "MANAGE_TOKEN_AUTHORIZED_CURRENT_TRUTH_SEED",
            "fast_rebooking_ownership_authority": "EXISTING_APPOINTMENT_MANAGE_TOKEN",
            "historical_appointment_is_current_truth": False,
            "canonical_availability_authority": "evaluate_booking_slot",
            "canonical_booking_authority": "create_public_booking",
            "direct_appointment_insert_allowed": False,
        },
        "delivery_slices": {"R1": "ACCEPTED_PROVEN", "R2": "AUTHORIZED"},
        "production": "NO_GO",
    }
    value["next_product_action"].update(
        {
            "phase": "PHASE_7",
            "node": "NODE_3_FAVORITES_FAST_REBOOKING",
            "slice": "R2_PRODUCT_INTEGRATION_AUTHORIZED",
            "execution_base_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        }
    )
    value["accepted_gates"].append(
        {
            "gate": PHASE7_NODE3_R1_GATE,
            "status": "CLOSED_PROVEN",
            "evidence_level": "E2_EXECUTABLE_EXACT_SHA_CI",
            "tested_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
            "run_ids": [str(PHASE7_NODE3_R1_CI_RUN_ID)],
            "closed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reopen_condition": "Failed exact-SHA Node 3 R1 contract verification or CI regression",
        }
    )
    value["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return value


def _phase7_node3_r1_accepted_registry(registry: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(registry))
    capabilities = value.get("additional_program_capabilities")
    if not isinstance(capabilities, list):
        raise CanonicalReconciliationError("additional_program_capabilities is missing or invalid")
    matches = [
        item for item in capabilities
        if isinstance(item, dict) and item.get("key") == "favorites_rebooking"
    ]
    if len(matches) != 1:
        raise CanonicalReconciliationError("favorites_rebooking registry entry is missing or duplicated")
    favorites = matches[0]
    if favorites.get("source_state") != "PLANNED" or favorites.get("program_maturity") != "PLANNED":
        raise CanonicalReconciliationError("favorites_rebooking is not still PLANNED")
    favorites.update(
        {
            "source_state": "LIVE_ACCEPTANCE_ONLY",
            "program_maturity": "REAL_CODE_NOT_LIVE_VERIFIED",
            "execution_contract_id": "LARI-P7-N3-FAVORITES-REBOOKING-R1",
            "execution_base_sha": PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            "delivery_slice": "R1_ACCEPTED_R2_AUTHORIZED",
            "accepted_r1_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
            "accepted_r1_ci_run_id": PHASE7_NODE3_R1_CI_RUN_ID,
        }
    )
    return value


def _decision_024_text(existing: str) -> str:
    if "DECISION-024" in existing:
        raise CanonicalReconciliationError("DECISION-024 already exists")
    block = f"""## DECISION-024: Phase 7 Node 3 R1 Server Foundation Acceptance and R2 Product Integration Authorization
- **Status**: ACCEPTED
- **Authority**: `{PHASE7_NODE3_R1_CONTROLLER_AUTHORITY}`.
- **R1 Acceptance**: Phase 7 Node 3 Favorites & Fast Rebooking R1 is accepted at exact product SHA `{PHASE7_NODE3_R1_ACCEPTED_SHA}` from execution base `{PHASE7_NODE3_R1_EXECUTION_BASE_SHA}`, with exact-SHA GitHub Actions Run `{PHASE7_NODE3_R1_CI_RUN_ID}` completed successfully.
- **Favorites Authority**: Favorites are an `auth.uid()`-scoped relationship over canonical tenant truth. Duplicate marketplace, business, or customer truth is forbidden. Browser `lari_customer_auth`/localStorage identity and raw email or phone are not server favorites authorization.
- **Fast Rebooking Authority**: Fast rebooking is a current-truth seed only. Existing appointment manage-token authority proves ownership of the historical appointment; current service, staff, branch, price, and duration must be re-resolved. `evaluate_booking_slot` remains availability authority and `create_public_booking` remains booking transaction authority. Node 3 code cannot directly insert a rebooking appointment.
- **R2 Authorization**: Bounded Node 3 R2 Product Integration is authorized from exact execution base `{PHASE7_NODE3_R1_ACCEPTED_SHA}`. The fast-rebook UI must use the manage-token surface and R1 seed, require current availability selection, and finish through canonical booking. Favorites UI must use R1 RPCs and real Supabase authenticated identity; if that session is unavailable, the affected favorites-auth surface must fail or degrade safely. OTP, magic-link, or a new customer-auth architecture is not authorized by R2.
- **Independent Lane**: UI-V2 remains an independent parallel lane and the Node 3 branch is not the UI-V2 branch.
- **Production**: `NO_GO`.
"""
    return existing.rstrip() + "\n\n" + block


def _assert_exact_single_commit_lineage(
    product_workspace: Path,
    *,
    branch: str,
    execution_base_sha: str,
    candidate_sha: str,
) -> None:
    _git(["fetch", "origin", f"refs/heads/{branch}"], cwd=product_workspace, timeout=120)
    fetched = (_git(["rev-parse", "FETCH_HEAD"], cwd=product_workspace).stdout or "").strip().lower()
    parent = (_git(["rev-parse", f"{candidate_sha}^"], cwd=product_workspace).stdout or "").strip().lower()
    count = (_git(["rev-list", "--count", f"{execution_base_sha}..{candidate_sha}"], cwd=product_workspace).stdout or "").strip()
    if fetched != candidate_sha.lower() or parent != execution_base_sha.lower() or count != "1":
        raise CanonicalReconciliationError(
            "Node 3 R1 candidate is not exactly one clean descendant of its execution base"
        )


def _assert_phase7_node3_r1_kcp_coverage(
    descriptor: Mapping[str, Any],
    *,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    coverage = _assert_canonical_acceptance_kcp_coverage(
        descriptor,
        candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )
    if set(coverage.get("implementation_receipt_ids") or []) != {
        PHASE7_NODE3_R1_IMPLEMENTATION_RECEIPT_ID
    } or set(coverage.get("verification_receipt_ids") or []) != {
        PHASE7_NODE3_R1_VERIFICATION_RECEIPT_ID
    }:
        raise CanonicalReconciliationError(
            "Node 3 R1 KCP coverage does not contain the exact Controller-bound receipt pair"
        )
    return coverage


def record_phase7_node2_r3_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_run_id: int,
    controller_authority: str,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Apply the one authorized exact-SHA R3 acceptance transition, fail closed."""
    from aos.acceptance_receipt import AcceptanceReceipt, write_acceptance_receipt

    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")
    if (project_id, repository, control_ref) != (
        "lari", "MertSGI/Randapp-main", "control/lari-project-control-plane"
    ):
        raise CanonicalReconciliationError("R3 acceptance descriptor authority mismatch")
    if candidate_sha.lower() != PHASE7_NODE2_R3_ACCEPTED_SHA:
        raise CanonicalReconciliationError("R3 candidate SHA mismatch")
    if execution_base_sha.lower() != PHASE7_NODE2_R2_ACCEPTED_SHA:
        raise CanonicalReconciliationError("R3 execution-base SHA mismatch")
    if int(ci_run_id) != PHASE7_NODE2_R3_CI_RUN_ID:
        raise CanonicalReconciliationError("R3 CI run mismatch")
    if controller_authority != PHASE7_NODE2_R3_CONTROLLER_DECISION:
        raise CanonicalReconciliationError("R3 Controller authority mismatch")

    kcp_coverage = _assert_canonical_acceptance_kcp_coverage(
        descriptor,
        candidate_sha=candidate_sha,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )

    control, remote_before = _ensure_control_clone(repository, control_ref, runtime_dir)
    if remote_before.lower() != PHASE7_NODE2_R3_CONTROL_BASE_SHA:
        raise CanonicalReconciliationError(
            f"R3 starting control SHA drift: {remote_before} != {PHASE7_NODE2_R3_CONTROL_BASE_SHA}"
        )
    remote_candidate = _remote_branch_sha(product_workspace, PHASE7_NODE2_R3_BRANCH)
    if remote_candidate != PHASE7_NODE2_R3_ACCEPTED_SHA:
        raise CanonicalReconciliationError(
            f"R3 remote branch SHA mismatch: {remote_candidate} != {PHASE7_NODE2_R3_ACCEPTED_SHA}"
        )
    ci = _read_github_actions_run(repository, PHASE7_NODE2_R3_CI_RUN_ID)
    ci_matches = (
        int(ci.get("id") or 0) == PHASE7_NODE2_R3_CI_RUN_ID
        and str(ci.get("head_sha") or "").lower() == PHASE7_NODE2_R3_ACCEPTED_SHA
        and str(ci.get("head_branch") or "") == PHASE7_NODE2_R3_BRANCH
        and str(ci.get("status") or "").lower() == "completed"
        and str(ci.get("conclusion") or "").lower() == "success"
    )
    if not ci_matches:
        raise CanonicalReconciliationError("R3 hosted CI does not prove exact candidate SHA success")

    state_path = find_state_json(control, project_id)
    registry_path = control / "docs/project-control/PROGRAM_V2_CAPABILITY_REGISTRY.json"
    decisions_path = control / "docs/project-control/DECISIONS.md"
    r2_receipt_path = control / "docs/project-control/acceptance-receipt-discovery_marketplace_r2.json"
    if not registry_path.is_file() or not decisions_path.is_file() or not r2_receipt_path.is_file():
        raise CanonicalReconciliationError("Required canonical R3 acceptance inputs are missing")
    receipt_dir = control / "docs/project-control"
    historical_receipts = {
        path: path.read_bytes() for path in receipt_dir.glob("acceptance-receipt-*.json")
    }
    r3_receipt_path = receipt_dir / "acceptance-receipt-discovery_marketplace_r3.json"
    if r3_receipt_path.exists():
        raise CanonicalReconciliationError("R3 acceptance receipt already exists and is immutable")
    updated_state = _phase7_node2_r3_accepted_state(
        _read_json(state_path),
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        ci_run_id=ci_run_id,
        ci_conclusion=str(ci.get("conclusion") or ""),
        controller_authority=controller_authority,
    )
    updated_registry = _phase7_node2_r3_accepted_registry(_read_json(registry_path))
    updated_decisions = _decision_023_text(decisions_path.read_text(encoding="utf-8"))
    receipt = AcceptanceReceipt(
        receipt_id="receipt-lari-p7-n2-discovery-marketplace-r3",
        project_id="lari",
        lane="lane-b",
        slice_id="discovery_marketplace_r3",
        execution_base_sha=PHASE7_NODE2_R2_ACCEPTED_SHA,
        candidate_sha=PHASE7_NODE2_R3_ACCEPTED_SHA,
        ci_workflow_name="lari-phase5-postgres-acceptance.yml",
        ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
        control_sha_before=remote_before,
        canonical_control_transition_sha=remote_before,
        production="NO_GO",
    )

    _atomic_json(state_path, updated_state)
    _atomic_json(registry_path, updated_registry)
    _atomic_text(decisions_path, updated_decisions)
    receipt_path = write_acceptance_receipt(control, receipt)
    if any(path.read_bytes() != before for path, before in historical_receipts.items()):
        raise CanonicalReconciliationError("Historical acceptance receipt changed during R3 acceptance")

    status_lines = [
        line.rstrip().replace("\\", "/")
        for line in (_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=control).stdout or "").splitlines()
        if line.strip()
    ]
    changed = {line[3:] if len(line) > 3 else line for line in status_lines}
    if changed != PHASE7_NODE2_R3_ALLOWED_CONTROL_FILES:
        raise CanonicalReconciliationError(f"Unexpected R3 acceptance changed files: {sorted(changed)}")
    _git(["diff", "--check"], cwd=control)
    for rel in sorted(PHASE7_NODE2_R3_ALLOWED_CONTROL_FILES):
        _git(["add", "--", rel], cwd=control)
    staged = {
        line.strip().replace("\\", "/")
        for line in (_git(["diff", "--name-only", "--cached"], cwd=control).stdout or "").splitlines()
        if line.strip()
    }
    if staged != PHASE7_NODE2_R3_ALLOWED_CONTROL_FILES:
        raise CanonicalReconciliationError(f"Unexpected staged R3 acceptance files: {sorted(staged)}")

    _git(["fetch", "origin", control_ref], cwd=control, timeout=120)
    remote_now = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip().lower()
    if remote_now != remote_before.lower():
        raise CanonicalReconciliationError(
            f"Concurrent control drift detected: {remote_before} -> {remote_now}"
        )
    _git(
        [
            "-c", "user.name=AOS Canonical Reconciler",
            "-c", "user.email=aos-reconciler@users.noreply.github.com",
            "commit", "-m", "control(lari): accept Phase 7 Node 2 R3 and bind Node 3 preflight",
        ],
        cwd=control,
    )
    control_after = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip().lower()
    if not HEX40.fullmatch(control_after):
        raise CanonicalReconciliationError(f"Invalid resulting control SHA: {control_after!r}")
    _git(["push", "origin", f"HEAD:{control_ref}"], cwd=control, timeout=300)
    _git(["fetch", "origin", control_ref], cwd=control, timeout=120)
    remote_after = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip().lower()
    if remote_after != control_after:
        raise CanonicalReconciliationError(
            f"R3 acceptance push verification failed: {control_after} != {remote_after}"
        )

    result = {
        "status": "ACCEPTED",
        "control_sha_before": remote_before.lower(),
        "control_sha_after": control_after,
        "control_remote_sha_equal": True,
        "candidate_sha": PHASE7_NODE2_R3_ACCEPTED_SHA,
        "controller_authority": PHASE7_NODE2_R3_CONTROLLER_DECISION,
        "controller_decision_id": PHASE7_NODE2_R3_CONTROLLER_DECISION,
        "kcp_implementation_receipt_ids": kcp_coverage["implementation_receipt_ids"],
        "kcp_verification_receipt_ids": kcp_coverage["verification_receipt_ids"],
        "accepted_gate": PHASE7_NODE2_R3_GATE,
        "acceptance_receipt": receipt_path.relative_to(control).as_posix(),
        "current_status": PHASE7_NODE3_PREBIND_STATUS,
        "next_action_execution_base_sha": PHASE7_NODE2_R3_ACCEPTED_SHA,
        "production": "NO_GO",
    }
    _atomic_json(runtime_dir / "r3-acceptance.json", result)
    return result


def record_phase7_node3_r1_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_run_id: int,
    controller_authority: str,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Apply the one authorized exact-SHA Node 3 R1 acceptance transition."""
    from aos.acceptance_receipt import AcceptanceReceipt, write_acceptance_receipt

    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")
    if (project_id, repository, control_ref) != (
        "lari", "MertSGI/Randapp-main", "control/lari-project-control-plane"
    ):
        raise CanonicalReconciliationError("Node 3 R1 acceptance descriptor authority mismatch")
    if candidate_sha.lower() != PHASE7_NODE3_R1_ACCEPTED_SHA:
        raise CanonicalReconciliationError("Node 3 R1 candidate SHA mismatch")
    if execution_base_sha.lower() != PHASE7_NODE3_R1_EXECUTION_BASE_SHA:
        raise CanonicalReconciliationError("Node 3 R1 execution-base SHA mismatch")
    if int(ci_run_id) != PHASE7_NODE3_R1_CI_RUN_ID:
        raise CanonicalReconciliationError("Node 3 R1 CI run mismatch")
    if controller_authority != PHASE7_NODE3_R1_CONTROLLER_AUTHORITY:
        raise CanonicalReconciliationError("Node 3 R1 Controller authority mismatch")

    # KCP is deliberately asserted before a canonical checkout is created or
    # any control file can be written.
    kcp_coverage = _assert_phase7_node3_r1_kcp_coverage(
        descriptor,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )

    control, remote_before = _ensure_control_clone(repository, control_ref, runtime_dir)
    if remote_before.lower() != PHASE7_NODE3_R1_CONTROL_BASE_SHA:
        raise CanonicalReconciliationError(
            f"Node 3 R1 starting control SHA drift: {remote_before} != {PHASE7_NODE3_R1_CONTROL_BASE_SHA}"
        )
    remote_candidate = _remote_branch_sha(product_workspace, PHASE7_NODE3_R1_BRANCH)
    if remote_candidate != PHASE7_NODE3_R1_ACCEPTED_SHA:
        raise CanonicalReconciliationError(
            f"Node 3 R1 remote branch SHA mismatch: {remote_candidate} != {PHASE7_NODE3_R1_ACCEPTED_SHA}"
        )
    _assert_exact_single_commit_lineage(
        product_workspace,
        branch=PHASE7_NODE3_R1_BRANCH,
        execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
    )
    ci = _read_github_actions_run(repository, PHASE7_NODE3_R1_CI_RUN_ID)
    ci_matches = (
        int(ci.get("id") or 0) == PHASE7_NODE3_R1_CI_RUN_ID
        and str(ci.get("head_sha") or "").lower() == PHASE7_NODE3_R1_ACCEPTED_SHA
        and str(ci.get("head_branch") or "") == PHASE7_NODE3_R1_BRANCH
        and str(ci.get("status") or "").lower() == "completed"
        and str(ci.get("conclusion") or "").lower() == "success"
    )
    if not ci_matches:
        raise CanonicalReconciliationError(
            "Node 3 R1 hosted CI does not prove exact candidate SHA success"
        )

    state_path = find_state_json(control, project_id)
    control_dir = control / "docs/project-control"
    registry_path = control_dir / "PROGRAM_V2_CAPABILITY_REGISTRY.json"
    decisions_path = control_dir / "DECISIONS.md"
    prior_receipt_path = control_dir / "acceptance-receipt-discovery_marketplace_r3.json"
    if not registry_path.is_file() or not decisions_path.is_file() or not prior_receipt_path.is_file():
        raise CanonicalReconciliationError("Required canonical Node 3 R1 acceptance inputs are missing")
    historical_receipts = {
        path: path.read_bytes() for path in control_dir.glob("acceptance-receipt-*.json")
    }
    receipt_target = control_dir / "acceptance-receipt-favorites_rebooking_r1.json"
    if receipt_target.exists():
        raise CanonicalReconciliationError(
            "Node 3 R1 acceptance receipt already exists and is immutable"
        )

    updated_state = _phase7_node3_r1_accepted_state(
        _read_json(state_path),
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        ci_run_id=ci_run_id,
        ci_conclusion=str(ci.get("conclusion") or ""),
        controller_authority=controller_authority,
    )
    updated_registry = _phase7_node3_r1_accepted_registry(_read_json(registry_path))
    updated_decisions = _decision_024_text(decisions_path.read_text(encoding="utf-8"))
    receipt = AcceptanceReceipt(
        receipt_id="receipt-lari-p7-n3-favorites-rebooking-r1",
        project_id="lari",
        lane="lane-b",
        slice_id="favorites_rebooking_r1",
        execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
        ci_workflow_name="lari-phase5-postgres-acceptance.yml",
        ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
        control_sha_before=remote_before,
        canonical_control_transition_sha=remote_before,
        production="NO_GO",
    )

    _atomic_json(state_path, updated_state)
    _atomic_json(registry_path, updated_registry)
    _atomic_text(decisions_path, updated_decisions)
    receipt_path = write_acceptance_receipt(control, receipt)
    if any(path.read_bytes() != before for path, before in historical_receipts.items()):
        raise CanonicalReconciliationError(
            "Historical acceptance receipt changed during Node 3 R1 acceptance"
        )

    status_lines = [
        line.rstrip().replace("\\", "/")
        for line in (_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=control).stdout or "").splitlines()
        if line.strip()
    ]
    changed = {line[3:] if len(line) > 3 else line for line in status_lines}
    if changed != PHASE7_NODE3_R1_ALLOWED_CONTROL_FILES:
        raise CanonicalReconciliationError(
            f"Unexpected Node 3 R1 acceptance changed files: {sorted(changed)}"
        )
    _git(["diff", "--check"], cwd=control)
    for rel in sorted(PHASE7_NODE3_R1_ALLOWED_CONTROL_FILES):
        _git(["add", "--", rel], cwd=control)
    staged = {
        line.strip().replace("\\", "/")
        for line in (_git(["diff", "--name-only", "--cached"], cwd=control).stdout or "").splitlines()
        if line.strip()
    }
    if staged != PHASE7_NODE3_R1_ALLOWED_CONTROL_FILES:
        raise CanonicalReconciliationError(
            f"Unexpected staged Node 3 R1 acceptance files: {sorted(staged)}"
        )

    _git(["fetch", "origin", control_ref], cwd=control, timeout=120)
    remote_now = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip().lower()
    if remote_now != remote_before.lower():
        raise CanonicalReconciliationError(
            f"Concurrent control drift detected: {remote_before} -> {remote_now}"
        )
    _git(
        [
            "-c", "user.name=AOS Canonical Reconciler",
            "-c", "user.email=aos-reconciler@users.noreply.github.com",
            "commit", "-m", "control(lari): accept Phase 7 Node 3 R1 and authorize R2",
        ],
        cwd=control,
    )
    control_after = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip().lower()
    if not HEX40.fullmatch(control_after):
        raise CanonicalReconciliationError(f"Invalid resulting control SHA: {control_after!r}")
    _git(["push", "origin", f"HEAD:{control_ref}"], cwd=control, timeout=300)
    _git(["fetch", "origin", control_ref], cwd=control, timeout=120)
    remote_after = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip().lower()
    if remote_after != control_after:
        raise CanonicalReconciliationError(
            f"Node 3 R1 acceptance push verification failed: {control_after} != {remote_after}"
        )

    result = {
        "status": "ACCEPTED",
        "control_sha_before": remote_before.lower(),
        "control_sha_after": control_after,
        "control_remote_sha_equal": True,
        "candidate_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        "controller_authority": PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
        "controller_decision_id": "DECISION-024",
        "kcp_implementation_receipt_ids": kcp_coverage["implementation_receipt_ids"],
        "kcp_verification_receipt_ids": kcp_coverage["verification_receipt_ids"],
        "accepted_gate": PHASE7_NODE3_R1_GATE,
        "acceptance_receipt": receipt_path.relative_to(control).as_posix(),
        "current_status": PHASE7_NODE3_R2_AUTHORIZED_STATUS,
        "next_action_execution_base_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        "downstream_activation": {"status": "NOT_EVALUATED"},
        "production": "NO_GO",
    }

    # Canonical acceptance is already final here. Operational gate evaluation
    # may only touch existing command admission and is reported as an explicit
    # hold if it cannot complete; it never rolls back or obscures acceptance.
    try:
        from aos.cross_lane_coordinator import evaluate_downstream_gates
        from aos.runtime_admission import CommandAdmissionStore
        admission_store = CommandAdmissionStore(
            runtime_dir.parent.parent if runtime_dir.name == "project-runtime" else runtime_dir
        )
        activated = evaluate_downstream_gates(control, admission_store)
        result["downstream_activation"] = {
            "status": "SUCCEEDED" if activated else "NO_TRANSITION_REQUIRED",
            "activated_command_ids": [record.command_id for record in activated],
        }
    except Exception as exc:
        result["status"] = "ACCEPTED_WITH_DOWNSTREAM_HOLD"
        result["downstream_activation"] = {
            "status": "HOLD",
            "classification": getattr(
                exc,
                "classification",
                f"DOWNSTREAM_ACTIVATION_{type(exc).__name__.upper()}",
            ),
            "command_id": getattr(exc, "command_id", None),
        }
    _atomic_json(runtime_dir / "node3-r1-acceptance.json", result)
    return result


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
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Execute atomic forward-only canonical control-plane acceptance transition."""
    from aos.acceptance_receipt import AcceptanceReceipt, write_acceptance_receipt

    slice_lower = slice_id.lower().replace("-", "_")
    if (
        "node3" in slice_lower
        or "ui" in slice_lower
        or "convergence" in slice_lower
        or not (
            slice_lower.startswith("discovery_marketplace")
            or "node2" in slice_lower
            or slice_lower in ("r1", "r2", "r3")
        )
    ):
        raise CanonicalReconciliationError(
            f"record_slice_acceptance is reserved exclusively for Node 2 delivery slices; cannot accept '{slice_id}'. "
            "Use dedicated typed acceptance entrypoints for Node 3, UI-V2, and convergence."
        )

    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")

    kcp_coverage = _assert_canonical_acceptance_kcp_coverage(
        descriptor,
        candidate_sha=candidate_sha,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )

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

    if not isinstance(acceptance_receipt, AcceptanceReceipt):
        raise CanonicalReconciliationError("Structured acceptance receipt is required")
    if acceptance_receipt.project_id != project_id:
        raise CanonicalReconciliationError("Acceptance receipt project identity mismatch")
    if acceptance_receipt.candidate_sha.lower() != candidate_sha.lower():
        raise CanonicalReconciliationError("Acceptance receipt candidate SHA mismatch")
    bound_receipt = dataclasses.replace(
        acceptance_receipt,
        control_sha_before=control_sha,
        canonical_control_transition_sha=control_sha,
    )

    # 3. Prepare required registry reconciliation before any canonical write.
    registry_path = control / "docs" / "project-control" / "PROGRAM_V2_CAPABILITY_REGISTRY.json"
    updated_registry = None
    if registry_path.is_file():
        updated_registry = _read_json(registry_path)
        registry_items = updated_registry.get("current_live_commercial_registry")
        if not isinstance(registry_items, list):
            raise CanonicalReconciliationError("Canonical capability registry is invalid")
        matches = [
            item for item in registry_items
            if isinstance(item, dict) and item.get("key") == "discovery_marketplace"
        ]
        if len(matches) != 1:
            raise CanonicalReconciliationError(
                "Canonical discovery_marketplace registry entry is missing or duplicated"
            )
        matches[0]["program_maturity"] = "REAL_CODE_NOT_LIVE_VERIFIED"
        matches[0]["accepted_r2_sha"] = candidate_sha
        matches[0]["delivery_slice"] = "R2_ACCEPTED_R3_UI_READY"

    receipt_dir = control / "docs" / "project-control"
    historical_receipts = {
        path: path.read_bytes() for path in receipt_dir.glob("acceptance-receipt-*.json")
    }
    receipt_slug = bound_receipt.slice_id.replace("/", "-").replace(" ", "-").lower()
    receipt_target = receipt_dir / f"acceptance-receipt-{receipt_slug}.json"
    if receipt_target.exists():
        raise CanonicalReconciliationError(
            f"acceptance receipt is immutable: {receipt_target.name}"
        )

    # 4. Apply the precomputed canonical writes and stage only the new receipt.
    if updated_registry is not None:
        _atomic_json(registry_path, updated_registry)
        _git(["add", "--", str(registry_path)], cwd=control)

    _atomic_json(state_path, updated_state)
    _git(["add", "--", str(state_path)], cwd=control)
    try:
        receipt_path = write_acceptance_receipt(control, bound_receipt)
    except FileExistsError as exc:
        raise CanonicalReconciliationError(str(exc)) from exc
    if any(path.read_bytes() != before for path, before in historical_receipts.items()):
        raise CanonicalReconciliationError("Historical acceptance receipt changed during acceptance")
    _git(["add", "--", str(receipt_path)], cwd=control)

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

    result = {
        "status": "ACCEPTED",
        "slice_id": slice_id,
        "candidate_sha": candidate_sha,
        "control_sha_before": control_sha,
        "control_sha_after": new_control_sha,
        "control_transition_sha": new_control_sha,
        "controller_authority": bound_receipt.controller_authority,
        "controller_decision_id": bound_receipt.controller_authority,
        "kcp_implementation_receipt_ids": kcp_coverage["implementation_receipt_ids"],
        "kcp_verification_receipt_ids": kcp_coverage["verification_receipt_ids"],
        "next_status": updated_state["current_status"],
        "downstream_activation": {"status": "NOT_EVALUATED"},
    }

    # 9. Evaluate operational downstream gates after canonical acceptance is final.
    try:
        from aos.cross_lane_coordinator import evaluate_downstream_gates
        from aos.runtime_admission import CommandAdmissionStore
        admission_store = CommandAdmissionStore(runtime_dir.parent.parent if runtime_dir.name == "project-runtime" else runtime_dir)
        activated = evaluate_downstream_gates(control, admission_store)
        result["downstream_activation"] = {
            "status": "SUCCEEDED" if activated else "NO_TRANSITION_REQUIRED",
            "activated_command_ids": [record.command_id for record in activated],
        }
    except Exception as exc:
        result["status"] = "ACCEPTED_WITH_DOWNSTREAM_HOLD"
        result["downstream_activation"] = {
            "status": "HOLD",
            "classification": getattr(
                exc,
                "classification",
                f"DOWNSTREAM_ACTIVATION_{type(exc).__name__.upper()}",
            ),
            "command_id": getattr(exc, "command_id", None),
        }

    result["runtime_evidence_write"] = {"status": "RECORDED"}
    try:
        _atomic_json(runtime_dir / f"slice-acceptance-{slice_id.lower()}.json", result)
    except Exception as exc:
        result["runtime_evidence_write"] = {
            "status": "FAILED",
            "classification": f"RUNTIME_EVIDENCE_{type(exc).__name__.upper()}",
        }
    return result


def _verify_ci_evidence(
    repository: str,
    ci_evidence: Mapping[str, Any],
    *,
    candidate_sha: str,
    expected_branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify CI evidence proves exact candidate SHA success either via payload or GitHub Actions."""
    run_id = ci_evidence.get("run_id") or ci_evidence.get("id")
    if not run_id:
        raise CanonicalReconciliationError("CI run_id is required in ci_evidence")

    # If full run status is passed in evidence dict:
    status = str(ci_evidence.get("status") or "").lower()
    conclusion = str(ci_evidence.get("conclusion") or "").lower()
    head_sha = str(ci_evidence.get("head_sha") or "").lower()
    head_branch = str(ci_evidence.get("head_branch") or "")

    if status == "completed" and conclusion == "success" and head_sha == candidate_sha.lower():
        if expected_branch is not None and head_branch and head_branch != expected_branch:
            raise CanonicalReconciliationError(
                f"CI branch mismatch: expected {expected_branch}, got {head_branch}"
            )
        return dict(ci_evidence)

    # Otherwise query GitHub Actions directly
    ci = _read_github_actions_run(repository, int(run_id))
    ci_head_sha = str(ci.get("head_sha") or "").lower()
    ci_branch = str(ci.get("head_branch") or "")
    ci_status = str(ci.get("status") or "").lower()
    ci_conclusion = str(ci.get("conclusion") or "").lower()

    if (
        ci_head_sha != candidate_sha.lower()
        or ci_status != "completed"
        or ci_conclusion != "success"
    ):
        raise CanonicalReconciliationError(
            f"Hosted CI run {run_id} does not prove exact candidate SHA {candidate_sha} success: "
            f"status={ci_status}, conclusion={ci_conclusion}, head_sha={ci_head_sha}"
        )
    if expected_branch is not None and ci_branch and ci_branch != expected_branch:
        raise CanonicalReconciliationError(
            f"Hosted CI run {run_id} branch mismatch: expected {expected_branch}, got {ci_branch}"
        )
    return ci


def _record_typed_acceptance(
    *,
    acceptance_kind: str,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    expected_lane: str,
    ci_evidence: Mapping[str, Any],
    slice_id: str,
    acceptance_receipt: Any,
    expected_predecessor_authority: str,
    expected_control_sha_before: Optional[str] = None,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
    allowed_control_files: Optional[frozenset] = None,
    commit_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Generic fail-closed typed acceptance engine enforcing all Controller safety invariants."""
    from aos.acceptance_receipt import AcceptanceReceipt, write_acceptance_receipt

    descriptor = _read_json(descriptor_path)
    project_id = str(descriptor.get("project_id") or "")
    repository = str(descriptor.get("repository") or "")
    control_ref = str(descriptor.get("control_ref") or "")

    if not project_id or not repository or not control_ref:
        raise CanonicalReconciliationError(
            f"{acceptance_kind} acceptance descriptor incomplete: project_id, repository, control_ref required"
        )

    # 1. Exact candidate SHA format check
    if not HEX40.fullmatch(candidate_sha.lower()):
        raise CanonicalReconciliationError(f"Invalid candidate SHA format: {candidate_sha!r}")
    if not HEX40.fullmatch(execution_base_sha.lower()):
        raise CanonicalReconciliationError(f"Invalid execution base SHA format: {execution_base_sha!r}")

    # 2. Receipt contract check
    if not isinstance(acceptance_receipt, AcceptanceReceipt):
        raise CanonicalReconciliationError("Structured AcceptanceReceipt instance is required")
    if acceptance_receipt.project_id != project_id:
        raise CanonicalReconciliationError(
            f"Acceptance receipt project mismatch: expected {project_id}, got {acceptance_receipt.project_id}"
        )
    if acceptance_receipt.lane != expected_lane:
        raise CanonicalReconciliationError(
            f"Acceptance receipt lane mismatch: expected {expected_lane}, got {acceptance_receipt.lane}"
        )
    if acceptance_receipt.candidate_sha.lower() != candidate_sha.lower():
        raise CanonicalReconciliationError(
            f"Acceptance receipt candidate SHA mismatch: expected {candidate_sha}, got {acceptance_receipt.candidate_sha}"
        )
    if acceptance_receipt.execution_base_sha.lower() != execution_base_sha.lower():
        raise CanonicalReconciliationError(
            f"Acceptance receipt execution base SHA mismatch: expected {execution_base_sha}, got {acceptance_receipt.execution_base_sha}"
        )
    if acceptance_receipt.acceptance_result != "ACCEPTED":
        raise CanonicalReconciliationError(
            f"Acceptance receipt result must be ACCEPTED, got {acceptance_receipt.acceptance_result}"
        )
    if acceptance_receipt.controller_authority != expected_predecessor_authority:
        raise CanonicalReconciliationError(
            f"Controller authority mismatch: expected {expected_predecessor_authority}, got {acceptance_receipt.controller_authority}"
        )

    # 3. Exact successful CI evidence
    ci_verified = _verify_ci_evidence(repository, ci_evidence, candidate_sha=candidate_sha)
    ci_run_id = int(ci_verified.get("id") or ci_verified.get("run_id") or 0)
    if acceptance_receipt.ci_run_id != ci_run_id:
        raise CanonicalReconciliationError(
            f"Receipt CI run_id {acceptance_receipt.ci_run_id} != verified CI run_id {ci_run_id}"
        )

    # 4. KCP exact-SHA accepted-work coverage check
    kcp_coverage = _assert_canonical_acceptance_kcp_coverage(
        descriptor,
        candidate_sha=candidate_sha,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )

    # 5. Clone canonical control and verify compare-and-swap base SHA
    control, control_sha = _ensure_control_clone(repository, control_ref, runtime_dir)
    if expected_control_sha_before is not None and control_sha.lower() != expected_control_sha_before.lower():
        raise CanonicalReconciliationError(
            f"{acceptance_kind} control_sha_before CAS drift: expected {expected_control_sha_before}, got {control_sha}"
        )

    state_path = find_state_json(control, project_id)
    current_state = _read_json(state_path)
    validate_canonical_coherence(current_state)

    # Verify execution base matches current state execution chain / accepted SHA
    state_base = str(
        (current_state.get("candidate_release") or {}).get("accepted_product_sha")
        or current_state.get("next_action_execution_base_sha")
        or ""
    ).lower()
    if state_base and state_base != execution_base_sha.lower():
        raise CanonicalReconciliationError(
            f"Canonical state execution base drift: state has {state_base}, acceptance expects {execution_base_sha}"
        )

    # 6. Bind receipt with control_sha_before
    bound_receipt = dataclasses.replace(
        acceptance_receipt,
        control_sha_before=control_sha,
        canonical_control_transition_sha=control_sha,
    )

    # 7. Write scoped immutable receipt
    receipt_dir = control / "docs" / "project-control"
    historical_receipts = {
        path: path.read_bytes() for path in receipt_dir.glob("acceptance-receipt-*.json")
    }
    slug = bound_receipt.slice_id.replace("/", "-").replace(" ", "-").lower()
    target_path = receipt_dir / f"acceptance-receipt-{slug}.json"
    if target_path.exists():
        raise CanonicalReconciliationError(f"acceptance receipt is immutable: {target_path.name}")

    try:
        written_receipt_path = write_acceptance_receipt(control, bound_receipt)
    except FileExistsError as exc:
        raise CanonicalReconciliationError(str(exc)) from exc

    if any(path.read_bytes() != before for path, before in historical_receipts.items()):
        raise CanonicalReconciliationError("Historical acceptance receipt changed during acceptance")
    _git(["add", "--", str(written_receipt_path)], cwd=control)

    # 8. Check bounded canonical file scope
    diff_names = [
        f.replace("\\", "/")
        for f in (_git(["diff", "--name-only", "--cached"], cwd=control).stdout or "").splitlines()
        if f.strip()
    ]
    if allowed_control_files is not None:
        if set(diff_names) != allowed_control_files:
            raise CanonicalReconciliationError(
                f"Unexpected staged control files for {acceptance_kind}: {sorted(diff_names)} != {sorted(allowed_control_files)}"
            )
    else:
        for f in diff_names:
            if not f.startswith("docs/project-control/"):
                raise CanonicalReconciliationError(
                    f"Unexpected staged file outside docs/project-control/ for {acceptance_kind}: {f}"
                )

    # 9. Verify remote drift before push
    _git(["fetch", "origin", control_ref], cwd=control, timeout=60)
    remote_now = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    local_base = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip()
    if remote_now != local_base:
        raise CanonicalReconciliationError(
            f"Concurrent control drift detected: local {local_base} != remote {remote_now}"
        )

    # 10. Commit and fast-forward push
    msg = commit_message or f"control({project_id}): accept {acceptance_kind} {slice_id.upper()}"
    _git(
        [
            "-c", "user.name=AOS Canonical Reconciler",
            "-c", "user.email=aos-reconciler@users.noreply.github.com",
            "commit", "-m", msg,
        ],
        cwd=control,
    )
    new_control_sha = (_git(["rev-parse", "HEAD"], cwd=control).stdout or "").strip()
    _git(["push", "origin", f"HEAD:{control_ref}"], cwd=control, timeout=300)

    # 11. Verify remote SHA matches
    _git(["fetch", "origin", control_ref], cwd=control, timeout=60)
    remote_after = (_git(["rev-parse", "FETCH_HEAD"], cwd=control).stdout or "").strip()
    if remote_after != new_control_sha:
        raise CanonicalReconciliationError(
            f"Remote SHA verification failed after push: expected {new_control_sha}, got {remote_after}"
        )

    result = {
        "status": "ACCEPTED",
        "acceptance_kind": acceptance_kind,
        "slice_id": slice_id,
        "candidate_sha": candidate_sha,
        "execution_base_sha": execution_base_sha,
        "control_sha_before": control_sha,
        "control_sha_after": new_control_sha,
        "control_transition_sha": new_control_sha,
        "controller_authority": bound_receipt.controller_authority,
        "controller_decision_id": bound_receipt.controller_authority,
        "kcp_implementation_receipt_ids": kcp_coverage["implementation_receipt_ids"],
        "kcp_verification_receipt_ids": kcp_coverage["verification_receipt_ids"],
        "next_status": current_state.get("current_status"),
        "downstream_activation": {"status": "NOT_EVALUATED"},
    }

    try:
        from aos.cross_lane_coordinator import evaluate_downstream_gates
        from aos.runtime_admission import CommandAdmissionStore
        admission_store = CommandAdmissionStore(
            runtime_dir.parent.parent if runtime_dir.name == "project-runtime" else runtime_dir
        )
        activated = evaluate_downstream_gates(control, admission_store)
        result["downstream_activation"] = {
            "status": "SUCCEEDED" if activated else "NO_TRANSITION_REQUIRED",
            "activated_command_ids": [record.command_id for record in activated],
        }
    except Exception as exc:
        result["status"] = "ACCEPTED_WITH_DOWNSTREAM_HOLD"
        result["downstream_activation"] = {
            "status": "HOLD",
            "classification": getattr(
                exc,
                "classification",
                f"DOWNSTREAM_ACTIVATION_{type(exc).__name__.upper()}",
            ),
            "command_id": getattr(exc, "command_id", None),
        }

    result["runtime_evidence_write"] = {"status": "RECORDED"}
    evidence_file = runtime_dir / f"{acceptance_kind.lower().replace('_', '-')}-acceptance.json"
    try:
        _atomic_json(evidence_file, result)
    except Exception as exc:
        result["runtime_evidence_write"] = {
            "status": "FAILED",
            "classification": f"RUNTIME_EVIDENCE_{type(exc).__name__.upper()}",
        }
    return result


def record_program_v2_convergence_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_evidence: Mapping[str, Any],
    acceptance_receipt: Any,
    expected_predecessor_authority: str,
    expected_control_sha_before: Optional[str] = None,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Typed acceptance entrypoint for PROGRAM_V2_CONVERGENCE."""
    return _record_typed_acceptance(
        acceptance_kind="PROGRAM_V2_CONVERGENCE",
        descriptor_path=descriptor_path,
        product_workspace=product_workspace,
        runtime_dir=runtime_dir,
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        expected_lane="convergence",
        ci_evidence=ci_evidence,
        slice_id="program_v2_convergence",
        acceptance_receipt=acceptance_receipt,
        expected_predecessor_authority=expected_predecessor_authority,
        expected_control_sha_before=expected_control_sha_before,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )


def record_phase7_node3_r2_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_evidence: Mapping[str, Any],
    acceptance_receipt: Any,
    expected_predecessor_authority: str,
    expected_control_sha_before: Optional[str] = None,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Typed acceptance entrypoint for PHASE7_NODE3_R2."""
    return _record_typed_acceptance(
        acceptance_kind="PHASE7_NODE3_R2",
        descriptor_path=descriptor_path,
        product_workspace=product_workspace,
        runtime_dir=runtime_dir,
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        expected_lane="lane-b",
        ci_evidence=ci_evidence,
        slice_id="phase7_node3_r2",
        acceptance_receipt=acceptance_receipt,
        expected_predecessor_authority=expected_predecessor_authority,
        expected_control_sha_before=expected_control_sha_before,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )


def record_lari_ui_v2_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_evidence: Mapping[str, Any],
    acceptance_receipt: Any,
    expected_predecessor_authority: str,
    expected_control_sha_before: Optional[str] = None,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Typed acceptance entrypoint for LARI_UI_V2."""
    return _record_typed_acceptance(
        acceptance_kind="LARI_UI_V2",
        descriptor_path=descriptor_path,
        product_workspace=product_workspace,
        runtime_dir=runtime_dir,
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        expected_lane="lari-ui-v2",
        ci_evidence=ci_evidence,
        slice_id="lari_ui_v2",
        acceptance_receipt=acceptance_receipt,
        expected_predecessor_authority=expected_predecessor_authority,
        expected_control_sha_before=expected_control_sha_before,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )


def record_combined_release_acceptance(
    *,
    descriptor_path: Path,
    product_workspace: Path,
    runtime_dir: Path,
    candidate_sha: str,
    execution_base_sha: str,
    ci_evidence: Mapping[str, Any],
    acceptance_receipt: Any,
    expected_predecessor_authority: str,
    expected_control_sha_before: Optional[str] = None,
    knowledge_ledger: Any = None,
    runtime_home: Path | str | None = None,
) -> Dict[str, Any]:
    """Typed acceptance entrypoint for COMBINED_RELEASE."""
    return _record_typed_acceptance(
        acceptance_kind="COMBINED_RELEASE",
        descriptor_path=descriptor_path,
        product_workspace=product_workspace,
        runtime_dir=runtime_dir,
        candidate_sha=candidate_sha,
        execution_base_sha=execution_base_sha,
        expected_lane="release",
        ci_evidence=ci_evidence,
        slice_id="combined_release",
        acceptance_receipt=acceptance_receipt,
        expected_predecessor_authority=expected_predecessor_authority,
        expected_control_sha_before=expected_control_sha_before,
        knowledge_ledger=knowledge_ledger,
        runtime_home=runtime_home,
    )
