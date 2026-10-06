import copy
import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from aos.canonical_reconciler import (
    CanonicalReconciliationError,
    PHASE7_NODE2_R2_ACCEPTED_SHA,
    PHASE7_NODE2_R2_GATE,
    PHASE7_NODE2_R3_ACCEPTED_SHA,
    PHASE7_NODE2_R3_CI_RUN_ID,
    PHASE7_NODE2_R3_CONTROLLER_DECISION,
    PHASE7_NODE2_R3_GATE,
    PHASE7_NODE3_PREBIND_STATUS,
    PHASE7_NODE3_R1_ACCEPTED_SHA,
    PHASE7_NODE3_R1_BRANCH,
    PHASE7_NODE3_R1_CI_RUN_ID,
    PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
    PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
    PHASE7_NODE3_R1_GATE,
    PHASE7_NODE3_R2_AUTHORIZED_STATUS,
    _phase7_node3_r1_accepted_registry,
    _phase7_node3_r1_accepted_state,
    _phase7_node2_r3_accepted_registry,
    _phase7_node2_r3_accepted_state,
    bind_missing_execution_base,
    derive_latest_accepted_product_sha,
    record_phase7_node2_r3_acceptance,
    record_phase7_node3_r1_acceptance,
    reconcile_phase7_node2_r2_frontier,
    reconcile_missing_execution_base,
    record_slice_acceptance,
)
from aos.knowledge.ingress import ingest_accepted_work
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import (
    record_implementation_receipt,
    record_verification_receipt,
)


R1_SHA = "814e3ca0c09c3a484e20869f1a47a3545259f6db"
R2_SHA = PHASE7_NODE2_R2_ACCEPTED_SHA
R3_SHA = PHASE7_NODE2_R3_ACCEPTED_SHA


def _accepted_work_ledger(
    tmp_path: Path,
    candidate_sha: str,
    *,
    project_id: str = "lari",
) -> KnowledgeLedger:
    ledger = KnowledgeLedger(tmp_path / f"knowledge-{candidate_sha[:8]}")
    ingest_accepted_work(
        ledger,
        project_id=project_id,
        agent_class="CODEX",
        tool_name="pytest",
        base_sha="a" * 40,
        result_sha=candidate_sha,
        repository="MertSGI/Randapp-main",
        branch="test/accepted-work",
        module_ids=["canonical-acceptance"],
        changed_paths=["product/candidate.py"],
        evidence_refs=["ci-run:123"],
        verification_status="SUCCESS",
        canonical_next_action="Canonical acceptance",
        idempotency_key=f"accepted-{candidate_sha}",
    )
    return ledger


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


def _r3_bound_state() -> dict:
    _, state = reconcile_phase7_node2_r2_frontier(
        _partial_r2_state(),
        project_id="lari",
        latest_accepted_product_sha=R2_SHA,
    )
    state["production_status"] = "NO_GO"
    return state


def _phase7_registry() -> dict:
    return {
        "additional_program_capabilities": [
            {
                "key": "verified_reviews",
                "source_state": "PLANNED",
                "program_maturity": "PLANNED",
                "preserved": "verified",
            },
            {
                "key": "discovery_marketplace",
                "source_state": "PLANNED",
                "program_maturity": "PLANNED",
                "execution_base_sha": "2b5e08d2b8dc674dd1dd21ea93f1b967ec468201",
                "execution_authority": "DECISION-022",
                "delivery_slice": "R1_SERVER_AUTHORITY_FIRST",
            },
            {
                "key": "favorites_rebooking",
                "source_state": "PLANNED",
                "program_maturity": "PLANNED",
                "preserved": "favorites",
            },
        ],
        "current_live_commercial_registry": [
            {"key": "unrelated_live", "source_state": "LIVE"}
        ],
    }


def _node3_prebind_state() -> dict:
    return _phase7_node2_r3_accepted_state(
        _r3_bound_state(),
        candidate_sha=PHASE7_NODE2_R3_ACCEPTED_SHA,
        execution_base_sha=PHASE7_NODE2_R2_ACCEPTED_SHA,
        ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
        ci_conclusion="success",
        controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
    )


def _node3_accepted_work_ledger(tmp_path: Path) -> KnowledgeLedger:
    ledger = KnowledgeLedger(tmp_path / "node3-knowledge")
    ingest_accepted_work(
        ledger,
        project_id="lari",
        agent_class="CODEX",
        tool_name="codex.controller-authorized-implementation",
        base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        result_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
        repository="MertSGI/Randapp-main",
        branch=PHASE7_NODE3_R1_BRANCH,
        module_ids=["phase7-node3-favorites", "phase7-node3-fast-rebooking"],
        changed_paths=["supabase/migrations/node3.sql"],
        evidence_refs=[f"github-actions-run:{PHASE7_NODE3_R1_CI_RUN_ID}"],
        verification_status="SUCCESS",
        canonical_next_action="Controller review before canonical acceptance",
        idempotency_key=(
            f"lari:p7n3-r1:{PHASE7_NODE3_R1_ACCEPTED_SHA}:accepted-work"
        ),
    )
    return ledger


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


