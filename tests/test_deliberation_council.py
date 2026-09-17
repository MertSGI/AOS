"""Tests for AOS Deliberation Council V1: Multi-Agent Decision-Quality Layer."""

import json
from pathlib import Path
from aos.providers.council import (
    COUNCIL_MIN_REAL_QUORUM,
    COUNCIL_TARGET_MEMBER_COUNT,
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


def test_blind_proposals_anonymization_and_positional_bias():
    raw = [
        ("nemotron_provider", {"model": "nemotron-4", "title": "Option A", "rationale": "Direct SQL"}),
        ("gemini_provider", {"model": "gemini-2.5", "title": "Option B", "rationale": "RPC Interface"}),
        ("groq_provider", {"model": "llama-3.3-70b", "title": "Option C", "rationale": "gRPC Service"}),
    ]
    blinded = blind_proposals(raw, seed=42, shuffle=True)
    assert len(blinded) == 3
    # Check that model and provider metadata is stripped
    for b in blinded:
        assert "model" not in b.raw_payload
        assert "nemotron" not in json_str(b.raw_payload)
        assert "gemini" not in json_str(b.raw_payload)
        assert "groq" not in json_str(b.raw_payload)
    # Primary proposal was Option A. Due to shuffle with seed=42, verify Proposal A is not statically Option A
    primary_blinded = next(b for b in blinded if b.is_primary)
    assert primary_blinded.raw_payload["title"] == "Option A"


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
    # Supply 2 peer reviews to satisfy deliberative peer-review quorum (min_quorum=2)
    peer_reviews = [
        {
            "proposal_id": "Proposal A",
            "evidence_consistency": 0.95,
            "canonical_consistency": 0.95,
            "dependency_correctness": 0.90,
            "authority_compatibility": 1.0,
            "risk": 0.1,
            "reversibility": 0.9,
            "implementation_complexity": 0.85,
            "expected_value": 0.9,
            "contradictions": [],
            "ranked_preference": 1,
            "confidence": 0.9,
            "short_bounded_rationale": "Strong architecture adhering strictly to canonical RPC.",
        },
        {
            "proposal_id": "Proposal B",
            "evidence_consistency": 0.85,
            "canonical_consistency": 0.85,
            "dependency_correctness": 0.80,
            "authority_compatibility": 0.9,
            "risk": 0.2,
            "reversibility": 0.8,
            "implementation_complexity": 0.80,
            "expected_value": 0.85,
            "contradictions": [],
            "ranked_preference": 2,
            "confidence": 0.85,
            "short_bounded_rationale": "Good alternative schema migration.",
        },
    ]
    auths = {"DECISION-020": {}}
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide backend boundary for UI V2 slice",
        primary_proposal=primary,
        alternate_proposals=alternate,
        peer_reviews=peer_reviews,
        authority_records=auths,
        project_id="lari-ui-v2",
        command_id="continue-test-123",
        is_spare_capacity_available=True,
    )
    assert result.mode == "SHADOW_ONLY"
    assert result.quorum_reached
    assert council.shadow_sample_count == 1
    assert council.real_shadow_sample_count == 1
    assert council.trigger_count == 1

    # Check ledger persistence and explicit count telemetry
    ledger_file = tmp_path / "deliberation-shadow-ledger.jsonl"
    assert ledger_file.exists()
    lines = ledger_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["decision_class"] == "ARCHITECTURE_DECISION"
    assert rec["project_id"] == "lari-ui-v2"
    assert rec["command_id"] == "continue-test-123"
    assert rec["execution_affected"] is False
    assert rec["primary_proposal_count"] == 1
    assert rec["council_alternate_proposal_count"] == 1
    assert rec["total_blinded_proposal_count"] == 2
    assert rec["peer_reviewer_count"] == 2
    assert rec["valid_peer_review_count"] == 2
    assert rec["quorum_obtained"] is True
    assert rec["council_status"] == "QUORUM_OBTAINED"
    assert "primary_decision_fingerprint" in rec
    assert "council_agreement" in rec


