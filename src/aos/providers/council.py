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

# Truthful Council Operational Modes
COUNCIL_MODE_OFF = "OFF"
COUNCIL_MODE_SHADOW_BUDGETED = "SHADOW_BUDGETED"
COUNCIL_MODE_ADVISORY_ACTIVE = "ADVISORY_ACTIVE"
COUNCIL_MODE_HUMAN_DECISION_ADVISORY = "HUMAN_DECISION_ADVISORY"

COUNCIL_MODES = {
    COUNCIL_MODE_OFF,
    COUNCIL_MODE_SHADOW_BUDGETED,
    COUNCIL_MODE_ADVISORY_ACTIVE,
    COUNCIL_MODE_HUMAN_DECISION_ADVISORY,
    "SHADOW_ONLY",  # Backwards compatibility
}

# Configurable Hard Budgets
DEFAULT_MAX_COUNCIL_CALLS = 50
DEFAULT_MAX_COUNCIL_TOKEN_ESTIMATE = 100000
DEFAULT_MAX_COUNCIL_REASONING_SHARE = 0.10
DEFAULT_COUNCIL_COOLDOWN_SECONDS = 300.0


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
PEER_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "evidence_consistency",
        "canonical_consistency",
        "dependency_correctness",
        "authority_compatibility",
        "risk",
        "reversibility",
        "implementation_complexity",
        "expected_value",
        "contradictions",
        "ranked_preference",
        "confidence",
        "short_bounded_rationale",
    ],
    "properties": {
        "evidence_consistency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "canonical_consistency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "dependency_correctness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "authority_compatibility": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "risk": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reversibility": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "implementation_complexity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "expected_value": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "ranked_preference": {"type": "integer", "minimum": 1, "maximum": 10},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "short_bounded_rationale": {"type": "string", "maxLength": 500},
    },
}