def test_exact_r3_bound_state_accepts_and_advances_only_to_node3_prebind():
    state = _r3_bound_state()
    historical = {
        "node2_r1": state["phase7_accepted_execution_chain"]["node2_r1"],
        "node2_r2": state["phase7_accepted_execution_chain"]["node2_r2"],
        "contract_base": state["phase7_node2_contract"]["execution_base_sha"],
        "r1_authority": copy.deepcopy(state["phase7_node2_contract"]["r1_server_authority"]),
        "node3": state["phase7_node2_contract"]["node3"],
        "ui_v2": state["phase7_node2_contract"]["ui_v2"],
    }

    accepted = _phase7_node2_r3_accepted_state(
        state,
        candidate_sha=R3_SHA,
        execution_base_sha=R2_SHA,
        ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
        ci_conclusion="success",
        controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
    )

    assert state["current_status"] == "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY"
    assert accepted["current_status"] == PHASE7_NODE3_PREBIND_STATUS
    assert accepted["current_milestone"] == "Program V2 Phase 7 — Node 3 Favorites & Fast Rebooking Prebind"
    assert accepted["next_product_action"]["phase"] == "PHASE_7"
    assert accepted["next_product_action"]["node"] == "NODE_3_FAVORITES_FAST_REBOOKING"
    assert accepted["next_product_action"]["slice"] == "PREBIND_REQUIRED"
    assert accepted["next_product_action"]["execution_base_sha"] == R3_SHA
    assert accepted["next_action_execution_base_sha"] == R3_SHA
    assert accepted["candidate_release"]["accepted_product_sha"] == R3_SHA
    assert accepted["candidate_release"]["next_action_execution_base_sha"] == R3_SHA
    assert accepted["phase7_accepted_execution_chain"]["node2_r3"] == R3_SHA
    assert accepted["phase7_node2_contract"]["delivery_slices"]["R3"] == "ACCEPTED_PROVEN"
    assert accepted["phase7_accepted_execution_chain"]["node2_r1"] == historical["node2_r1"]
    assert accepted["phase7_accepted_execution_chain"]["node2_r2"] == historical["node2_r2"]
    assert accepted["phase7_node2_contract"]["execution_base_sha"] == historical["contract_base"]
    assert accepted["phase7_node2_contract"]["r1_server_authority"] == historical["r1_authority"]
    assert accepted["phase7_node2_contract"]["node3"] == historical["node3"] == "NOT_IN_SCOPE"
    assert accepted["phase7_node2_contract"]["ui_v2"] == historical["ui_v2"] == "RELEASED_ACTIVE"
    assert "visual" not in accepted["phase7_node2_contract"]
    assert "productization" not in accepted["phase7_node2_contract"]
    assert accepted["production_status"] == "NO_GO"
    gates = [gate for gate in accepted["accepted_gates"] if gate["gate"] == PHASE7_NODE2_R3_GATE]
    assert gates == [{
        "gate": PHASE7_NODE2_R3_GATE,
        "status": "CLOSED_PROVEN",
        "evidence_level": "E2_EXECUTABLE_EXACT_SHA_CI",
        "tested_sha": R3_SHA,
        "run_ids": [str(PHASE7_NODE2_R3_CI_RUN_ID)],
        "closed_at": gates[0]["closed_at"],
        "reopen_condition": "Failed exact-SHA R3 contract verification or CI regression",
    }]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_sha", "f" * 40),
        ("ci_run_id", PHASE7_NODE2_R3_CI_RUN_ID + 1),
        ("ci_conclusion", "failure"),
    ],
)
def test_r3_acceptance_evidence_mismatch_fails_without_state_mutation(field: str, value):
    state = _r3_bound_state()
    original = copy.deepcopy(state)
    kwargs = {
        "candidate_sha": R3_SHA,
        "execution_base_sha": R2_SHA,
        "ci_run_id": PHASE7_NODE2_R3_CI_RUN_ID,
        "ci_conclusion": "success",
        "controller_authority": PHASE7_NODE2_R3_CONTROLLER_DECISION,
    }
    kwargs[field] = value

    with pytest.raises(CanonicalReconciliationError):
        _phase7_node2_r3_accepted_state(state, **kwargs)

    assert state == original


def test_r3_gate_cannot_be_accepted_twice():
    state = _r3_bound_state()
    state["accepted_gates"].append({"gate": PHASE7_NODE2_R3_GATE})
    original = copy.deepcopy(state)
    with pytest.raises(CanonicalReconciliationError, match="R3_duplicate"):
        _phase7_node2_r3_accepted_state(
            state,
            candidate_sha=R3_SHA,
            execution_base_sha=R2_SHA,
            ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
            ci_conclusion="success",
            controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
        )
    assert state == original


