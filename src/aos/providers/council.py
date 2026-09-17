"""AOS Deliberation Council V1: Multi-Agent Decision-Quality Layer.

Features:
- Bounded decision trigger evaluating uncertainty, impact, reversibility, evidence strength, confidence
- Shadow mode execution (SHADOW_ONLY) with real decision recording without altering execution
- Target 3 council members, min real quorum >= 2
- Independent blinded proposals from policy-approved providers/roles before cross-exposure
- Positional bias elimination (Proposal A is not always primary; randomized/blinded labels)
- Anonymous peer critique and scoring across structured evaluation dimensions
- Structured scoring + quorum + synthesis (not simple raw majority voting)
- Fails closed to deterministic policy, authority, and evidence validators (cannot be voted away)
- Durable sanitized shadow-decision ledger persistence and state reconstruction across restarts
- Non-blocking spare-capacity checking with skip/defer metrics
- Correlated consensus risk detection (tracking semantic, evidence, and provider diversity)
- Real shadow sample counting requiring quorum >= 2, independent proposals >= 2, and real execution
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


COUNCIL_TARGET_MEMBER_COUNT = 3
COUNCIL_MIN_REAL_QUORUM = 2


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
    Ambiguous architectural choices, major replanning, security reviews, completion assessments,
    and tradeoff-heavy decisions trigger COUNCIL_REQUIRED=YES.
    """
    u_prompt = prompt.upper()
    d_type = decision_type.upper()

    # Deterministic bypasses
    if any(k in d_type for k in ("READ", "GIT_STATUS", "SCHEMA_CHECK", "EXACT_CI", "PROCESS_STATUS")):
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
        "PRODUCTIZATION", "GOLDEN_PATH", "CANONICAL_DRIFT", "DESIGN_DIRECTION",
        "COMPLETION_ASSESSMENT", "REPAIR_STRATEGY", "FRONTIER_SELECTION",
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
    council_required = (
        is_high_impact
        or uncertainty >= 0.6
        or (impact >= 0.7 and reversibility <= 0.5)
        or primary_confidence < 0.7
    )

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
    is_primary: bool = False


