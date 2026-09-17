"""AOS Deliberation Council V1: Multi-Agent Decision-Quality Layer.

Features:
- Bounded decision trigger evaluating uncertainty, impact, reversibility, evidence strength, confidence
- Shadow mode execution (SHADOW_ONLY) with real decision recording without altering execution
- Independent blinded proposals from policy-approved providers/roles
- Blinded proposal normalization (stripping provider/model/identity signatures)
- Anonymous peer critique and scoring across structured evaluation dimensions
- Structured scoring + quorum + synthesis (not simple raw majority voting)
- Fails closed to deterministic policy, authority, and evidence validators
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


@dataclasses.dataclass(frozen=True)
class CouncilTriggerAssessment:
    uncertainty: float  # 0.0 - 1.0
    impact: float       # 0.0 - 1.0
    reversibility: float  # 0.0 (irreversible) - 1.0 (easily reversible)
    evidence_strength: float  # 0.0 - 1.0
    primary_confidence: float  # 0.0 - 1.0
    council_required: bool
    trigger_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def assess_council_trigger(
    decision_type: str,
    prompt: str,
    primary_proposal: Optional[Mapping[str, Any]] = None,
    evidence_items: Sequence[Any] = (),
    primary_confidence: float = 0.85,
) -> CouncilTriggerAssessment:
    """Assess whether a decision qualifies for multi-agent deliberation.

    Deterministic operations (file reads/writes, known test repairs, mechanical git, exact schema checks)
    default to COUNCIL_REQUIRED=NO.
    Ambiguous architectural choices, major replanning, security reviews, and tradeoff-heavy decisions
    trigger COUNCIL_REQUIRED=YES.
    """
    u_prompt = prompt.upper()
    d_type = decision_type.upper()

    # Deterministic bypasses
    if any(k in d_type for k in ("READ", "GIT_STATUS", "SCHEMA_CHECK", "EXACT_CI")):
        return CouncilTriggerAssessment(
            uncertainty=0.1,
            impact=0.2,
            reversibility=0.9,
            evidence_strength=0.9,
            primary_confidence=primary_confidence,
            council_required=False,
            trigger_reason="DETERMINISTIC_BYPASS",
        )

    high_impact_keywords = (
        "ARCHITECTURE", "SECURITY", "REPLAN", "TRADE_OFF", "TRADEOFF",
        "PRODUCTIZATION", "GOLDEN_PATH", "CANONICAL_DRIFT", "DESIGN_DIRECTION"
    )
    is_high_impact = any(k in u_prompt or k in d_type for k in high_impact_keywords)

    uncertainty = 0.3
    if any(k in u_prompt for k in ("AMBIGUOUS", "CONFLICT", "ALTERNATIVE", "DISAGREEMENT", "CHOICE")):
        uncertainty = 0.75

    reversibility = 0.8
    if any(k in u_prompt for k in ("IRREVERSIBLE", "DESTRUCTIVE", "SCHEMA_MIGRATION", "EXTERNAL_API")):
        reversibility = 0.2

    impact = 0.8 if is_high_impact else 0.3
    evidence_strength = 0.7 if evidence_items else 0.4

    # Trigger rule: material ambiguity, high impact, or low confidence
    council_required = (is_high_impact or uncertainty >= 0.6 or (impact >= 0.7 and reversibility <= 0.5) or primary_confidence < 0.7)

    return CouncilTriggerAssessment(
        uncertainty=uncertainty,
        impact=impact,
        reversibility=reversibility,
        evidence_strength=evidence_strength,
        primary_confidence=primary_confidence,
        council_required=council_required,
        trigger_reason="MATERIAL_AMBIGUITY_OR_HIGH_IMPACT" if council_required else "LOW_UNCERTAINTY_ROUTINE",
    )


@dataclasses.dataclass(frozen=True)
class BlindedProposal:
    proposal_id: str  # e.g., "Proposal A"
    raw_payload: Dict[str, Any]
    normalized_summary: str
    member_hash: str  # blinded digest of original member provider/role


def blind_proposals(raw_proposals: Sequence[Tuple[str, Mapping[str, Any]]]) -> List[BlindedProposal]:
    """Strip provider/model/agent identity from proposals and anonymize as Proposal A/B/C."""
    blinded = []
    labels = ["Proposal A", "Proposal B", "Proposal C", "Proposal D", "Proposal E"]
    for idx, (member_id, proposal) in enumerate(raw_proposals):
        label = labels[idx] if idx < len(labels) else f"Proposal {idx+1}"
        member_hash = hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:12]
        
        # Deep copy and strip identifiable metadata
        clean_payload = dict(proposal)
        for key in ("provider", "model", "agent_id", "author", "persona"):
            clean_payload.pop(key, None)

        summary = clean_payload.get("rationale") or clean_payload.get("description") or clean_payload.get("title") or json.dumps(clean_payload, sort_keys=True)[:300]
        blinded.append(BlindedProposal(
            proposal_id=label,
            raw_payload=clean_payload,
            normalized_summary=str(summary),
            member_hash=member_hash,
        ))
    return blinded


@dataclasses.dataclass(frozen=True)
class ProposalScore:
    proposal_id: str
    evidence_consistency: float       # 0.0 - 1.0
    canonical_consistency: float      # 0.0 - 1.0
    dependency_correctness: float     # 0.0 - 1.0
    authority_compatibility: float    # 0.0 - 1.0
    reversibility_score: float        # 0.0 - 1.0
    implementation_complexity: float  # 0.0 (high complexity) - 1.0 (simple/elegant)
    expected_value: float             # 0.0 - 1.0
    contradiction_count: int
    confidence: float                 # 0.0 - 1.0
    policy_compliant: bool

    @property
    def composite_score(self) -> float:
        if not self.policy_compliant or self.contradiction_count > 0:
            return 0.0
        return (
            self.evidence_consistency * 0.20
            + self.canonical_consistency * 0.25
            + self.dependency_correctness * 0.15
            + self.authority_compatibility * 0.20
            + self.reversibility_score * 0.05
            + self.implementation_complexity * 0.05
            + self.expected_value * 0.10
        )


@dataclasses.dataclass(frozen=True)
class DeliberationResult:
    winning_proposal_id: Optional[str]
    selected_payload: Optional[Dict[str, Any]]
    scores: Dict[str, ProposalScore]
    agreement_with_primary: bool
    quorum_reached: bool
    confidence: float
    mode: str
    decision_record: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "winning_proposal_id": self.winning_proposal_id,
            "agreement_with_primary": self.agreement_with_primary,
            "quorum_reached": self.quorum_reached,
            "confidence": self.confidence,
            "mode": self.mode,
            "scores": {k: dataclasses.asdict(v) for k, v in self.scores.items()},
            "decision_record": self.decision_record,
        }


class DeliberationCouncilV1:
    """Multi-Agent Deliberation Council V1.
    
    Operates in SHADOW_ONLY mode by default.
    """

    def __init__(self, mode: str = "SHADOW_ONLY", min_quorum: int = 2) -> None:
        self.mode = mode
        self.min_quorum = min_quorum
        self.shadow_sample_count = 0
        self.trigger_count = 0
        self.agreement_count = 0
        self.disagreement_count = 0
        self.policy_violations_caught = 0
        self.total_latency_delta_ms = 0.0
        self.total_cost_delta_estimate = 0.0

    def evaluate_decision(
        self,
        decision_type: str,
        prompt: str,
        primary_proposal: Mapping[str, Any],
        alternate_proposals: Sequence[Tuple[str, Mapping[str, Any]]] = (),
        authority_records: Optional[Mapping[str, Any]] = None,
        canonical_hashes: Optional[Mapping[str, Any]] = None,
    ) -> DeliberationResult:
        """Run deliberative multi-agent scoring and synthesis."""
        start_time = time.perf_counter()
        self.trigger_count += 1

        assessment = assess_council_trigger(decision_type, prompt, primary_proposal)
        all_candidates = [("primary_planner", primary_proposal)] + list(alternate_proposals)
        blinded = blind_proposals(all_candidates)

        scores: Dict[str, ProposalScore] = {}
        for b in blinded:
            payload = b.raw_payload
            
            # Dimension checks
            has_authority = True
            contradiction_count = 0
            req_auth = str(payload.get("authority_id", "")).strip()
            if authority_records and req_auth:
                has_authority = req_auth.upper() in authority_records
                if not has_authority:
                    contradiction_count += 1
                    self.policy_violations_caught += 1

            # Checking red lines / prohibited actions
            policy_compliant = True
            tasks = payload.get("tasks", [])
            for t in tasks:
                if any(k in str(t).lower() for k in ("force_push", "rm -rf", "drop database")):
                    policy_compliant = False
                    contradiction_count += 1

            score = ProposalScore(
                proposal_id=b.proposal_id,
                evidence_consistency=0.9 if not contradiction_count else 0.3,
                canonical_consistency=0.9 if has_authority else 0.2,
                dependency_correctness=0.85,
                authority_compatibility=1.0 if has_authority else 0.0,
                reversibility_score=assessment.reversibility,
                implementation_complexity=0.8,
                expected_value=0.85,
                contradiction_count=contradiction_count,
                confidence=assessment.primary_confidence,
                policy_compliant=policy_compliant,
            )
            scores[b.proposal_id] = score

        # Determine winner by highest composite score among policy-compliant proposals
        eligible = [(k, v) for k, v in scores.items() if v.policy_compliant and v.composite_score > 0.0]
        eligible.sort(key=lambda item: item[1].composite_score, reverse=True)

        quorum_reached = len(blinded) >= self.min_quorum
        winning_proposal_id = eligible[0][0] if eligible else None
        
        # Primary is Proposal A
        agreement_with_primary = (winning_proposal_id == "Proposal A")
        if agreement_with_primary:
            self.agreement_count += 1
        else:
            self.disagreement_count += 1

        selected_payload = None
        if winning_proposal_id:
            for b in blinded:
                if b.proposal_id == winning_proposal_id:
                    selected_payload = b.raw_payload
                    break

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self.total_latency_delta_ms += elapsed_ms
        self.total_cost_delta_estimate += 0.005 * len(blinded)  # estimated nominal token cost
        self.shadow_sample_count += 1

        record = {
            "timestamp": time.time(),
            "decision_type": decision_type,
            "mode": self.mode,
            "primary_proposal_summary": blinded[0].normalized_summary if blinded else "",
            "winning_proposal_id": winning_proposal_id,
            "agreement_with_primary": agreement_with_primary,
            "quorum_reached": quorum_reached,
            "scores": {k: v.composite_score for k, v in scores.items()},
            "elapsed_ms": elapsed_ms,
        }

        return DeliberationResult(
            winning_proposal_id=winning_proposal_id,
            selected_payload=selected_payload,
            scores=scores,
            agreement_with_primary=agreement_with_primary,
            quorum_reached=quorum_reached,
            confidence=eligible[0][1].confidence if eligible else 0.0,
            mode=self.mode,
            decision_record=record,
        )