def test_r3_registry_updates_only_accepted_capabilities_and_preserves_node3_planned():
    registry = _phase7_registry()
    original_live = copy.deepcopy(registry["current_live_commercial_registry"])

    accepted = _phase7_node2_r3_accepted_registry(registry)
    items = {item["key"]: item for item in accepted["additional_program_capabilities"]}

    assert registry["additional_program_capabilities"][0]["source_state"] == "PLANNED"
    assert accepted["current_live_commercial_registry"] == original_live
    assert items["verified_reviews"]["source_state"] == "LIVE_ACCEPTANCE_ONLY"
    assert items["verified_reviews"]["program_maturity"] == "REAL_CODE_NOT_LIVE_VERIFIED"
    assert items["verified_reviews"]["preserved"] == "verified"
    assert items["discovery_marketplace"]["source_state"] == "LIVE_ACCEPTANCE_ONLY"
    assert items["discovery_marketplace"]["program_maturity"] == "REAL_CODE_NOT_LIVE_VERIFIED"
    assert items["discovery_marketplace"]["accepted_r3_sha"] == R3_SHA
    assert items["discovery_marketplace"]["accepted_r3_ci_run_id"] == PHASE7_NODE2_R3_CI_RUN_ID
    assert items["discovery_marketplace"]["delivery_slice"] == "R3_ACCEPTED_NODE3_PREBIND"
    assert items["discovery_marketplace"]["execution_base_sha"] == "2b5e08d2b8dc674dd1dd21ea93f1b967ec468201"
    assert items["favorites_rebooking"] == registry["additional_program_capabilities"][2]
    assert items["favorites_rebooking"]["source_state"] == "PLANNED"
    assert items["favorites_rebooking"]["program_maturity"] == "PLANNED"


def _r3_acceptance_control_repo(control: Path) -> tuple[Path, bytes]:
    state_path = control / "docs/project-control/STATE.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_r3_bound_state()), encoding="utf-8")
    (state_path.parent / "PROGRAM_V2_CAPABILITY_REGISTRY.json").write_text(
        json.dumps(_phase7_registry()), encoding="utf-8"
    )
    (state_path.parent / "DECISIONS.md").write_text(
        "# Decisions\n\n## DECISION-022: Existing Node 2 Authority\n- **Status**: ACCEPTED\n",
        encoding="utf-8",
    )
    r2_receipt = state_path.parent / "acceptance-receipt-discovery_marketplace_r2.json"
    r2_receipt.write_text('{"receipt_id":"r2-preserved"}\n', encoding="utf-8")
    r2_before = r2_receipt.read_bytes()
    _git(control, "init")
    _git(control, "add", ".")
    _git(
        control, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid",
        "commit", "-m", "control base"
    )
    return state_path, r2_before


def test_record_r3_acceptance_writes_exact_four_control_files(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    state_path, r2_before = _r3_acceptance_control_repo(control)
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
    real_git = cr._git
    pushed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal pushed
        args = list(args)
        if args[:3] == ["fetch", "origin", "control/lari-project-control-plane"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            stdout = (_git(control, "rev-parse", "HEAD") if pushed else cr.PHASE7_NODE2_R3_CONTROL_BASE_SHA) + "\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[:2] == ["push", "origin"]:
            pushed = True
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_git(args, cwd=cwd, check=check, timeout=timeout)

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, cr.PHASE7_NODE2_R3_CONTROL_BASE_SHA))
    monkeypatch.setattr(cr, "_remote_branch_sha", lambda *args: R3_SHA)
    monkeypatch.setattr(cr, "_read_github_actions_run", lambda *args: {
        "id": PHASE7_NODE2_R3_CI_RUN_ID,
        "head_sha": R3_SHA,
        "head_branch": cr.PHASE7_NODE2_R3_BRANCH,
        "status": "completed",
        "conclusion": "success",
    })
    monkeypatch.setattr(cr, "_git", fake_git)

    result = record_phase7_node2_r3_acceptance(
        descriptor_path=descriptor,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
        candidate_sha=R3_SHA,
        execution_base_sha=R2_SHA,
        ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
        controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
        knowledge_ledger=_accepted_work_ledger(tmp_path, R3_SHA),
    )

    changed = set(_git(control, "show", "--pretty=format:", "--name-only", "HEAD").splitlines())
    assert changed == cr.PHASE7_NODE2_R3_ALLOWED_CONTROL_FILES
    assert result["status"] == "ACCEPTED"
    assert result["control_remote_sha_equal"] is True
    assert (state_path.parent / "acceptance-receipt-discovery_marketplace_r2.json").read_bytes() == r2_before
    receipt = json.loads((state_path.parent / "acceptance-receipt-discovery_marketplace_r3.json").read_text())
    assert receipt["execution_base_sha"] == R2_SHA
    assert receipt["candidate_sha"] == R3_SHA
    assert receipt["ci_run_id"] == PHASE7_NODE2_R3_CI_RUN_ID
    assert receipt["controller_authority"] == PHASE7_NODE2_R3_CONTROLLER_DECISION
    assert receipt["control_sha_before"] == cr.PHASE7_NODE2_R3_CONTROL_BASE_SHA
    assert receipt["canonical_control_transition_sha"] == cr.PHASE7_NODE2_R3_CONTROL_BASE_SHA
    assert receipt["production"] == "NO_GO"
    decisions = (state_path.parent / "DECISIONS.md").read_text(encoding="utf-8")
    assert decisions.count("DECISION-023") == 1
    assert "does not close broader UI-V2 visual or productization work" in decisions


def test_record_r3_acceptance_starting_control_drift_fails_before_mutation(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    state_path, _ = _r3_acceptance_control_repo(control)
    before = {path: path.read_bytes() for path in state_path.parent.iterdir()}
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, "f" * 40))

    with pytest.raises(CanonicalReconciliationError, match="starting control SHA drift"):
        record_phase7_node2_r3_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=R3_SHA,
            execution_base_sha=R2_SHA,
            ci_run_id=PHASE7_NODE2_R3_CI_RUN_ID,
            controller_authority=PHASE7_NODE2_R3_CONTROLLER_DECISION,
            knowledge_ledger=_accepted_work_ledger(tmp_path, R3_SHA),
        )

    assert {path: path.read_bytes() for path in state_path.parent.iterdir()} == before