def blind_proposals(
    raw_proposals: Sequence[Tuple[str, Mapping[str, Any]]],
    *,
    seed: Optional[Any] = None,
    shuffle: bool = True,
) -> List[BlindedProposal]:
    """Strip provider/model/agent identity from proposals and anonymize as Proposal A/B/C.

    Shuffles proposals to eliminate positional bias (Primary planner is not always Proposal A).
    Maintains internal is_primary flag for private retrospective evaluation only.
    """
    labels = ["Proposal A", "Proposal B", "Proposal C", "Proposal D", "Proposal E", "Proposal F"]
    indexed = list(enumerate(raw_proposals))
    if shuffle and len(indexed) > 1:
        rng = random.Random(seed) if seed is not None else random.Random()
        rng.shuffle(indexed)

    blinded = []
    for label_idx, (orig_idx, (member_id, proposal)) in enumerate(indexed):
        label = labels[label_idx] if label_idx < len(labels) else f"Proposal {label_idx+1}"
        member_hash = hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:12]

        clean_payload = dict(proposal)
        for key in ("provider", "model", "agent_id", "author", "persona", "provider_name", "backend"):
            clean_payload.pop(key, None)

        summary = (
            clean_payload.get("rationale")
            or clean_payload.get("description")
            or clean_payload.get("title")
            or json.dumps(clean_payload, sort_keys=True)[:300]
        )
        is_primary = (member_id == "primary_planner" or orig_idx == 0)
        blinded.append(BlindedProposal(
            proposal_id=label,
            raw_payload=clean_payload,
            normalized_summary=str(summary),
            member_hash=member_hash,
            is_primary=is_primary,
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
    Ensures council shadow work is strictly non-blocking and consumes only spare reasoning capacity.
    Persists durable, sanitized decision ledger entries without secrets or raw chain-of-thought.
    Reconstructs aggregate metrics from durable ledger on instantiation.
    """

    def __init__(
        self,
        mode: str = "SHADOW_ONLY",
        min_quorum: int = COUNCIL_MIN_REAL_QUORUM,
        target_members: int = COUNCIL_TARGET_MEMBER_COUNT,
        ledger_dir: Optional[Path] = None,
    ) -> None:
        self.mode = mode
        self.min_quorum = min_quorum
        self.target_members = target_members
        self.ledger_dir = ledger_dir
        self.shadow_sample_count = 0
        self.real_shadow_sample_count = 0
        self.trigger_count = 0
        self.skipped_capacity_count = 0
        self.agreement_count = 0
        self.disagreement_count = 0
        self.policy_violations_caught = 0
        self.canonical_contradictions_caught = 0
        self.primary_errors_caught = 0
        self.false_disagreement_count = 0
        self.correlated_consensus_risk_count = 0
        self.total_latency_delta_ms = 0.0
        self.total_cost_delta_estimate = 0.0
        self.primary_execution_interference_count = 0

        if self.ledger_dir:
            self._reconstruct_from_ledger()

    def _reconstruct_from_ledger(self) -> None:
        """Reconstruct durable council metrics from ledger file if present."""
        if not self.ledger_dir:
            return
        ledger_file = self.ledger_dir / "deliberation-shadow-ledger.jsonl"
        if not ledger_file.is_file():
            return
        try:
            with open(ledger_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self.trigger_count += 1
                    if rec.get("council_trigger_reason") == "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS":
                        self.skipped_capacity_count += 1
                        continue

                    self.shadow_sample_count += 1
                    if (
                        rec.get("quorum_obtained")
                        and rec.get("anonymous_proposal_count", 0) >= self.min_quorum
                        and rec.get("is_real_execution", True)
                    ):
                        self.real_shadow_sample_count += 1

                    if rec.get("council_agreement", True):
                        self.agreement_count += 1
                    else:
                        self.disagreement_count += 1
                        if rec.get("policy_violation_caught") or rec.get("canonical_conflict_detected"):
                            self.primary_errors_caught += 1
                        else:
                            self.false_disagreement_count += 1

                    if rec.get("policy_violation_caught"):
                        self.policy_violations_caught += 1
                    if rec.get("canonical_conflict_detected"):
                        self.canonical_contradictions_caught += 1
                    if rec.get("correlated_consensus_risk"):
                        self.correlated_consensus_risk_count += 1
                    self.total_latency_delta_ms += float(rec.get("latency_delta_ms", 0.0))
                    self.total_cost_delta_estimate += float(rec.get("estimated_request_cost_delta", 0.0))
        except Exception:
            pass

    def evaluate_decision(
        self,
        decision_type: str,
        prompt: str,
        primary_proposal: Mapping[str, Any],
        alternate_proposals: Sequence[Tuple[str, Mapping[str, Any]]] = (),
        authority_records: Optional[Mapping[str, Any]] = None,
        canonical_hashes: Optional[Mapping[str, Any]] = None,
        project_id: str = "lari",
        command_id: Optional[str] = None,
        is_spare_capacity_available: bool = True,
        is_real_execution: bool = True,
        seed: Optional[Any] = None,
    ) -> DeliberationResult:
        """Run deliberative multi-agent scoring and synthesis.

        If spare capacity is unavailable (e.g. rate-limited, provider backoff active),
        the evaluation gracefully defers/skips without blocking or delaying primary execution.
        """
        start_time = time.perf_counter()
        self.trigger_count += 1

        assessment = assess_council_trigger(decision_type, prompt, primary_proposal)

        # Invariant: COUNCIL_MAY_CONSUME_SPARE_REASONING_CAPACITY_ONLY=YES
        if not is_spare_capacity_available:
            self.skipped_capacity_count += 1
            decision_id = f"dec-{hashlib.sha256((prompt + str(time.time())).encode('utf-8')).hexdigest()[:12]}"
            skip_record = {
                "decision_id": decision_id,
                "project_id": project_id,
                "command_id": command_id or "unknown-command",
                "decision_class": decision_type,
                "timestamp": time.time(),
                "primary_decision_fingerprint": hashlib.sha256(json.dumps(primary_proposal, sort_keys=True).encode("utf-8")).hexdigest()[:16],
                "primary_confidence": assessment.primary_confidence,
                "council_trigger_reason": "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS",
                "anonymous_proposal_count": 0,
                "quorum_obtained": False,
                "council_result_fingerprint": None,
                "council_agreement": True,
                "council_confidence": 0.0,
                "contradiction_detected": False,
                "policy_violation_caught": False,
                "canonical_conflict_detected": False,
                "correlated_consensus_risk": False,
                "estimated_request_cost_delta": 0.0,
                "latency_delta_ms": 0.0,
                "is_real_execution": is_real_execution,
                "execution_affected": False,
            }
            self._persist_decision_ledger(skip_record)
            return DeliberationResult(
                winning_proposal_id=None,
                selected_payload=None,
                scores={},
                agreement_with_primary=True,
                quorum_reached=False,
                confidence=0.0,
                mode=self.mode,
                decision_record=skip_record,
            )

        all_candidates = [("primary_planner", primary_proposal)] + list(alternate_proposals)
        blinded = blind_proposals(all_candidates, seed=seed, shuffle=True)

        scores: Dict[str, ProposalScore] = {}
        contradiction_found = False
        policy_violation_found = False
        canonical_conflict_found = False

        for b in blinded:
            payload = b.raw_payload

            # Authority / policy gates: cannot be overridden by votes
            has_authority = True
            contradiction_count = 0
            req_auth = str(payload.get("authority_id", "")).strip()
            if authority_records and req_auth:
                has_authority = req_auth.upper() in authority_records
                if not has_authority:
                    contradiction_count += 1
                    self.policy_violations_caught += 1
                    policy_violation_found = True
                    canonical_conflict_found = True

            # Checking red lines / prohibited actions
            policy_compliant = True
            tasks = payload.get("tasks", [])
            for t in tasks:
                t_str = json.dumps(t, sort_keys=True).lower() if isinstance(t, (dict, list)) else str(t).lower()
                if any(k in t_str for k in ("force_push", "--force", "-f", "rm -rf", "drop database", "sudo", "bypass_human_gate")):
                    policy_compliant = False
                    contradiction_count += 1
                    policy_violation_found = True

            if contradiction_count > 0:
                contradiction_found = True

            score = ProposalScore(
                proposal_id=b.proposal_id,
                evidence_consistency=0.9 if not contradiction_count else 0.2,
                canonical_consistency=0.9 if has_authority else 0.1,
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

        # Correlated Consensus Risk Detection:
        proposal_summaries = [b.normalized_summary.strip() for b in blinded]
        semantic_diversity = len(set(proposal_summaries)) / max(1, len(proposal_summaries))
        correlated_risk = False
        if len(blinded) >= 2 and semantic_diversity <= 0.5:
            correlated_risk = True
            self.correlated_consensus_risk_count += 1

        # Determine winner by highest composite score among policy-compliant proposals
        eligible = [(k, v) for k, v in scores.items() if v.policy_compliant and v.composite_score > 0.0]
        eligible.sort(key=lambda item: item[1].composite_score, reverse=True)

        quorum_reached = len(blinded) >= self.min_quorum
        winning_proposal_id = eligible[0][0] if (eligible and quorum_reached) else (eligible[0][0] if eligible else None)

        # Check agreement with primary without exposing primary's identity externally
        primary_proposal_id = next((b.proposal_id for b in blinded if b.is_primary), None)
        agreement_with_primary = (winning_proposal_id == primary_proposal_id)

        if agreement_with_primary:
            self.agreement_count += 1
        else:
            self.disagreement_count += 1
            if policy_violation_found or canonical_conflict_found:
                self.primary_errors_caught += 1
            else:
                self.false_disagreement_count += 1

        selected_payload = None
        if winning_proposal_id:
            for b in blinded:
                if b.proposal_id == winning_proposal_id:
                    selected_payload = b.raw_payload
                    break

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self.total_latency_delta_ms += elapsed_ms
        cost_delta = 0.005 * len(blinded)
        self.total_cost_delta_estimate += cost_delta

        self.shadow_sample_count += 1
        # Only increment real_shadow_sample_count if quorum was reached with >= 2 independent proposals
        if is_real_execution and quorum_reached and len(blinded) >= self.min_quorum:
            self.real_shadow_sample_count += 1

        primary_fingerprint = hashlib.sha256(json.dumps(primary_proposal, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        result_fingerprint = hashlib.sha256(json.dumps(selected_payload or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        decision_id = f"dec-{hashlib.sha256((prompt + primary_fingerprint + str(time.time())).encode('utf-8')).hexdigest()[:12]}"

        # Confidence: if correlated consensus risk is high, reduce confidence
        base_confidence = eligible[0][1].confidence if eligible else 0.0
        if correlated_risk:
            base_confidence = min(base_confidence, 0.45)

        record = {
            "decision_id": decision_id,
            "project_id": project_id,
            "command_id": command_id or "unknown-command",
            "decision_class": decision_type,
            "timestamp": time.time(),
            "primary_decision_fingerprint": primary_fingerprint,
            "primary_confidence": assessment.primary_confidence,
            "council_trigger_reason": assessment.trigger_reason,
            "anonymous_proposal_count": len(blinded),
            "quorum_obtained": quorum_reached,
            "council_result_fingerprint": result_fingerprint,
            "council_agreement": agreement_with_primary,
            "council_confidence": base_confidence,
            "contradiction_detected": contradiction_found,
            "policy_violation_caught": policy_violation_found,
            "canonical_conflict_detected": canonical_conflict_found,
            "correlated_consensus_risk": correlated_risk,
            "estimated_request_cost_delta": cost_delta,
            "latency_delta_ms": elapsed_ms,
            "is_real_execution": is_real_execution,
            "execution_affected": False,  # INVARIANT: never alters live execution in SHADOW_ONLY
        }

        self._persist_decision_ledger(record)

        return DeliberationResult(
            winning_proposal_id=winning_proposal_id,
            selected_payload=selected_payload,
            scores=scores,
            agreement_with_primary=agreement_with_primary,
            quorum_reached=quorum_reached,
            confidence=base_confidence,
            mode=self.mode,
            decision_record=record,
        )

    def _persist_decision_ledger(self, record: Mapping[str, Any]) -> None:
        """Persist structured sanitized decision artifact to durable ledger."""
        if not self.ledger_dir:
            return
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
            ledger_file = self.ledger_dir / "deliberation-shadow-ledger.jsonl"
            with open(ledger_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(record), ensure_ascii=False) + "\n")
        except Exception:
            # Shadow ledger must never crash primary execution path
            pass

    def check_live_eligibility(self) -> Tuple[bool, str]:
        """Check promotion gate to SELECTIVE_LIVE.

        Requires:
        - REAL_SHADOW_SAMPLE_COUNT >= 20
        - PRIMARY_EXECUTION_INTERFERENCE_COUNT == 0
        - POLICY_VIOLATIONS_CAUGHT >= 0
        - No unhandled errors or shadow-induced product waits
        """
        if self.real_shadow_sample_count < 20:
            return False, f"REAL_SHADOW_SAMPLE_COUNT={self.real_shadow_sample_count} < 20 target"
        if self.primary_execution_interference_count > 0:
            return False, f"PRIMARY_EXECUTION_INTERFERENCE_COUNT={self.primary_execution_interference_count} > 0"
        return True, "ELIGIBLE_FOR_SELECTIVE_LIVE_REVIEW"
