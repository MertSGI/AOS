"""Tests for AOS Deliberation Council V1."""

import json
from pathlib import Path
from aos.providers.council import (
    DeliberationCouncilV1,
    assess_council_trigger,
    blind_proposals,
)


def json_str(data):
    return json.dumps(data)


def test_assess_council_trigger_deterministic_bypass():
    assessment = assess_council_trigger("READ_FILE", "Read path foo/bar.txt")
    assert not assessment.council_required
    assert assessment.trigger_reason == "DETERMINISTIC_BYPASS"


def test_assess_council_trigger_high_impact_architecture():
    assessment = assess_council_trigger(
        "ARCHITECTURE_DECISION",
        "Select architectural pattern for multi-lane productization tradeoff",
    )
    assert assessment.council_required
    assert assessment.trigger_reason == "MATERIAL_AMBIGUITY_OR_HIGH_IMPACT"


def test_blind_proposals_anonymization():
    raw = [
        ("nemotron_provider", {"model": "nemotron-4", "title": "Option A", "rationale": "Direct SQL"}),
        ("gemini_provider", {"model": "gemini-2.5", "title": "Option B", "rationale": "RPC Interface"}),
    ]
    blinded = blind_proposals(raw)
    assert len(blinded) == 2
    assert blinded[0].proposal_id == "Proposal A"
    assert blinded[1].proposal_id == "Proposal B"
    assert "model" not in blinded[0].raw_payload
    assert "nemotron" not in json_str(blinded[0].raw_payload)


def test_deliberation_council_shadow_evaluation(tmp_path=None):
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {
        "title": "Primary Architecture",
        "authority_id": "DECISION-020",
        "rationale": "Canonical RPC endpoint",
        "tasks": [{"node_id": "task-1", "action": "rpc_call"}],
    }
    alternate = (
        ("alt_member", {
            "title": "Alternate Architecture",
            "authority_id": "DECISION-020",
            "rationale": "Alternative table schema",
            "tasks": [{"node_id": "task-2", "action": "migration"}],
        }),
    )
    auths = {"DECISION-020": {}}
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide backend boundary for UI V2 slice",
        primary_proposal=primary,
        alternate_proposals=alternate,
        authority_records=auths,
        project_id="lari-ui-v2",
        command_id="continue-test-123",
        is_spare_capacity_available=True,
    )
    assert result.mode == "SHADOW_ONLY"
    assert result.quorum_reached
    assert result.winning_proposal_id in ("Proposal A", "Proposal B")
    assert council.shadow_sample_count == 1
    assert council.real_shadow_sample_count == 1
    assert council.trigger_count == 1

    # Check ledger persistence
    ledger_file = tmp_path / "deliberation-shadow-ledger.jsonl"
    assert ledger_file.exists()
    lines = ledger_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["decision_class"] == "ARCHITECTURE_DECISION"
    assert rec["project_id"] == "lari-ui-v2"
    assert rec["command_id"] == "continue-test-123"
    assert rec["execution_affected"] is False
    assert "primary_decision_fingerprint" in rec
    assert "council_agreement" in rec


def test_deliberation_council_catches_authority_violation():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2)
    primary_bad_auth = {
        "title": "Unapproved Proposal",
        "authority_id": "DECISION-999-UNAUTHORIZED",
        "tasks": [],
    }
    alternate_good_auth = (
        ("alt_good", {
            "title": "Approved Proposal",
            "authority_id": "DECISION-020",
            "tasks": [],
        }),
    )
    auths = {"DECISION-020": {}}
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Select pattern",
        primary_proposal=primary_bad_auth,
        alternate_proposals=alternate_good_auth,
        authority_records=auths,
    )
    # The council must reject Proposal A because its authority is missing, selecting Proposal B
    assert result.winning_proposal_id == "Proposal B"
    assert not result.agreement_with_primary
    assert council.policy_violations_caught >= 1
    assert council.canonical_contradictions_caught >= 0


def test_deliberation_council_skips_when_no_spare_capacity(tmp_path=None):
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {"title": "Primary Plan", "authority_id": "DECISION-020"}
    result = council.evaluate_decision(
        decision_type="REPLANNING",
        prompt="Replanning next batch",
        primary_proposal=primary,
        is_spare_capacity_available=False,  # e.g., provider backoff active
    )
    assert council.skipped_capacity_count == 1
    assert result.winning_proposal_id is None
    assert result.decision_record["council_trigger_reason"] == "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS"
    assert result.decision_record["execution_affected"] is False


def test_deliberation_council_correlated_consensus_detection():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2)
    # Both proposals provide identical summary/assumptions
    primary = {"title": "Identical Plan", "rationale": "Exact same rationale"}
    alternate = (
        ("member_2", {"title": "Identical Plan", "rationale": "Exact same rationale"}),
    )
    result = council.evaluate_decision(
        decision_type="DESIGN_DIRECTION",
        prompt="Design direction choice",
        primary_proposal=primary,
        alternate_proposals=alternate,
    )
    assert result.decision_record["correlated_consensus_risk"] is True
    assert council.correlated_consensus_risk_count == 1


def test_deliberation_council_live_eligibility_gate():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY")
    eligible, reason = council.check_live_eligibility()
    assert not eligible
    assert "target" in reason

    council.real_shadow_sample_count = 25
    eligible, reason = council.check_live_eligibility()
    assert eligible
    assert reason == "ELIGIBLE_FOR_SELECTIVE_LIVE_REVIEW"