def _node3_acceptance_control_repo(control: Path) -> tuple[Path, dict[Path, bytes]]:
    control_dir = control / "docs/project-control"
    control_dir.mkdir(parents=True)
    state = _node3_prebind_state()
    state["parallel_lanes"] = {"ui_v2": {"status": "ACTIVE", "branch": "ui-v2-independent"}}
    state_path = control_dir / "STATE.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    registry = _phase7_node2_r3_accepted_registry(_phase7_registry())
    (control_dir / "PROGRAM_V2_CAPABILITY_REGISTRY.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    (control_dir / "DECISIONS.md").write_text(
        "# Decisions\n\n## DECISION-023: Existing Node 2 R3 Acceptance\n- **Status**: ACCEPTED\n",
        encoding="utf-8",
    )
    for name in (
        "acceptance-receipt-discovery_marketplace_r2.json",
        "acceptance-receipt-discovery_marketplace_r3.json",
    ):
        (control_dir / name).write_text(
            json.dumps({"receipt_id": f"preserved-{name}"}) + "\n", encoding="utf-8"
        )
    historical = {
        path: path.read_bytes() for path in control_dir.glob("acceptance-receipt-*.json")
    }
    _git(control, "init")
    _git(control, "add", ".")
    _git(
        control, "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid",
        "commit", "-m", "node3 control base"
    )
    return state_path, historical


def _node3_descriptor(path: Path) -> Path:
    path.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
    return path


def _node3_ci(**overrides) -> dict:
    value = {
        "id": PHASE7_NODE3_R1_CI_RUN_ID,
        "head_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        "head_branch": PHASE7_NODE3_R1_BRANCH,
        "status": "completed",
        "conclusion": "success",
    }
    value.update(overrides)
    return value


