import copy
import json
import subprocess
from pathlib import Path

import pytest

from aos.canonical_reconciler import (
    CanonicalReconciliationError,
    PHASE7_NODE2_R2_ACCEPTED_SHA,
    PHASE7_NODE2_R2_GATE,
    bind_missing_execution_base,
    derive_latest_accepted_product_sha,
    reconcile_phase7_node2_r2_frontier,
    reconcile_missing_execution_base,
    record_slice_acceptance,
)


R1_SHA = "814e3ca0c09c3a484e20869f1a47a3545259f6db"
R2_SHA = PHASE7_NODE2_R2_ACCEPTED_SHA


def _partial_r2_state() -> dict:
    return {
        "current_status": "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY",
        "current_milestone": "Program V2 Phase 7 — Node 2 Discovery Marketplace R3",
        "next_action": (
            f"Implement Phase 7 Node 2 R3 — Customer-facing Discovery / Portfolio UI "
            f"from accepted R2 execution base {R2_SHA} under DECISION-022. "
            "Production remains NO_GO."
        ),
        "next_action_execution_base_sha": R1_SHA,
        "candidate_release": {
            "accepted_product_sha": R2_SHA,
            "next_action_execution_base_sha": R2_SHA,
        },
        "accepted_gates": [
            {
                "gate": PHASE7_NODE2_R2_GATE,
                "status": "CLOSED_PROVEN",
                "tested_sha": R2_SHA,
            }
        ],
        "phase7_accepted_execution_chain": {
            "node1": "2" * 40,
            "node2_r1": R1_SHA,
        },
        "phase7_node2_discovery_marketplace_r1_state": {
            "product_sha": R1_SHA,
        },
        "next_product_action": {
            "phase": "PHASE_7",
            "node": "NODE_2_DISCOVERY_MARKETPLACE",
            "slice": "R2_APPLICATION_SERVICE_ADAPTER",
            "canonical_capability_order": [
                "Node 1 — Verified Reviews",
                "Node 2 — Discovery Marketplace",
                "Node 3 — Favorites & Fast Rebooking",
            ],
            "execution_base_sha": R1_SHA,
        },
        "phase7_node2_contract": {
            "contract_id": "LARI-P7-N2-DISCOVERY-MARKETPLACE-R2",
            "authority": "DECISION-022",
            "status": "BOUND_READY_FOR_IMPLEMENTATION",
            "execution_base_sha": R1_SHA,
            "r1_server_authority": {
                "status": "ACCEPTED_PROVEN",
                "product_sha": R1_SHA,
            },
            "architecture": {"model": "SERVER_AUTHORITATIVE_PUBLIC_PROJECTION"},
            "server_contract": {"minimum_capabilities": ["bounded discovery/search"]},
            "acceptance": {"production": "NO_GO"},
            "delivery_slices": {
                "R1": "ACCEPTED_PROVEN",
                "R2": "BOUND_READY_FOR_IMPLEMENTATION",
                "R3": "BLOCKED_UNTIL_R2_ACCEPTED",
            },
            "ui_v2": "HOLD_UNTIL_R1_AND_R2_ACCEPTED",
            "node3": "NOT_IN_SCOPE",
        },
    }


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


def test_bind_missing_execution_base_returns_no_change_when_current():
    state = {"next_action_execution_base_sha": "a" * 40}
    status, updated = bind_missing_execution_base(state, "a" * 40)

    assert status == "ALREADY_CURRENT"
    assert updated == state


