"""Design Intelligence Contracts & Schemas (R10).

Provides versioned dataclasses with strict validation, deterministic JSON serialization,
and provenance tracking for all design-related judgment structures.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import datetime
import json
import uuid


class JudgmentVerdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class FactType(str, Enum):
    CANONICAL_PRODUCT_FACT = "CANONICAL_PRODUCT_FACT"
    CANONICAL_TENANT_FACT = "CANONICAL_TENANT_FACT"
    DESIGN_INFERENCE = "DESIGN_INFERENCE"
    REFERENCE_DERIVED_PRINCIPLE = "REFERENCE_DERIVED_PRINCIPLE"
    HUMAN_PREFERENCE = "HUMAN_PREFERENCE"
    PLACEHOLDER_VISUAL_CONTENT = "PLACEHOLDER_VISUAL_CONTENT"


class EvidenceModality(str, Enum):
    STATIC_SOURCE_HEURISTIC = "STATIC_SOURCE_HEURISTIC"
    STRUCTURED_SEMANTIC = "STRUCTURED_SEMANTIC"
    DOM_METRIC = "DOM_METRIC"
    PIXEL_VISUAL = "PIXEL_VISUAL"
    HUMAN_VISUAL = "HUMAN_VISUAL"


class VisualQACoverage(str, Enum):
    FULL_PASS = "FULL_PASS"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    FAIL = "FAIL"


class HumanReviewReadinessState(str, Enum):
    DESIGN_DISCOVERY_COMPLETE = "DESIGN_DISCOVERY_COMPLETE"
    VISUAL_CRITIC_COMPLETE = "VISUAL_CRITIC_COMPLETE"
    HUMAN_VISUAL_REVIEW_READY = "HUMAN_VISUAL_REVIEW_READY"
    HUMAN_ACCEPTED = "HUMAN_ACCEPTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"


class FeedbackRating(str, Enum):
    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    TOO_GENERIC = "TOO_GENERIC"
    TOO_BUSY = "TOO_BUSY"
    TOO_EMPTY = "TOO_EMPTY"
    PREMIUM = "PREMIUM"
    CHEAP_LOOKING = "CHEAP_LOOKING"
    GOOD_HERO = "GOOD_HERO"
    BAD_HERO = "BAD_HERO"
    GOOD_MOTION = "GOOD_MOTION"
    EXCESSIVE_MOTION = "EXCESSIVE_MOTION"
    GOOD_PRODUCT_REVEAL = "GOOD_PRODUCT_REVEAL"


class ContentBlockCategory(str, Enum):
    FACTUAL = "FACTUAL"
    INFERENTIAL = "INFERENTIAL"
    DECORATIVE_OR_UI = "DECORATIVE_OR_UI"


class EvidenceOrigin(str, Enum):
    FAKE_TEST_ARTIFACT = "FAKE_TEST_ARTIFACT"
    SIMULATED_VISUAL_TEST = "SIMULATED_VISUAL_TEST"
    REAL_LOCAL_BROWSER_SCREENSHOT = "REAL_LOCAL_BROWSER_SCREENSHOT"
    REAL_PROVIDER_VISUAL_REVIEW = "REAL_PROVIDER_VISUAL_REVIEW"


@dataclass
class GroundedFact:
    fact_id: str
    fact_type: FactType
    value: str
    source_type: str
    source_reference: str
    confidence: float = 1.0


@dataclass
class GroundedFactLedger:
    ledger_id: str
    project_id: str
    facts: List[GroundedFact] = field(default_factory=list)

    def add_fact(self, fact: GroundedFact) -> None:
        self.facts.append(fact)

    def get_fact_by_id(self, fact_id: str) -> Optional[GroundedFact]:
        return next((f for f in self.facts if f.fact_id == fact_id), None)

    def get_canonical_facts(self) -> List[GroundedFact]:
        return [f for f in self.facts if f.fact_type in (FactType.CANONICAL_PRODUCT_FACT, FactType.CANONICAL_TENANT_FACT)]


@dataclass
class GroundedContentBlock:
    text: str
    semantic_role: str
    provenance_kind: FactType
    source_fact_ids: List[str]
    category: ContentBlockCategory = ContentBlockCategory.FACTUAL
    is_customer_facing: bool = True


@dataclass
class GroundedContentManifest:
    manifest_id: str
    project_id: str
    blocks: List[GroundedContentBlock] = field(default_factory=list)
    unmanifested_customer_facing_text_detected: bool = False

    def is_complete(self) -> bool:
        return not self.unmanifested_customer_facing_text_detected and len(self.blocks) > 0


@dataclass
class DesignProjectBrief:
    brief_id: str
    project_id: str
    tenant_name: str
    industry: str
    target_audience: str
    core_job_to_be_done: str
    brand_posture: str
    supported_claims: List[str] = field(default_factory=list)
    fact_ledger: Optional[GroundedFactLedger] = None
    version: str = "v1.1"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())



@dataclass
class ReferenceSource:
    source_id: str
    url_or_name: str
    purpose: str
    strength: str
    integration_cost: str
    dependency_cost: str
    license_provenance: str
    supply_chain_risk: str
    generic_design_risk: str
    recommended_use: str
    do_not_use_conditions: List[str] = field(default_factory=list)
    observation: str = ""
    retrieval_timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    is_placeholder: bool = False


@dataclass
class ReferenceSignal:
    signal_id: str
    source_id: str
    category: str  # hero_composition, typography, motion, CTAs, trust_mechanisms
    observation: str
    extracted_principle: str
    confidence_score: float = 1.0


@dataclass
class SalesFoldSpec:
    understandable_within_seconds: bool
    target_persona_clear: bool
    primary_value_prop: str
    visible_product_evidence: bool
    dominant_cta_text: str
    cta_result_understandable: bool
    creates_credible_desire: bool
    unsupported_claims_detected: List[str] = field(default_factory=list)


@dataclass
class ProductStorySpec:
    spec_id: str
    hero_narrative: str
    value_pillars: List[str]
    product_proof_points: List[str]
    sales_fold: SalesFoldSpec
    version: str = "v1.0"


@dataclass
class DesignDNA:
    dna_id: str
    project_id: str
    visual_personality: str
    typography_pairings: List[str]
    color_palette: List[str]
    motion_philosophy: str
    grid_structure: str
    mobile_strategy: str
    trust_strategy: str
    version: str = "v1.0"


@dataclass
class VisualEvidenceManifest:
    manifest_id: str
    run_id: str
    viewports_captured: List[int]  # [375, 390, 768, 1024, 1440, 1920]
    screenshot_paths: Dict[int, str]  # viewport -> path
    horizontal_overflow_detected: Dict[int, bool] = field(default_factory=dict)
    cta_visible: Dict[int, bool] = field(default_factory=dict)
    capture_mode: str = "FAKE_TEST_ARTIFACT"
    capture_adapter: str = "FakeBrowserScreenshotAdapter"
    file_hashes: Dict[int, str] = field(default_factory=dict)
    captured_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())



@dataclass
class CritiqueFinding:
    finding_id: str
    critic_name: str
    verdict: JudgmentVerdict
    dimension: str
    title: str
    details: str
    evidence_modality: EvidenceModality = EvidenceModality.STATIC_SOURCE_HEURISTIC
    evidence_ids: List[str] = field(default_factory=list)
    suggested_fix: Optional[str] = None


@dataclass
class CritiqueScorecard:
    scorecard_id: str
    project_id: str
    overall_verdict: JudgmentVerdict
    critic_findings: List[CritiqueFinding]
    scored_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def has_critical_failure(self) -> bool:
        return any(f.verdict == JudgmentVerdict.FAIL for f in self.critic_findings)


@dataclass
class HumanDesignFeedback:
    feedback_id: str
    project_id: str
    source_attribution: str  # e.g., "human_user:alice"
    rating: FeedbackRating
    target_element: str  # e.g., "hero_section"
    comments: Optional[str] = None
    is_reversible: bool = True
    recorded_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())


@dataclass
class DesignPreferenceProfile:
    profile_id: str
    project_id: str
    liked_principles: List[str] = field(default_factory=list)
    disliked_anti_patterns: List[str] = field(default_factory=list)
    feedback_history: List[HumanDesignFeedback] = field(default_factory=list)


@dataclass
class ProductDemoVideoSpec:
    spec_id: str
    title: str
    duration_seconds: float
    scene_script: List[Dict[str, Any]]
    renderer_adapter_type: str = "v1_stub_adapter"
    version: str = "v1.0"


@dataclass
class DesignRecommendation:
    recommendation_id: str
    project_id: str
    recommended_dna: Optional[DesignDNA] = None
    product_story: Optional[ProductStorySpec] = None
    provenance_feedback_ids: List[str] = field(default_factory=list)
    rationale: str = ""
    recommended_concept: str = "NONE"  # Set to NONE when weak or forced


@dataclass
class BenchmarkResult:
    benchmark_id: str
    fixture_name: str
    passed: bool
    detected_failures: List[str]
    false_positives: List[str]
    false_negatives: List[str]
    duration_seconds: float