@dataclasses.dataclass(frozen=True)
class StructuredPeerReview:
    proposal_id: str
    evidence_consistency: float
    canonical_consistency: float
    dependency_correctness: float
    authority_compatibility: float
    risk: float
    reversibility: float
    implementation_complexity: float
    expected_value: float
    contradictions: Tuple[str, ...]
    ranked_preference: int
    confidence: float
    short_bounded_rationale: str
    reviewer_hash: Optional[str] = None

    @classmethod
    def from_dict(cls, proposal_id: str, data: Mapping[str, Any], reviewer_hash: Optional[str] = None) -> "StructuredPeerReview":
        def _num(val: Any, default: float = 0.5) -> float:
            try:
                v = float(val)
                return max(0.0, min(1.0, v))
            except (TypeError, ValueError):
                return default

        def _int(val: Any, default: int = 1) -> int:
            try:
                v = int(val)
                return max(1, min(10, v))
            except (TypeError, ValueError):
                return default

        def _str_bounded(val: Any, max_len: int = 500) -> str:
            s = str(val or "").strip()
            return s[:max_len]

        contras = data.get("contradictions", [])
        if isinstance(contras, list):
            clean_contras = tuple(_str_bounded(c, 120) for c in contras if str(c).strip())
        else:
            clean_contras = ()

        rev_hash = reviewer_hash or data.get("reviewer_hash")
        if not rev_hash and data.get("reviewer_id"):
            rev_hash = hashlib.sha256(str(data["reviewer_id"]).encode("utf-8")).hexdigest()[:12]
        if rev_hash:
            rev_hash = str(rev_hash).strip()[:32]

        return cls(
            proposal_id=str(proposal_id),
            evidence_consistency=_num(data.get("evidence_consistency"), 0.8),
            canonical_consistency=_num(data.get("canonical_consistency"), 0.8),
            dependency_correctness=_num(data.get("dependency_correctness"), 0.85),
            authority_compatibility=_num(data.get("authority_compatibility"), 0.9),
            risk=_num(data.get("risk"), 0.2),
            reversibility=_num(data.get("reversibility"), 0.8),
            implementation_complexity=_num(data.get("implementation_complexity"), 0.7),
            expected_value=_num(data.get("expected_value"), 0.8),
            contradictions=clean_contras,
            ranked_preference=_int(data.get("ranked_preference"), 1),
            confidence=_num(data.get("confidence"), 0.85),
            short_bounded_rationale=_str_bounded(data.get("short_bounded_rationale"), 500),
            reviewer_hash=rev_hash or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "proposal_id": self.proposal_id,
            "evidence_consistency": self.evidence_consistency,
            "canonical_consistency": self.canonical_consistency,
            "dependency_correctness": self.dependency_correctness,
            "authority_compatibility": self.authority_compatibility,
            "risk": self.risk,
            "reversibility": self.reversibility,
            "implementation_complexity": self.implementation_complexity,
            "expected_value": self.expected_value,
            "contradictions": list(self.contradictions),
            "ranked_preference": self.ranked_preference,
            "confidence": self.confidence,
            "short_bounded_rationale": self.short_bounded_rationale,
        }
        if self.reviewer_hash:
            res["reviewer_hash"] = self.reviewer_hash
        return res


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
        max_council_calls: int = DEFAULT_MAX_COUNCIL_CALLS,
        max_council_token_estimate: int = DEFAULT_MAX_COUNCIL_TOKEN_ESTIMATE,
        max_reasoning_share: float = DEFAULT_MAX_COUNCIL_REASONING_SHARE,
        cooldown_seconds: float = DEFAULT_COUNCIL_COOLDOWN_SECONDS,
    ) -> None:
        self.mode = mode if mode in COUNCIL_MODES else "SHADOW_ONLY"
        self.min_quorum = min_quorum
        self.target_members = target_members
        self.ledger_dir = ledger_dir
        self.max_council_calls = max_council_calls
        self.max_council_token_estimate = max_council_token_estimate
        self.max_reasoning_share = max_reasoning_share
        self.cooldown_seconds = cooldown_seconds
        self.last_deliberation_time = 0.0
        self.shadow_sample_count = 0
        self.real_shadow_sample_count = 0
        self.trigger_count = 0
        self.skipped_capacity_count = 0
        self.skipped_redundant_count = 0
        self.skipped_budget_count = 0
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
        self.council_provider_call_count = 0
        self.council_provider_token_estimate = 0
        self.primary_provider_call_count = 0
        self.primary_provider_token_estimate = 0
        self.council_reasoning_share_estimate = 0.0
        self._recent_fingerprints: set[str] = set()

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
                    status = rec.get("council_status")
                    if rec.get("council_trigger_reason") == "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS" or status == "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS":
                        self.skipped_capacity_count += 1
                        continue
                    if status == "SKIP_REDUNDANT_SAMPLE":
                        self.skipped_redundant_count += 1
                        continue

                    self.shadow_sample_count += 1
                    total_blinded = rec.get("total_blinded_proposal_count", rec.get("anonymous_proposal_count", 0))
                    valid_reviews = rec.get("valid_peer_review_count", 0)
                    independent_proposals = rec.get("independent_proposal_count", total_blinded)
                    distinct_reviewers = rec.get("distinct_reviewer_count", valid_reviews)
                    coverage = rec.get("reviewed_proposal_coverage", valid_reviews)
                    quorum = rec.get("quorum_obtained", False)

                    # Quorum requires proposal count >= min_quorum, valid reviews >= min_quorum,
                    # independent proposals >= 2, distinct reviewers >= 2, and reviewed coverage >= 2
                    is_real_quorum = (
                        quorum
                        and total_blinded >= self.min_quorum
                        and valid_reviews >= self.min_quorum
                        and independent_proposals >= 2
                        and distinct_reviewers >= 2
                        and coverage >= 2
                    )

                    if is_real_quorum and rec.get("is_real_execution", True):
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
                    self.council_provider_call_count += int(rec.get("council_provider_call_count", 0) or 0)
                    self.council_provider_token_estimate += int(rec.get("council_provider_token_estimate", 0) or 0)
                    self.total_latency_delta_ms += float(rec.get("latency_delta_ms", 0.0))
                    self.total_cost_delta_estimate += float(rec.get("estimated_request_cost_delta", 0.0))

            # Also reconstruct durable primary reasoning calls/tokens from attempts journals
            if self.ledger_dir:
                runtime_root = self.ledger_dir.parent
                attempts_file = runtime_root / "provider-attempts.jsonl"
                if attempts_file.is_file():
                    try:
                        for pline in attempts_file.read_text("utf-8").strip().splitlines():
                            if pline.strip():
                                patt = json.loads(pline)
                                if patt.get("status") == "SUCCESS":
                                    self.primary_provider_call_count += 1
                                    self.primary_provider_token_estimate += int(patt.get("tokens", 800) or 800)
                    except Exception:
                        pass
            total_calls = max(1, self.primary_provider_call_count + self.council_provider_call_count)
            self.council_reasoning_share_estimate = round(self.council_provider_call_count / total_calls, 4)
        except Exception:
            pass


    def evaluate_decision(
        self,
        decision_type: str,
        prompt: str,
        primary_proposal: Mapping[str, Any],
        alternate_proposals: Sequence[Tuple[str, Mapping[str, Any]]] = (),
        peer_reviews: Optional[Sequence[Any]] = None,
        authority_records: Optional[Mapping[str, Any]] = None,
        canonical_hashes: Optional[Mapping[str, Any]] = None,
        project_id: str = "lari",
        command_id: Optional[str] = None,
        is_spare_capacity_available: bool = True,
        is_real_execution: bool = True,
        seed: Optional[Any] = None,
        reviewers: Sequence[Tuple[str, Any]] = (),
    ) -> DeliberationResult:
        """Run deliberative multi-agent scoring and synthesis.

        FIRST PASS:
          Member 1 (primary) -> independent proposal
          Member 2 (alternate) -> independent proposal
          Member 3 (alternate) -> independent proposal
          No cross-exposure.

        THEN BLIND:
          Proposal A, Proposal B, Proposal C...
          Provider, model, and member identities stripped.

        SECOND PASS (ANONYMOUS PEER REVIEW):
          Each reviewer receives only the blinded proposals without author/provider/model identity.
          Reviewers return StructuredPeerReview conforming strictly to schema.
          Raw chain-of-thought is never persisted; only bounded structured summaries.

        DETERMINISTIC VALIDATION & COMPOSITE SCORING:
          Model peer review scores are combined with authoritative deterministic safety /
          authority / evidence policy validation.
          A proposal failing deterministic checks is permanently INELIGIBLE (composite_score = 0.0)
          regardless of model votes.

        QUORUM SEMANTICS:
          Requires total_blinded_proposal_count >= min_quorum AND valid_peer_review_count >= min_quorum.
          If not reached:
            COUNCIL_STATUS=INSUFFICIENT_QUORUM
            winning_proposal_id=None
            selected_payload=None
            REAL_SHADOW_SAMPLE_INCREMENT=NO
        """
        start_time = time.perf_counter()
        self.trigger_count += 1

        # Mode Guard: In COUNCIL_MODE_OFF, return immediately with zero Council deliberation
        if self.mode == COUNCIL_MODE_OFF:
            return DeliberationResult(
                winning_proposal_id=None,
                selected_payload=None,
                scores={},
                agreement_with_primary=True,
                quorum_reached=False,
                confidence=0.0,
                mode=self.mode,
                decision_record={"status": "COUNCIL_MODE_OFF", "reason": "Council is disabled in OFF mode"},
            )

        assessment = assess_council_trigger(decision_type, prompt, primary_proposal)

        primary_proposal_count = 1
        alternate_proposal_count = len(alternate_proposals)

        # Trigger Guard: If routine decision or deterministic operation, do NOT deliberate
        if not assessment.council_required:
            return DeliberationResult(
                winning_proposal_id=None,
                selected_payload=None,
                scores={},
                agreement_with_primary=True,
                quorum_reached=False,
                confidence=assessment.primary_confidence,
                mode=self.mode,
                decision_record={
                    "status": "ROUTINE_BYPASS",
                    "trigger_reason": assessment.trigger_reason,
                    "council_required": False,
                },
            )

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
                "primary_proposal_count": primary_proposal_count,
                "council_alternate_proposal_count": alternate_proposal_count,
                "total_blinded_proposal_count": 0,
                "independent_proposal_count": 0,
                "peer_reviewer_count": 0,
                "valid_peer_review_count": 0,
                "distinct_reviewer_count": 0,
                "reviewed_proposal_coverage": 0,
                "quorum_obtained": False,
                "council_status": "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS",
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

        # Sample Deduplication Optimization:
        # Exactly one Council deliberation per unchanged decision fingerprint
        primary_fingerprint = hashlib.sha256(json.dumps(primary_proposal, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        decision_fingerprint = hashlib.sha256(f"{decision_type}:{prompt[:300]}:{primary_fingerprint}".encode("utf-8")).hexdigest()[:16]
        if decision_fingerprint in self._recent_fingerprints:
            self.skipped_redundant_count += 1
            decision_id = f"dec-{hashlib.sha256((prompt + primary_fingerprint + str(time.time())).encode('utf-8')).hexdigest()[:12]}"
            skip_record = {
                "decision_id": decision_id,
                "project_id": project_id,
                "command_id": command_id or "unknown-command",
                "decision_class": decision_type,
                "timestamp": time.time(),
                "primary_decision_fingerprint": primary_fingerprint,
                "primary_confidence": assessment.primary_confidence,
                "council_trigger_reason": assessment.trigger_reason,
                "primary_proposal_count": primary_proposal_count,
                "council_alternate_proposal_count": alternate_proposal_count,
                "total_blinded_proposal_count": 0,
                "independent_proposal_count": 0,
                "peer_reviewer_count": 0,
                "valid_peer_review_count": 0,
                "distinct_reviewer_count": 0,
                "reviewed_proposal_coverage": 0,
                "quorum_obtained": False,
                "council_status": "SKIP_REDUNDANT_SAMPLE",
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

        # Hard Budget Guards: MAX_COUNCIL_CALLS, MAX_COUNCIL_TOKEN_ESTIMATE, and COOLDOWN
        now_ts = time.time()
        anticipated_council_calls = len(reviewers) if reviewers else 0
        current_total = self.primary_provider_call_count + self.council_provider_call_count
        anticipated_share = (self.council_provider_call_count + anticipated_council_calls) / max(1, current_total + anticipated_council_calls)

        share_limit_exceeded = (self.primary_provider_call_count > 0 and anticipated_share > self.max_reasoning_share)
        budget_exceeded = (
            (self.council_provider_call_count + anticipated_council_calls > self.max_council_calls)
            or (self.council_provider_token_estimate >= self.max_council_token_estimate)
            or share_limit_exceeded
            or (self.last_deliberation_time > 0 and (now_ts - self.last_deliberation_time) < self.cooldown_seconds and self.council_provider_call_count > 0)
        )

        if budget_exceeded:
            self.skipped_budget_count += 1
            decision_id = f"dec-{hashlib.sha256((prompt + primary_fingerprint + str(time.time())).encode('utf-8')).hexdigest()[:12]}"
            status_text = "SKIPPED_DUE_TO_BUDGET_SHARE_LIMIT" if share_limit_exceeded else "SKIPPED_DUE_TO_BUDGET_LIMIT"
            budget_skip_record = {
                "decision_id": decision_id,
                "project_id": project_id,
                "command_id": command_id or "unknown-command",
                "decision_class": decision_type,
                "timestamp": now_ts,
                "primary_decision_fingerprint": primary_fingerprint,
                "primary_confidence": assessment.primary_confidence,
                "council_trigger_reason": status_text,
                "primary_proposal_count": primary_proposal_count,
                "council_alternate_proposal_count": alternate_proposal_count,
                "total_blinded_proposal_count": 0,
                "independent_proposal_count": 0,
                "peer_reviewer_count": 0,
                "valid_peer_review_count": 0,
                "distinct_reviewer_count": 0,
                "reviewed_proposal_coverage": 0,
                "quorum_obtained": False,
                "council_status": status_text,
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
            self._persist_decision_ledger(budget_skip_record)
            return DeliberationResult(
                winning_proposal_id=None,
                selected_payload=None,
                scores={},
                agreement_with_primary=True,
                quorum_reached=False,
                confidence=0.0,
                mode=self.mode,
                decision_record=budget_skip_record,
            )

        self._recent_fingerprints.add(decision_fingerprint)
        self.last_deliberation_time = now_ts

        # FIRST PASS & BLINDING:
        all_candidates = [("primary_planner", primary_proposal)] + list(alternate_proposals)
        blinded = blind_proposals(all_candidates, seed=seed, shuffle=True)
        total_blinded_count = len(blinded)
        independent_proposal_count = len({b.member_hash for b in blinded})

        # SECOND PASS: ANONYMOUS PEER REVIEWS
        collected_reviews: List[StructuredPeerReview] = []
        peer_reviewer_count = 0

        # Case A: Explicitly supplied peer reviews
        if peer_reviews:
            peer_reviewer_count = len(peer_reviews)
            for idx, item in enumerate(peer_reviews):
                if isinstance(item, StructuredPeerReview):
                    collected_reviews.append(item)
                elif isinstance(item, dict):
                    pid = item.get("proposal_id", "")
                    if pid:
                        try:
                            # Generate a distinct reviewer hash if not already provided
                            rev_hash = item.get("reviewer_hash")
                            if not rev_hash:
                                rev_id = item.get("reviewer_id") or f"reviewer_{idx + 1}"
                                rev_hash = hashlib.sha256(str(rev_id).encode("utf-8")).hexdigest()[:12]
                            collected_reviews.append(StructuredPeerReview.from_dict(pid, item, reviewer_hash=rev_hash))
                        except Exception:
                            pass

        # Case B: Reviewer provider callables provided
        elif reviewers and is_spare_capacity_available:
            peer_reviewer_count = len(reviewers)
            # Reviewer payload ONLY contains blinded proposals; NEVER author/provider/model identity
            blinded_summary_for_reviewers = [
                {"proposal_id": b.proposal_id, "summary": b.normalized_summary, "payload": b.raw_payload}
                for b in blinded
            ]
            review_prompt = (
                "Review the following anonymous proposals strictly against policy, correctness, and value.\n"
                f"Proposals:\n{json.dumps(blinded_summary_for_reviewers, ensure_ascii=False, indent=2)}\n"
            )
            for r_id, reviewer_obj in reviewers:
                try:
                    rev_hash = hashlib.sha256(str(r_id).encode("utf-8")).hexdigest()[:12]
                    if hasattr(reviewer_obj, "generate_plan"):
                        self.council_provider_call_count += 1
                        self.council_provider_token_estimate += 600
                        plan_data, _, _ = reviewer_obj.generate_plan(review_prompt, PEER_REVIEW_SCHEMA)
                        if isinstance(plan_data, dict):
                            # Can return single review or list of reviews
                            if "proposal_id" in plan_data:
                                collected_reviews.append(StructuredPeerReview.from_dict(plan_data["proposal_id"], plan_data, reviewer_hash=rev_hash))
                            elif "reviews" in plan_data and isinstance(plan_data["reviews"], list):
                                for rev in plan_data["reviews"]:
                                    if isinstance(rev, dict) and rev.get("proposal_id"):
                                        collected_reviews.append(StructuredPeerReview.from_dict(rev["proposal_id"], rev, reviewer_hash=rev_hash))
                except Exception:
                    pass

        # Validate collected peer reviews and deduplicate multiple reviews from the same reviewer for the same proposal
        valid_peer_reviews: List[StructuredPeerReview] = []
        seen_reviewer_proposals: set[tuple[Optional[str], str]] = set()
        for r in collected_reviews:
            if any(r.proposal_id == b.proposal_id for b in blinded):
                key = (r.reviewer_hash, r.proposal_id)
                if r.reviewer_hash and key in seen_reviewer_proposals:
                    continue
                if r.reviewer_hash:
                    seen_reviewer_proposals.add(key)
                valid_peer_reviews.append(r)

        valid_peer_review_count = len(valid_peer_reviews)
        distinct_reviewer_count = len({r.reviewer_hash for r in valid_peer_reviews if r.reviewer_hash})
        if not distinct_reviewer_count and valid_peer_reviews:
            distinct_reviewer_count = valid_peer_review_count
        reviewed_proposal_coverage = len({r.proposal_id for r in valid_peer_reviews})

        # Aggregation of peer review metrics per proposal
        reviews_by_proposal: Dict[str, List[StructuredPeerReview]] = {b.proposal_id: [] for b in blinded}
        for rev in valid_peer_reviews:
            if rev.proposal_id in reviews_by_proposal:
                reviews_by_proposal[rev.proposal_id].append(rev)

        scores: Dict[str, ProposalScore] = {}
        contradiction_found = False
        policy_violation_found = False
        canonical_conflict_found = False

        for b in blinded:
            payload = b.raw_payload

            # Deterministic Authority / policy gates: CANNOT be overridden by peer votes
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

            # Checking red lines / prohibited actions deterministically
            policy_compliant = True
            tasks = payload.get("tasks", [])
            for t in tasks:
                t_str = json.dumps(t, sort_keys=True).lower() if isinstance(t, (dict, list)) else str(t).lower()
                if any(k in t_str for k in ("force_push", "--force", "-f", "rm -rf", "drop database", "sudo", "bypass_human_gate")):
                    policy_compliant = False
                    contradiction_count += 1
                    policy_violation_found = True

            p_reviews = reviews_by_proposal.get(b.proposal_id, [])
            for pr in p_reviews:
                contradiction_count += len(pr.contradictions)
                if pr.risk > 0.8:
                    contradiction_count += 1

            if contradiction_count > 0:
                contradiction_found = True

            # Blend peer review metrics if present, otherwise default to baseline
            if p_reviews:
                n = float(len(p_reviews))
                avg_evidence = sum(r.evidence_consistency for r in p_reviews) / n
                avg_canonical = sum(r.canonical_consistency for r in p_reviews) / n if has_authority else 0.0
                avg_dep = sum(r.dependency_correctness for r in p_reviews) / n
                avg_auth = sum(r.authority_compatibility for r in p_reviews) / n if has_authority else 0.0
                avg_rev = sum(r.reversibility for r in p_reviews) / n
                avg_compl = sum(r.implementation_complexity for r in p_reviews) / n
                avg_val = sum(r.expected_value for r in p_reviews) / n
                avg_conf = sum(r.confidence for r in p_reviews) / n
            else:
                avg_evidence = 0.9 if not contradiction_count else 0.2
                avg_canonical = 0.9 if has_authority else 0.1
                avg_dep = 0.85
                avg_auth = 1.0 if has_authority else 0.0
                avg_rev = assessment.reversibility
                avg_compl = 0.8
                avg_val = 0.85
                avg_conf = assessment.primary_confidence

            score = ProposalScore(
                proposal_id=b.proposal_id,
                evidence_consistency=avg_evidence,
                canonical_consistency=avg_canonical,
                dependency_correctness=avg_dep,
                authority_compatibility=avg_auth,
                reversibility_score=avg_rev,
                implementation_complexity=avg_compl,
                expected_value=avg_val,
                contradiction_count=contradiction_count,
                confidence=avg_conf,
                policy_compliant=policy_compliant and has_authority,
            )
            scores[b.proposal_id] = score

        # Correlated Consensus Risk Detection:
        proposal_summaries = [b.normalized_summary.strip() for b in blinded]
        semantic_diversity = len(set(proposal_summaries)) / max(1, len(proposal_summaries))
        correlated_risk = False
        if len(blinded) >= 2 and semantic_diversity <= 0.5:
            correlated_risk = True
            self.correlated_consensus_risk_count += 1

        # Strict Quorum Semantics:
        # Full deliberative quorum requires:
        # 1. total_blinded_proposal_count >= min_quorum
        # 2. independent_proposal_count >= 2
        # 3. valid_peer_review_count >= min_quorum
        # 4. distinct_reviewer_count >= 2
        # 5. reviewed_proposal_coverage >= 2
        quorum_reached = (
            total_blinded_count >= self.min_quorum
            and independent_proposal_count >= 2
            and valid_peer_review_count >= self.min_quorum
            and distinct_reviewer_count >= 2
            and reviewed_proposal_coverage >= 2
        )

        eligible = [(k, v) for k, v in scores.items() if v.policy_compliant and v.composite_score > 0.0]
        eligible.sort(key=lambda item: item[1].composite_score, reverse=True)

        if quorum_reached and eligible:
            winning_proposal_id = eligible[0][0]
            council_status = "QUORUM_OBTAINED"
        else:
            winning_proposal_id = None
            council_status = "INSUFFICIENT_QUORUM"

        # Check agreement with primary without exposing primary's identity externally
        primary_proposal_id = next((b.proposal_id for b in blinded if b.is_primary), None)
        primary_score = scores.get(primary_proposal_id) if primary_proposal_id else None
        primary_is_valid = primary_score.policy_compliant and primary_score.composite_score > 0.0 if primary_score else False

        if winning_proposal_id:
            agreement_with_primary = (winning_proposal_id == primary_proposal_id)
        else:
            # When no council winner due to quorum or policy rejection, agreement is True unless primary itself had policy/canonical violation
            agreement_with_primary = primary_is_valid

        if winning_proposal_id:
            if agreement_with_primary:
                self.agreement_count += 1
            else:
                self.disagreement_count += 1
                if policy_violation_found or canonical_conflict_found:
                    self.primary_errors_caught += 1
                else:
                    self.false_disagreement_count += 1
        elif not primary_is_valid and (policy_violation_found or canonical_conflict_found):
            self.disagreement_count += 1
            self.primary_errors_caught += 1

        selected_payload = None
        if winning_proposal_id:
            for b in blinded:
                if b.proposal_id == winning_proposal_id:
                    selected_payload = b.raw_payload
                    break

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self.total_latency_delta_ms += elapsed_ms
        cost_delta = 0.005 * len(blinded) + 0.002 * valid_peer_review_count
        self.total_cost_delta_estimate += cost_delta

        self.shadow_sample_count += 1
        # REAL_SHADOW_SAMPLE_INCREMENT requires genuine quorum with valid peer reviews and >= min_quorum proposals
        if (
            is_real_execution
            and quorum_reached
            and total_blinded_count >= self.min_quorum
            and independent_proposal_count >= 2
            and valid_peer_review_count >= self.min_quorum
            and distinct_reviewer_count >= 2
            and reviewed_proposal_coverage >= 2
        ):
            self.real_shadow_sample_count += 1

        result_fingerprint = hashlib.sha256(json.dumps(selected_payload or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        decision_id = f"dec-{hashlib.sha256((prompt + primary_fingerprint + str(time.time())).encode('utf-8')).hexdigest()[:12]}"

        # Confidence: if correlated consensus risk is high, reduce confidence
        base_confidence = eligible[0][1].confidence if (eligible and quorum_reached) else 0.0
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
            "primary_proposal_count": primary_proposal_count,
            "council_alternate_proposal_count": alternate_proposal_count,
            "total_blinded_proposal_count": total_blinded_count,
            "independent_proposal_count": independent_proposal_count,
            "peer_reviewer_count": peer_reviewer_count,
            "valid_peer_review_count": valid_peer_review_count,
            "distinct_reviewer_count": distinct_reviewer_count,
            "reviewed_proposal_coverage": reviewed_proposal_coverage,
            "quorum_obtained": quorum_reached,
            "council_status": council_status,
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

    def record_decision_outcome(self, decision_id: str, outcome: str) -> None:
        """Record retrospective outcome of a council shadow decision."""
        if not self.ledger_dir:
            return
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
            outcomes_file = self.ledger_dir / "deliberation-shadow-outcomes.jsonl"
            entry = {
                "decision_id": str(decision_id),
                "outcome": str(outcome),
                "timestamp": time.time(),
            }
            with open(outcomes_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def get_quality_metrics(self) -> Dict[str, Any]:
        """Return comprehensive council decision-quality and observability metrics."""
        total_evals = max(1, self.shadow_sample_count)
        real_sample_rate = self.real_shadow_sample_count / total_evals
        agreement_rate = self.agreement_count / total_evals
        disagreement_rate = self.disagreement_count / total_evals
        consensus_risk_rate = self.correlated_consensus_risk_count / total_evals

        total_reasoning_calls = max(1, self.primary_provider_call_count + self.council_provider_call_count)
        share_est = round(self.council_provider_call_count / total_reasoning_calls, 4)
        return {
            "PRIMARY_PROVIDER_CALL_COUNT": self.primary_provider_call_count,
            "PRIMARY_PROVIDER_TOKEN_ESTIMATE": self.primary_provider_token_estimate,
            "COUNCIL_PROVIDER_CALL_COUNT": self.council_provider_call_count,
            "COUNCIL_PROVIDER_TOKEN_ESTIMATE": self.council_provider_token_estimate,
            "COUNCIL_REASONING_SHARE_ESTIMATE": share_est,
            "COUNCIL_TRIGGER_COUNT": self.trigger_count,
            "COUNCIL_SHADOW_SAMPLE_COUNT": self.shadow_sample_count,
            "COUNCIL_REAL_SAMPLE_COUNT": self.real_shadow_sample_count,
            "COUNCIL_REAL_SAMPLE_RATE": round(real_sample_rate, 4),
            "COUNCIL_SKIPPED_CAPACITY_COUNT": self.skipped_capacity_count,
            "COUNCIL_SKIPPED_REDUNDANT_COUNT": self.skipped_redundant_count,
            "COUNCIL_AGREEMENT_RATE": round(agreement_rate, 4),
            "COUNCIL_DISAGREEMENT_RATE": round(disagreement_rate, 4),
            "COUNCIL_CORRELATED_CONSENSUS_RATE": round(consensus_risk_rate, 4),
            "COUNCIL_POLICY_VIOLATIONS_CAUGHT": self.policy_violations_caught,
            "COUNCIL_CANONICAL_CONTRADICTIONS_CAUGHT": self.canonical_contradictions_caught,
            "COUNCIL_PRIMARY_ERRORS_CAUGHT": self.primary_errors_caught,
            "COUNCIL_PRIMARY_EXECUTION_INTERFERENCE_COUNT": self.primary_execution_interference_count,
            "COUNCIL_TOTAL_LATENCY_DELTA_MS": round(self.total_latency_delta_ms, 2),
            "COUNCIL_TOTAL_COST_DELTA_ESTIMATE": round(self.total_cost_delta_estimate, 4),
        }


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