def test_record_node3_r1_acceptance_is_exact_and_authorizes_r2(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr, cross_lane_coordinator

    control = tmp_path / "control"
    state_path, historical = _node3_acceptance_control_repo(control)
    descriptor = _node3_descriptor(tmp_path / "lari.json")
    state_before = json.loads(state_path.read_text(encoding="utf-8"))
    registry_path = state_path.parent / "PROGRAM_V2_CAPABILITY_REGISTRY.json"
    registry_before = json.loads(registry_path.read_text(encoding="utf-8"))
    real_git = cr._git
    pushed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal pushed
        args = list(args)
        if args[:3] == ["fetch", "origin", "control/lari-project-control-plane"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            observed = _git(control, "rev-parse", "HEAD") if pushed else cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
            return subprocess.CompletedProcess(args, 0, observed + "\n", "")
        if args[:2] == ["push", "origin"]:
            pushed = True
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_git(args, cwd=cwd, check=check, timeout=timeout)

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (
        control, cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
    ))
    monkeypatch.setattr(cr, "_remote_branch_sha", lambda *args: PHASE7_NODE3_R1_ACCEPTED_SHA)
    monkeypatch.setattr(cr, "_assert_exact_single_commit_lineage", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "_read_github_actions_run", lambda *args: _node3_ci())
    monkeypatch.setattr(cr, "_git", fake_git)
    monkeypatch.setattr(cross_lane_coordinator, "evaluate_downstream_gates", lambda *args: [])

    result = record_phase7_node3_r1_acceptance(
        descriptor_path=descriptor,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
        candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
        execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
        controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
        knowledge_ledger=_node3_accepted_work_ledger(tmp_path),
    )

    changed = set(_git(control, "show", "--pretty=format:", "--name-only", "HEAD").splitlines())
    assert changed == cr.PHASE7_NODE3_R1_ALLOWED_CONTROL_FILES
    assert result["status"] == "ACCEPTED"
    assert result["control_remote_sha_equal"] is True
    assert result["accepted_gate"] == PHASE7_NODE3_R1_GATE
    assert result["next_action_execution_base_sha"] == PHASE7_NODE3_R1_ACCEPTED_SHA
    assert result["downstream_activation"]["status"] == "NO_TRANSITION_REQUIRED"
    assert result["kcp_implementation_receipt_ids"] == [
        cr.PHASE7_NODE3_R1_IMPLEMENTATION_RECEIPT_ID
    ]
    assert result["kcp_verification_receipt_ids"] == [
        cr.PHASE7_NODE3_R1_VERIFICATION_RECEIPT_ID
    ]

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["current_status"] == PHASE7_NODE3_R2_AUTHORIZED_STATUS
    assert written["next_action_execution_base_sha"] == PHASE7_NODE3_R1_ACCEPTED_SHA
    assert written["next_product_action"] == {
        **state_before["next_product_action"],
        "phase": "PHASE_7",
        "node": "NODE_3_FAVORITES_FAST_REBOOKING",
        "slice": "R2_PRODUCT_INTEGRATION_AUTHORIZED",
        "execution_base_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
    }
    assert written["phase7_accepted_execution_chain"] == {
        **state_before["phase7_accepted_execution_chain"],
        "node3_r1": PHASE7_NODE3_R1_ACCEPTED_SHA,
    }
    assert written["phase7_node2_contract"] == state_before["phase7_node2_contract"]
    assert written["parallel_lanes"] == state_before["parallel_lanes"]
    contract = written["phase7_node3_contract"]
    assert contract["authority"] == "DECISION-024"
    assert contract["r1_server_foundation"] == {
        "status": "ACCEPTED_PROVEN",
        "product_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        "ci_run_id": PHASE7_NODE3_R1_CI_RUN_ID,
    }
    assert contract["delivery_slices"] == {"R1": "ACCEPTED_PROVEN", "R2": "AUTHORIZED"}
    assert contract["production"] == "NO_GO"
    node3_gates = [gate for gate in written["accepted_gates"] if gate["gate"] == PHASE7_NODE3_R1_GATE]
    assert len(node3_gates) == 1
    assert node3_gates[0]["gate"].startswith("P7N3-")
    assert not node3_gates[0]["gate"].startswith("P7N2-")
    assert node3_gates[0]["tested_sha"] == PHASE7_NODE3_R1_ACCEPTED_SHA
    assert node3_gates[0]["run_ids"] == [str(PHASE7_NODE3_R1_CI_RUN_ID)]
    assert written["production_status"] == "NO_GO"

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    before_items = {item["key"]: item for item in registry_before["additional_program_capabilities"]}
    items = {item["key"]: item for item in registry["additional_program_capabilities"]}
    assert items["verified_reviews"] == before_items["verified_reviews"]
    assert items["discovery_marketplace"] == before_items["discovery_marketplace"]
    assert items["favorites_rebooking"]["source_state"] == "LIVE_ACCEPTANCE_ONLY"
    assert items["favorites_rebooking"]["program_maturity"] == "REAL_CODE_NOT_LIVE_VERIFIED"
    assert items["favorites_rebooking"]["delivery_slice"] == "R1_ACCEPTED_R2_AUTHORIZED"
    assert items["favorites_rebooking"]["accepted_r1_sha"] == PHASE7_NODE3_R1_ACCEPTED_SHA

    for path, before in historical.items():
        assert path.read_bytes() == before
    receipt = json.loads(
        (state_path.parent / "acceptance-receipt-favorites_rebooking_r1.json").read_text()
    )
    assert receipt["project_id"] == "lari"
    assert receipt["lane"] == "lane-b"
    assert receipt["slice_id"] == "favorites_rebooking_r1"
    assert receipt["execution_base_sha"] == PHASE7_NODE3_R1_EXECUTION_BASE_SHA
    assert receipt["candidate_sha"] == PHASE7_NODE3_R1_ACCEPTED_SHA
    assert receipt["ci_run_id"] == PHASE7_NODE3_R1_CI_RUN_ID
    assert receipt["control_sha_before"] == cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
    assert receipt["production"] == "NO_GO"
    decisions = (state_path.parent / "DECISIONS.md").read_text(encoding="utf-8")
    assert decisions.count("DECISION-024") == 1
    assert "current-truth seed only" in decisions
    assert "R2 Product Integration is authorized" in decisions


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"candidate_sha": "c" * 40}, "candidate SHA mismatch"),
        ({"execution_base_sha": "b" * 40}, "execution-base SHA mismatch"),
        ({"ci_run_id": 1}, "CI run mismatch"),
        ({"controller_authority": "WRONG"}, "Controller authority mismatch"),
    ],
)
def test_record_node3_r1_acceptance_rejects_wrong_exact_authority(
    tmp_path: Path, monkeypatch, override: dict, message: str
):
    from aos import canonical_reconciler as cr

    clone_called = False

    def forbidden_clone(*args, **kwargs):
        nonlocal clone_called
        clone_called = True
        raise AssertionError("control checkout must not be created")

    monkeypatch.setattr(cr, "_ensure_control_clone", forbidden_clone)
    arguments = {
        "descriptor_path": _node3_descriptor(tmp_path / "lari.json"),
        "product_workspace": tmp_path / "product",
        "runtime_dir": tmp_path / "runtime",
        "candidate_sha": PHASE7_NODE3_R1_ACCEPTED_SHA,
        "execution_base_sha": PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
        "ci_run_id": PHASE7_NODE3_R1_CI_RUN_ID,
        "controller_authority": PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
        "knowledge_ledger": _node3_accepted_work_ledger(tmp_path),
    }
    arguments.update(override)
    with pytest.raises(CanonicalReconciliationError, match=message):
        record_phase7_node3_r1_acceptance(**arguments)
    assert clone_called is False


@pytest.mark.parametrize("ledger_kind", ["missing", "wrong_sha"])
def test_record_node3_r1_acceptance_kcp_failure_has_zero_control_writes(
    tmp_path: Path, monkeypatch, ledger_kind: str
):
    from aos import canonical_reconciler as cr

    clone_called = False

    def forbidden_clone(*args, **kwargs):
        nonlocal clone_called
        clone_called = True
        raise AssertionError("control checkout must not be created")

    monkeypatch.setattr(cr, "_ensure_control_clone", forbidden_clone)
    ledger = (
        KnowledgeLedger(tmp_path / "empty")
        if ledger_kind == "missing"
        else _accepted_work_ledger(tmp_path, "a" * 40)
    )
    with pytest.raises(CanonicalReconciliationError, match="KCP_ACCEPTED_WORK_COVERAGE_REQUIRED"):
        record_phase7_node3_r1_acceptance(
            descriptor_path=_node3_descriptor(tmp_path / "lari.json"),
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
            execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
            controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
            knowledge_ledger=ledger,
        )
    assert clone_called is False