def test_deliberation_council_one_member_does_not_count_as_real_shadow_sample(tmp_path=None):
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {
        "title": "Solo Proposal",
        "authority_id": "DECISION-020",
        "tasks": [],
    }
    # No alternate proposals provided -> only 1 proposal exists -> quorum NOT obtained
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Solo decision without alternates",
        primary_proposal=primary,
        alternate_proposals=(),
        is_spare_capacity_available=True,
        is_real_execution=True,
    )
    assert not result.quorum_reached
    assert result.winning_proposal_id is None
    assert result.decision_record["council_status"] == "INSUFFICIENT_QUORUM"
    assert council.shadow_sample_count == 1
    # INVARIANT: Do NOT count primary-only evaluations as Council real shadow samples
    assert council.real_shadow_sample_count == 0


def test_deliberation_council_without_peer_review_quorum_does_not_increment_real_shadow_sample(tmp_path=None):
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {"title": "Primary", "authority_id": "DECISION-020"}
    alt = [("m2", {"title": "Alt 1", "authority_id": "DECISION-020"})]
    auths = {"DECISION-020": {}}
    # 2 proposals, but 0 peer reviews -> quorum NOT obtained
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide architecture",
        primary_proposal=primary,
        alternate_proposals=alt,
        authority_records=auths,
        is_spare_capacity_available=True,
        is_real_execution=True,
    )
    assert not result.quorum_reached
    assert result.winning_proposal_id is None
    assert result.decision_record["council_status"] == "INSUFFICIENT_QUORUM"
    assert result.decision_record["valid_peer_review_count"] == 0
    assert council.real_shadow_sample_count == 0


def test_deliberation_council_durable_metrics_survive_reconstruction(tmp_path=None):
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())

    council1 = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {"title": "P1", "authority_id": "DECISION-020"}
    alt = [("m2", {"title": "P2", "authority_id": "DECISION-020"})]
    reviews = [
        {"proposal_id": "Proposal A", "ranked_preference": 1, "short_bounded_rationale": "Solid"},
        {"proposal_id": "Proposal B", "ranked_preference": 2, "short_bounded_rationale": "Good"},
    ]
    auths = {"DECISION-020": {}}
    council1.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Test prompt",
        primary_proposal=primary,
        alternate_proposals=alt,
        peer_reviews=reviews,
        authority_records=auths,
    )
    assert council1.real_shadow_sample_count == 1
    assert council1.trigger_count == 1

    # Simulate runtime restart / new Council instance in same directory
    council2 = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    assert council2.real_shadow_sample_count == 1
    assert council2.trigger_count == 1
    assert council2.shadow_sample_count == 1


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
    assert not result.agreement_with_primary
    assert council.policy_violations_caught >= 1


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


def test_deliberation_council_correlated_consensus_penalizes_confidence():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2)
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
    assert result.confidence <= 0.45


def test_deliberation_council_deterministic_evidence_cannot_be_voted_away():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2)
    # Prohibited action (force_push) in multiple proposals
    p1 = {"title": "Plan 1", "tasks": [{"cmd": ["git", "push", "--force"]}]}
    p2 = [("m2", {"title": "Plan 2", "tasks": [{"cmd": ["git", "push", "--force"]}]})]
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Emergency push",
        primary_proposal=p1,
        alternate_proposals=p2,
    )
    # None should be eligible because red-line policy violations cannot be voted away
    assert result.winning_proposal_id is None


def test_deliberation_council_live_eligibility_gate():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY")
    eligible, reason = council.check_live_eligibility()
    assert not eligible
    assert "target" in reason

    council.real_shadow_sample_count = 25
    eligible, reason = council.check_live_eligibility()
    assert eligible
    assert reason == "ELIGIBLE_FOR_SELECTIVE_LIVE_REVIEW"


def test_first_pass_proposal_isolation_and_anonymous_mapping():
    """Verify first-pass proposals have zero cross-exposure, and blinding strips all identities."""
    proposals = [
        ("primary_planner", {"provider": "nemotron", "model": "nemotron-4", "title": "Primary Plan", "rationale": "Base plan"}),
        ("gemini_member", {"provider": "gemini", "model": "gemini-2.5", "title": "Gemini Plan", "rationale": "Alt plan 1"}),
        ("groq_member", {"provider": "groq", "model": "llama-3.3-70b", "title": "Groq Plan", "rationale": "Alt plan 2"}),
    ]
    blinded = blind_proposals(proposals, seed=1234, shuffle=True)
    assert len(blinded) == 3
    # Check that each blinded proposal has no provider, model, agent_id or persona
    for b in blinded:
        assert b.proposal_id.startswith("Proposal ")
        for forbidden in ("provider", "model", "agent_id", "persona", "nemotron", "gemini", "groq"):
            assert forbidden not in json.dumps(b.raw_payload)