def test_exact_r2_partial_frontier_reconciles_without_rewriting_r1_history():
    state = _partial_r2_state()
    protected = {
        "chain_r1": state["phase7_accepted_execution_chain"]["node2_r1"],
        "r1_product": state["phase7_node2_discovery_marketplace_r1_state"]["product_sha"],
        "contract_base": state["phase7_node2_contract"]["execution_base_sha"],
        "r1_authority": copy.deepcopy(state["phase7_node2_contract"]["r1_server_authority"]),
        "architecture": copy.deepcopy(state["phase7_node2_contract"]["architecture"]),
        "server_contract": copy.deepcopy(state["phase7_node2_contract"]["server_contract"]),
        "acceptance": copy.deepcopy(state["phase7_node2_contract"]["acceptance"]),
        "capability_order": copy.deepcopy(state["next_product_action"]["canonical_capability_order"]),
    }

    action, updated = reconcile_phase7_node2_r2_frontier(
        state,
        project_id="lari",
        latest_accepted_product_sha=R2_SHA,
    )

    assert action == "RECONCILED_PHASE7_NODE2_R2_FRONTIER"
    assert state["next_action_execution_base_sha"] == R1_SHA
    assert updated["next_action_execution_base_sha"] == R2_SHA
    assert updated["next_product_action"] == {
        "phase": "PHASE_7",
        "node": "NODE_2_DISCOVERY_MARKETPLACE",
        "slice": "R3_CUSTOMER_FACING_DISCOVERY_PORTFOLIO_UI",
        "canonical_capability_order": protected["capability_order"],
        "execution_base_sha": R2_SHA,
    }
    assert updated["phase7_accepted_execution_chain"]["node2_r1"] == protected["chain_r1"]
    assert updated["phase7_accepted_execution_chain"]["node2_r2"] == R2_SHA
    assert updated["phase7_node2_discovery_marketplace_r1_state"]["product_sha"] == protected["r1_product"]
    contract = updated["phase7_node2_contract"]
    assert contract["execution_base_sha"] == protected["contract_base"]
    assert contract["r1_server_authority"] == protected["r1_authority"]
    assert contract["architecture"] == protected["architecture"]
    assert contract["server_contract"] == protected["server_contract"]
    assert contract["acceptance"] == protected["acceptance"]
    assert contract["status"] == "ACCEPTED_PROVEN"
    assert contract["delivery_slices"] == {
        "R1": "ACCEPTED_PROVEN",
        "R2": "ACCEPTED_PROVEN",
        "R3": "BOUND_READY_FOR_IMPLEMENTATION",
    }
    assert contract["ui_v2"] == "RELEASED_ACTIVE"


@pytest.mark.parametrize(
    "mismatch",
    [
        "project_id",
        "current_status",
        "latest_accepted_product_sha",
        "next_action_execution_base_sha",
        "candidate_release.accepted_product_sha",
        "candidate_release.next_action_execution_base_sha",
        "accepted_gates.R2.status",
        "accepted_gates.R2.tested_sha",
        "next_action.R3",
        "next_action.execution_base_sha",
    ],
)
def test_r2_partial_frontier_fails_closed_on_any_corroborating_mismatch(mismatch: str):
    state = _partial_r2_state()
    original = copy.deepcopy(state)
    project_id = "lari"
    latest = R2_SHA

    if mismatch == "project_id":
        project_id = "other"
    elif mismatch == "current_status":
        state["current_status"] = "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_BOUND_READY"
    elif mismatch == "latest_accepted_product_sha":
        latest = "f" * 40
    elif mismatch == "next_action_execution_base_sha":
        state["next_action_execution_base_sha"] = "f" * 40
    elif mismatch == "candidate_release.accepted_product_sha":
        state["candidate_release"]["accepted_product_sha"] = "f" * 40
    elif mismatch == "candidate_release.next_action_execution_base_sha":
        state["candidate_release"]["next_action_execution_base_sha"] = "f" * 40
    elif mismatch == "accepted_gates.R2.status":
        state["accepted_gates"][0]["status"] = "OPEN"
    elif mismatch == "accepted_gates.R2.tested_sha":
        state["accepted_gates"][0]["tested_sha"] = "f" * 40
    elif mismatch == "next_action.R3":
        state["next_action"] = state["next_action"].replace("R3", "R2")
    elif mismatch == "next_action.execution_base_sha":
        state["next_action"] = state["next_action"].replace(R2_SHA, "f" * 40)

    expected_after_failure = copy.deepcopy(state)
    with pytest.raises(CanonicalReconciliationError):
        reconcile_phase7_node2_r2_frontier(
            state,
            project_id=project_id,
            latest_accepted_product_sha=latest,
        )

    assert state == expected_after_failure
    if mismatch == "project_id":
        assert state == original


def test_r2_partial_frontier_accepts_the_stated_gate_id_spelling():
    state = _partial_r2_state()
    state["accepted_gates"][0]["gate"] = "P7N2-DISCOVERY-MARKETPLACE_R2"

    action, updated = reconcile_phase7_node2_r2_frontier(
        state,
        project_id="lari",
        latest_accepted_product_sha=R2_SHA,
    )

    assert action == "RECONCILED_PHASE7_NODE2_R2_FRONTIER"
    assert updated["next_action_execution_base_sha"] == R2_SHA