def test_record_node3_r1_acceptance_rejects_control_base_drift_before_mutation(
    tmp_path: Path, monkeypatch
):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    state_path, _ = _node3_acceptance_control_repo(control)
    before = {path: path.read_bytes() for path in state_path.parent.iterdir()}
    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, "f" * 40))
    with pytest.raises(CanonicalReconciliationError, match="starting control SHA drift"):
        record_phase7_node3_r1_acceptance(
            descriptor_path=_node3_descriptor(tmp_path / "lari.json"),
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
            execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
            controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
            knowledge_ledger=_node3_accepted_work_ledger(tmp_path),
        )
    assert {path: path.read_bytes() for path in state_path.parent.iterdir()} == before


@pytest.mark.parametrize(
    "ci_override",
    [
        {"id": 1},
        {"head_sha": "c" * 40},
        {"head_branch": "wrong-branch"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
    ],
)
def test_record_node3_r1_acceptance_requires_exact_hosted_ci(
    tmp_path: Path, monkeypatch, ci_override: dict
):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    state_path, _ = _node3_acceptance_control_repo(control)
    before = {path: path.read_bytes() for path in state_path.parent.iterdir()}
    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (
        control, cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
    ))
    monkeypatch.setattr(cr, "_remote_branch_sha", lambda *args: PHASE7_NODE3_R1_ACCEPTED_SHA)
    monkeypatch.setattr(cr, "_assert_exact_single_commit_lineage", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "_read_github_actions_run", lambda *args: _node3_ci(**ci_override))
    with pytest.raises(CanonicalReconciliationError, match="hosted CI"):
        record_phase7_node3_r1_acceptance(
            descriptor_path=_node3_descriptor(tmp_path / "lari.json"),
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
            execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
            controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
            knowledge_ledger=_node3_accepted_work_ledger(tmp_path),
        )
    assert {path: path.read_bytes() for path in state_path.parent.iterdir()} == before


def test_record_node3_r1_acceptance_rejects_concurrent_remote_drift(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    _node3_acceptance_control_repo(control)
    real_git = cr._git
    committed = False
    pushed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal committed, pushed
        args = list(args)
        if args[:3] == ["fetch", "origin", "control/lari-project-control-plane"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            return subprocess.CompletedProcess(args, 0, "f" * 40 + "\n", "")
        if "commit" in args:
            committed = True
        if args[:2] == ["push", "origin"]:
            pushed = True
        return real_git(args, cwd=cwd, check=check, timeout=timeout)

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (
        control, cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
    ))
    monkeypatch.setattr(cr, "_remote_branch_sha", lambda *args: PHASE7_NODE3_R1_ACCEPTED_SHA)
    monkeypatch.setattr(cr, "_assert_exact_single_commit_lineage", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "_read_github_actions_run", lambda *args: _node3_ci())
    monkeypatch.setattr(cr, "_git", fake_git)
    with pytest.raises(CanonicalReconciliationError, match="Concurrent control drift"):
        record_phase7_node3_r1_acceptance(
            descriptor_path=_node3_descriptor(tmp_path / "lari.json"),
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
            execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
            controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
            knowledge_ledger=_node3_accepted_work_ledger(tmp_path),
        )
    assert committed is False
    assert pushed is False


def test_record_node3_r1_acceptance_verifies_remote_after_push(tmp_path: Path, monkeypatch):
    from aos import canonical_reconciler as cr

    control = tmp_path / "control"
    _node3_acceptance_control_repo(control)
    real_git = cr._git
    pushed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal pushed
        args = list(args)
        if args[:3] == ["fetch", "origin", "control/lari-project-control-plane"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            observed = "f" * 40 if pushed else cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
            return subprocess.CompletedProcess(args, 0, observed + "\n", "")
        if args[:2] == ["push", "origin"]:
            pushed = True
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_git(args, cwd=cwd, check=check, timeout=timeout)

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (
        control, cr.PHASE7_NODE3_R1_CONTROL_BASE_SHA
    ))
    monkeypatch.setattr(cr, "_remote_branch_sha", lambda *args: PHASE7_NODE3_R1_ACCEPTED_SHA)
    monkeypatch.setattr(cr, "_assert_exact_single_commit_lineage", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "_read_github_actions_run", lambda *args: _node3_ci())
    monkeypatch.setattr(cr, "_git", fake_git)
    with pytest.raises(CanonicalReconciliationError, match="push verification failed"):
        record_phase7_node3_r1_acceptance(
            descriptor_path=_node3_descriptor(tmp_path / "lari.json"),
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=PHASE7_NODE3_R1_ACCEPTED_SHA,
            execution_base_sha=PHASE7_NODE3_R1_EXECUTION_BASE_SHA,
            ci_run_id=PHASE7_NODE3_R1_CI_RUN_ID,
            controller_authority=PHASE7_NODE3_R1_CONTROLLER_AUTHORITY,
            knowledge_ledger=_node3_accepted_work_ledger(tmp_path),
        )
    assert pushed is True


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
    monkeypatch.setattr(cross_lane_coordinator, "evaluate_downstream_gates", lambda *args: [])

    receipt = acceptance_receipt.AcceptanceReceipt(
        receipt_id="receipt-r2",
        project_id="lari",
        lane="lane-b",
        slice_id="discovery-marketplace_r2",
        execution_base_sha=R1_SHA,
        candidate_sha=candidate_sha,
        ci_workflow_name="test.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="TEST-CONTROLLER",
        control_sha_before=control_sha,
    )
    result = record_slice_acceptance(
        descriptor_path=descriptor_path,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
        candidate_sha=candidate_sha,
        execution_base_sha=R1_SHA,
        ci_evidence={"run_id": "123"},
        slice_id="discovery-marketplace_r2",
        acceptance_receipt=receipt,
        knowledge_ledger=_accepted_work_ledger(tmp_path, candidate_sha),
    )

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "ACCEPTED"
    assert result["control_transition_sha"] == transition_sha
    assert result["control_sha_before"] == control_sha
    assert result["control_sha_after"] == transition_sha
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


def test_acceptance_without_kcp_coverage_cannot_touch_control_repo(tmp_path: Path, monkeypatch):
    from aos import acceptance_receipt, canonical_reconciler as cr

    control = tmp_path / "control"
    control.mkdir()
    sentinel = control / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
    clone_called = False

    def forbidden_clone(*_args, **_kwargs):
        nonlocal clone_called
        clone_called = True
        return control, "d" * 40

    monkeypatch.setattr(cr, "_ensure_control_clone", forbidden_clone)
    receipt = acceptance_receipt.AcceptanceReceipt(
        receipt_id="missing-kcp",
        project_id="lari",
        lane="lane-b",
        slice_id="r2",
        execution_base_sha=R1_SHA,
        candidate_sha="c" * 40,
        ci_workflow_name="test.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="TEST",
        control_sha_before="d" * 40,
    )

    with pytest.raises(CanonicalReconciliationError, match="KCP_ACCEPTED_WORK_COVERAGE_REQUIRED"):
        record_slice_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha="c" * 40,
            execution_base_sha=R1_SHA,
            ci_evidence={"run_id": "123"},
            slice_id="r2",
            acceptance_receipt=receipt,
            knowledge_ledger=KnowledgeLedger(tmp_path / "empty-knowledge"),
        )

    assert clone_called is False
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert list(control.iterdir()) == [sentinel]