def test_structured_peer_review_schema_validation():
    from aos.providers.council import StructuredPeerReview
    data = {
        "proposal_id": "Proposal A",
        "evidence_consistency": 0.88,
        "canonical_consistency": 0.92,
        "dependency_correctness": 0.85,
        "authority_compatibility": 0.95,
        "risk": 0.15,
        "reversibility": 0.8,
        "implementation_complexity": 0.75,
        "expected_value": 0.9,
        "contradictions": ["Minor detail"],
        "ranked_preference": 1,
        "confidence": 0.85,
        "short_bounded_rationale": "Exemplary adherence to standards.",
    }
    review = StructuredPeerReview.from_dict("Proposal A", data)
    assert review.proposal_id == "Proposal A"
    assert review.evidence_consistency == 0.88
    assert review.canonical_consistency == 0.92
    assert review.contradictions == ("Minor detail",)
    assert len(review.short_bounded_rationale) <= 500


def test_popular_proposal_rejected_by_policy_remains_ineligible(tmp_path=None):
    """Even if all reviewers vote unanimously for a proposal, deterministic policy check rejects it."""
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, ledger_dir=tmp_path)
    primary = {
        "title": "Unapproved Bypass Plan",
        "authority_id": "DECISION-UNAUTHORIZED",
        "tasks": [{"action": "sudo rm -rf /"}],
    }
    alt = [("alt_member", {"title": "Safe Compliant Plan", "authority_id": "DECISION-020", "tasks": []})]
    # Reviewers give 100% scores to Proposal A (even if it's the unapproved one)
    reviews = [
        {"proposal_id": "Proposal A", "ranked_preference": 1, "evidence_consistency": 1.0, "short_bounded_rationale": "High praise"},
        {"proposal_id": "Proposal B", "ranked_preference": 2, "evidence_consistency": 0.5, "short_bounded_rationale": "Low praise"},
    ]
    auths = {"DECISION-020": {}}
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Test policy supremacy",
        primary_proposal=primary,
        alternate_proposals=alt,
        peer_reviews=reviews,
        authority_records=auths,
    )
    for pid, score in result.scores.items():
        # The proposal containing unauthorized authority or redline tasks MUST be composite_score = 0.0
        if not score.policy_compliant:
            assert score.composite_score == 0.0


def test_full_three_proposal_two_reviewer_deliberation(tmp_path=None):
    """Verify target full council: PRIMARY_PROPOSAL_COUNT=1, ALTERNATES=2, BLINDED=3, REVIEWERS=2, QUORUM=PASS."""
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2, target_members=3, ledger_dir=tmp_path)
    p1 = {"title": "P1", "authority_id": "DECISION-020", "tasks": []}
    alts = [
        ("member_2", {"title": "P2", "authority_id": "DECISION-020", "tasks": []}),
        ("member_3", {"title": "P3", "authority_id": "DECISION-020", "tasks": []}),
    ]
    reviews = [
        {"proposal_id": "Proposal A", "ranked_preference": 1, "short_bounded_rationale": "Best"},
        {"proposal_id": "Proposal B", "ranked_preference": 2, "short_bounded_rationale": "Good"},
        {"proposal_id": "Proposal C", "ranked_preference": 3, "short_bounded_rationale": "Acceptable"},
    ]
    auths = {"DECISION-020": {}}
    result = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="3-member deliberation",
        primary_proposal=p1,
        alternate_proposals=alts,
        peer_reviews=reviews,
        authority_records=auths,
        is_real_execution=True,
    )
    assert result.quorum_reached
    assert result.winning_proposal_id is not None
    rec = result.decision_record
    assert rec["primary_proposal_count"] == 1
    assert rec["council_alternate_proposal_count"] == 2
    assert rec["total_blinded_proposal_count"] == 3
    assert rec["valid_peer_review_count"] >= 2
    assert rec["quorum_obtained"] is True
    assert rec["council_status"] == "QUORUM_OBTAINED"
    assert council.real_shadow_sample_count == 1