def test_reconciliation_wrapper_returns_human_required_without_mutation_on_mismatch(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    state_path = control / "docs" / "project-control" / "STATE.json"
    state_path.parent.mkdir(parents=True)
    state = _partial_r2_state()
    state["candidate_release"]["accepted_product_sha"] = "f" * 40
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()

    descriptor_path = tmp_path / "lari.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "project_id": "lari",
                "repository": "MertSGI/Randapp-main",
                "control_ref": "control/lari-project-control-plane",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cr, "_git", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, "d" * 40))
    monkeypatch.setattr(cr, "derive_latest_accepted_product_sha", lambda *args: (R2_SHA, "receipt.json", 999))
    monkeypatch.setattr(cr, "find_state_json", lambda *args: state_path)

    result = reconcile_missing_execution_base(
        descriptor_path=descriptor_path,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
    )

    assert result["status"] == "HUMAN_REQUIRED"
    assert result["reason"] == "CANONICAL_EXECUTION_BASE_CONFLICT"
    assert "candidate_release.accepted_product_sha" in result["detail"]
    assert state_path.read_bytes() == before


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


def test_record_slice_acceptance_writes_complete_r3_frontier(tmp_path: Path, monkeypatch):
    from aos import acceptance_receipt, canonical_reconciler as cr, cross_lane_coordinator

    candidate_sha = "c" * 40
    control_sha = "d" * 40
    transition_sha = "e" * 40
    control = tmp_path / "control"
    state_path = control / "docs" / "project-control" / "STATE.json"
    state_path.parent.mkdir(parents=True)
    state = _partial_r2_state()
    state["current_status"] = "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_BOUND_READY"
    state["current_milestone"] = "Program V2 Phase 7 — Node 2 Discovery Marketplace R2"
    state["next_action"] = f"Implement Phase 7 Node 2 R2 from accepted R1 base {R1_SHA}."
    state["candidate_release"]["accepted_product_sha"] = R1_SHA
    state["candidate_release"]["next_action_execution_base_sha"] = R1_SHA
    state_path.write_text(json.dumps(state), encoding="utf-8")

    descriptor_path = tmp_path / "lari.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "project_id": "lari",
                "repository": "MertSGI/Randapp-main",
                "control_ref": "control/lari-project-control-plane",
            }
        ),
        encoding="utf-8",
    )
    committed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal committed
        args = list(args)
        stdout = ""
        if args[:2] == ["ls-files", "*STATE*.json"]:
            stdout = "docs/project-control/STATE.json\n"
        elif args[:3] == ["diff", "--name-only", "--cached"]:
            stdout = "docs/project-control/STATE.json\n"
        elif args[:2] == ["rev-parse", "FETCH_HEAD"]:
            stdout = (transition_sha if committed else control_sha) + "\n"
        elif args[:2] == ["rev-parse", "HEAD"]:
            stdout = (transition_sha if committed else control_sha) + "\n"
        elif "commit" in args:
            committed = True
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, control_sha))
    monkeypatch.setattr(cr, "_git", fake_git)
    monkeypatch.setattr(acceptance_receipt, "write_acceptance_receipt", lambda *args: state_path)
    monkeypatch.setattr(cross_lane_coordinator, "evaluate_downstream_gates", lambda *args: None)

    result = record_slice_acceptance(
        descriptor_path=descriptor_path,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
        candidate_sha=candidate_sha,
        execution_base_sha=R1_SHA,
        ci_evidence={"run_id": "123"},
        slice_id="discovery-marketplace_r2",
        acceptance_receipt=object(),
    )

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "ACCEPTED"
    assert result["control_transition_sha"] == transition_sha
    assert written["current_status"] == "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY"
    assert candidate_sha in written["next_action"]
    assert written["next_action_execution_base_sha"] == candidate_sha
    assert written["next_product_action"]["slice"] == "R3_CUSTOMER_FACING_DISCOVERY_PORTFOLIO_UI"
    assert written["next_product_action"]["execution_base_sha"] == candidate_sha
    assert written["candidate_release"]["accepted_product_sha"] == candidate_sha
    assert written["candidate_release"]["next_action_execution_base_sha"] == candidate_sha
    assert written["phase7_accepted_execution_chain"]["node2_r1"] == R1_SHA
    assert written["phase7_accepted_execution_chain"]["node2_r2"] == candidate_sha
    assert written["phase7_node2_contract"]["execution_base_sha"] == R1_SHA
    assert written["phase7_node2_contract"]["r1_server_authority"]["product_sha"] == R1_SHA
    assert written["phase7_node2_contract"]["delivery_slices"] == {
        "R1": "ACCEPTED_PROVEN",
        "R2": "ACCEPTED_PROVEN",
        "R3": "BOUND_READY_FOR_IMPLEMENTATION",
    }
    assert written["phase7_node2_contract"]["ui_v2"] == "RELEASED_ACTIVE"