@pytest.mark.parametrize("receipt_kind", ["wrong_sha", "implementation_only", "verification_only"])
def test_acceptance_kcp_gate_requires_both_exact_sha_receipts(
    tmp_path: Path,
    receipt_kind: str,
):
    from aos import canonical_reconciler as cr

    candidate_sha = "c" * 40
    ledger = KnowledgeLedger(tmp_path / receipt_kind)
    common = {
        "project_id": "lari",
        "agent_class": "CODEX",
        "tool_name": "pytest",
        "base_sha": "a" * 40,
        "result_sha": candidate_sha,
        "module_ids": ["acceptance"],
        "changed_paths": ["product/candidate.py"],
        "evidence_refs": ["ci-run:123"],
        "canonical_next_action": "Canonical acceptance",
    }
    if receipt_kind == "wrong_sha":
        ledger = _accepted_work_ledger(tmp_path, "b" * 40)
    elif receipt_kind == "implementation_only":
        record_implementation_receipt(
            ledger,
            idempotency_key="implementation-only",
            claims={},
            **common,
        )
    else:
        record_verification_receipt(
            ledger,
            idempotency_key="verification-only",
            verification={"status": "SUCCESS"},
            claims={},
            **common,
        )

    with pytest.raises(CanonicalReconciliationError, match="KCP_ACCEPTED_WORK_COVERAGE_REQUIRED"):
        cr._assert_canonical_acceptance_kcp_coverage(
            {"project_id": "lari"},
            candidate_sha=candidate_sha,
            knowledge_ledger=ledger,
        )


@pytest.mark.parametrize("token_env", [None, "GH_TOKEN", "GITHUB_TOKEN"])
def test_github_actions_request_auth_is_optional_and_secret_safe(
    monkeypatch,
    token_env: str | None,
):
    from aos import canonical_reconciler as cr

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    secret = "github-secret-value"
    if token_env:
        monkeypatch.setenv(token_env, secret)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id": 123, "conclusion": "success"}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(cr.urllib.request, "urlopen", fake_urlopen)
    result = cr._read_github_actions_run("MertSGI/AOS", 123)

    authorization = captured["request"].get_header("Authorization")
    assert authorization == (f"Bearer {secret}" if token_env else None)
    assert captured["timeout"] == 30
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("code", "headers", "classification"),
    [
        (401, {}, "GITHUB_AUTHENTICATION_FAILED"),
        (403, {}, "GITHUB_FORBIDDEN"),
        (403, {"X-RateLimit-Remaining": "0"}, "GITHUB_RATE_LIMITED"),
        (429, {"Retry-After": "10"}, "GITHUB_RATE_LIMITED"),
    ],
)
def test_github_actions_http_failures_are_bounded_and_sanitized(
    monkeypatch,
    code: int,
    headers: dict,
    classification: str,
):
    from aos import canonical_reconciler as cr

    secret = "must-not-leak"
    monkeypatch.setenv("GH_TOKEN", secret)

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/redacted",
            code,
            "failure",
            headers,
            io.BytesIO(b"ignored"),
        )

    monkeypatch.setattr(cr.urllib.request, "urlopen", fail)
    with pytest.raises(CanonicalReconciliationError, match=classification) as exc_info:
        cr._read_github_actions_run("MertSGI/AOS", 123)
    assert secret not in str(exc_info.value)


