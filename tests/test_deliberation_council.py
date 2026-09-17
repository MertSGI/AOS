"""Tests for AOS Deliberation Council V1."""

import pytest
from aos.providers.council import (
    DeliberationCouncilV1,
    assess_council_trigger,
    blind_proposals,
)


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


def json_str(data):
    import json
    return json.dumps(data)


def test_deliberation_council_shadow_evaluation():
    council = DeliberationCouncilV1(mode="SHADOW_ONLY", min_quorum=2)
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
    )
    assert result.mode == "SHADOW_ONLY"
    assert result.quorum_reached
    assert result.winning_proposal_id in ("Proposal A", "Proposal B")
    assert council.shadow_sample_count == 1
    assert council.trigger_count == 1


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