def test_registry_reconciliation_failure_blocks_commit_and_all_canonical_writes(
    tmp_path: Path,
    monkeypatch,
):
    from aos import acceptance_receipt, canonical_reconciler as cr

    candidate_sha = "c" * 40
    control_sha = "d" * 40
    control = tmp_path / "control"
    state_path = control / "docs" / "project-control" / "STATE.json"
    state_path.parent.mkdir(parents=True)
    state = _partial_r2_state()
    state["current_status"] = "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_BOUND_READY"
    state["current_milestone"] = "Program V2 Phase 7 Node 2 Discovery Marketplace R2"
    state["next_action"] = f"Implement Phase 7 Node 2 R2 from accepted R1 base {R1_SHA}."
    state["candidate_release"]["accepted_product_sha"] = R1_SHA
    state["candidate_release"]["next_action_execution_base_sha"] = R1_SHA
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()
    registry_path = state_path.parent / "PROGRAM_V2_CAPABILITY_REGISTRY.json"
    registry_path.write_text('{"current_live_commercial_registry": []}', encoding="utf-8")
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
    committed = False

    def fake_git(args, *, cwd, check=True, timeout=300):
        nonlocal committed
        args = list(args)
        if "commit" in args:
            committed = True
        stdout = "docs/project-control/STATE.json\n" if args[:2] == ["ls-files", "*STATE*.json"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, control_sha))
    monkeypatch.setattr(cr, "_git", fake_git)
    receipt = acceptance_receipt.AcceptanceReceipt(
        receipt_id="registry-failure",
        project_id="lari",
        lane="lane-b",
        slice_id="r2",
        execution_base_sha=R1_SHA,
        candidate_sha=candidate_sha,
        ci_workflow_name="test.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="TEST",
        control_sha_before=control_sha,
    )

    with pytest.raises(CanonicalReconciliationError, match="registry entry is missing"):
        record_slice_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=candidate_sha,
            execution_base_sha=R1_SHA,
            ci_evidence={"run_id": "123"},
            slice_id="r2",
            acceptance_receipt=receipt,
            knowledge_ledger=_accepted_work_ledger(tmp_path, candidate_sha),
        )

    assert committed is False
    assert state_path.read_bytes() == before
    assert not list(state_path.parent.glob("acceptance-receipt-*.json"))


def test_post_commit_downstream_failure_returns_explicit_hold(tmp_path: Path, monkeypatch):
    from aos import acceptance_receipt, canonical_reconciler as cr, cross_lane_coordinator

    candidate_sha = "c" * 40
    control_sha = "d" * 40
    transition_sha = "e" * 40
    control = tmp_path / "control"
    state_path = control / "docs" / "project-control" / "STATE.json"
    state_path.parent.mkdir(parents=True)
    state = _partial_r2_state()
    state["current_status"] = "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_BOUND_READY"
    state["current_milestone"] = "Program V2 Phase 7 Node 2 Discovery Marketplace R2"
    state["next_action"] = f"Implement Phase 7 Node 2 R2 from accepted R1 base {R1_SHA}."
    state["candidate_release"]["accepted_product_sha"] = R1_SHA
    state["candidate_release"]["next_action_execution_base_sha"] = R1_SHA
    state_path.write_text(json.dumps(state), encoding="utf-8")
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }), encoding="utf-8")
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
    monkeypatch.setattr(
        cross_lane_coordinator,
        "evaluate_downstream_gates",
        lambda *args: (_ for _ in ()).throw(
            cross_lane_coordinator.DownstreamActivationError(
                "continue-61be4ab1af53cfa646d773ce",
                "DOWNSTREAM_ACTIVATION_RUNTIMEERROR",
            )
        ),
    )
    receipt = acceptance_receipt.AcceptanceReceipt(
        receipt_id="downstream-hold",
        project_id="lari",
        lane="lane-b",
        slice_id="r2",
        execution_base_sha=R1_SHA,
        candidate_sha=candidate_sha,
        ci_workflow_name="test.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="TEST-CONTROLLER",
        control_sha_before=control_sha,
    )

    result = record_slice_acceptance(
        descriptor_path=descriptor,
        product_workspace=tmp_path / "product",
        runtime_dir=tmp_path / "runtime",
        candidate_sha=candidate_sha,
        execution_base_sha=R1_SHA,
        ci_evidence={"run_id": "123"},
        slice_id="r2",
        acceptance_receipt=receipt,
        knowledge_ledger=_accepted_work_ledger(tmp_path, candidate_sha),
    )

    assert committed is True
    assert result["status"] == "ACCEPTED_WITH_DOWNSTREAM_HOLD"
    assert result["control_sha_after"] == transition_sha
    assert result["downstream_activation"] == {
        "status": "HOLD",
        "classification": "DOWNSTREAM_ACTIVATION_RUNTIMEERROR",
        "command_id": "continue-61be4ab1af53cfa646d773ce",
    }
    recorded = json.loads((tmp_path / "runtime" / "slice-acceptance-r2.json").read_text())
    assert recorded == result
