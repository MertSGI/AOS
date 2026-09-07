"""Autonomous Design Loop Pipeline (R17).

Coordinates 8 bounded roles through research, DNA generation, product story,
visual implementation, critic ensemble evaluation, and bounded revision loops.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
import uuid
from extensions.design_intelligence.contracts import (
    DesignProjectBrief,
    DesignDNA,
    ProductStorySpec,
    CritiqueScorecard,
    JudgmentVerdict,
    DesignRecommendation,
    GroundedFactLedger,
    GroundedFact,
    GroundedContentManifest,
    FactType,
    HumanReviewReadinessState,
    VisualEvidenceManifest,
    EvidenceModality,
    EvidenceOrigin,
)
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence
from extensions.design_intelligence.design_dna import DesignDNAEngine
from extensions.design_intelligence.critics import DesignCriticEnsemble
from extensions.design_intelligence.taste_memory import TasteMemory


class DesignRole(str, Enum):
    REFERENCE_RESEARCHER = "REFERENCE_RESEARCHER"
    PRODUCT_STORY_DESIGNER = "PRODUCT_STORY_DESIGNER"
    VISUAL_DESIGNER = "VISUAL_DESIGNER"
    GROUNDING_CRITIC = "GROUNDING_CRITIC"
    ANTI_GENERIC_CRITIC = "ANTI_GENERIC_CRITIC"
    CONVERSION_CRITIC = "CONVERSION_CRITIC"
    ACCESSIBILITY_CRITIC = "ACCESSIBILITY_CRITIC"
    PRODUCT_SEMANTICS_CRITIC = "PRODUCT_SEMANTICS_CRITIC"
    FINAL_DESIGN_REVIEWER = "FINAL_DESIGN_REVIEWER"


@dataclass
class DesignLoopResult:
    pipeline_id: str
    project_id: str
    overall_verdict: JudgmentVerdict
    cycles_completed: int
    max_cycles: int
    final_scorecard: CritiqueScorecard
    recommendation: Optional[DesignRecommendation]
    human_review_state: HumanReviewReadinessState = HumanReviewReadinessState.DESIGN_DISCOVERY_COMPLETE
    human_review_required_with_blockers: bool = False
    blockers: List[str] = field(default_factory=list)


def evaluate_human_visual_review_readiness(
    scorecard: CritiqueScorecard,
    fact_ledger: Optional[GroundedFactLedger],
    content_manifest: Optional[GroundedContentManifest],
    ref_intel: Optional[ReferenceIntelligence],
    evidence_manifest: Optional[VisualEvidenceManifest],
    visual_adapter: Optional[Any],
) -> Dict[str, Any]:
    """Central evaluator for HUMAN_VISUAL_REVIEW_READY state (Section 2, 3, 12).
    
    Requires ALL:
    1. GroundingIntegrityCritic = PASS
    2. GroundedContentManifest is not None AND is_complete() == True (Section 2)
    3. Reference Intelligence present AND at least 1 valid reference signal (Section 3)
    4. Visual QA = FULL_PASS
    5. Real screenshot artifact integrity = PASS
    6. Capture origin = REAL_LOCAL_BROWSER_SCREENSHOT
    7. REAL VisualCriticAdapter actually executed
    8. Real visual finding modality/origin confirmed
    9. No critical static/semantic/visual critic FAIL
    10. Inspectable screenshot paths are present
    """
    reasons = []

    # 1 & 9. Static & semantic critic check
    if scorecard.has_critical_failure():
        reasons.append("CritiqueScorecard has critical FAIL findings")

    grounding_finding = next((f for f in scorecard.critic_findings if f.critic_name == "GroundingIntegrityCritic"), None)
    if not grounding_finding or grounding_finding.verdict != JudgmentVerdict.PASS:
        reasons.append("GroundingIntegrityCritic did not PASS")

    # 2. Content manifest completeness (Mandatory Section 2)
    if content_manifest is None:
        reasons.append("GROUNDING_CONTENT_MANIFEST_MISSING: GroundedContentManifest is None")
    elif not content_manifest.is_complete():
        reasons.append("GroundedContentManifest is incomplete")

    # 3. Reference evidence mandatory (Section 3)
    if ref_intel is None:
        reasons.append("REFERENCE_PROVENANCE_EVIDENCE_MISSING: ReferenceIntelligence is None")
    else:
        signals = ref_intel.query_signals()
        if not signals:
            reasons.append("REFERENCE_PROVENANCE_EVIDENCE_MISSING: Zero evidence-eligible reference signals found")
        else:
            for sig in signals:
                src = ref_intel._sources.get(sig.source_id)
                if not src or not ref_intel.validate_reference_source(src):
                    reasons.append(f"Reference signal '{sig.signal_id}' uses invalid source '{sig.source_id}'")

    # 4 & 5 & 6 & 10. Evidence manifest & artifact integrity
    if not evidence_manifest:
        reasons.append("No VisualEvidenceManifest present")
    else:
        if manifest_capture_mode := getattr(evidence_manifest, "capture_mode", None):
            if manifest_capture_mode != "REAL_LOCAL_BROWSER_SCREENSHOT":
                reasons.append(f"Capture origin '{manifest_capture_mode}' is not REAL_LOCAL_BROWSER_SCREENSHOT")
        else:
            reasons.append("VisualEvidenceManifest missing capture_mode")

        if not evidence_manifest.screenshot_paths:
            reasons.append("No screenshot paths present in manifest")
        else:
            from extensions.design_intelligence.visual_qa import validate_real_artifact_integrity, VisualQAEvaluator
            qa_res = VisualQAEvaluator(check_file_existence=True).evaluate_manifest(evidence_manifest)
            if not qa_res.overall_pass:
                reasons.append(f"Visual QA is not FULL_PASS (status: {qa_res.coverage_status.value})")

            integ = validate_real_artifact_integrity(evidence_manifest)
            if not integ["valid"]:
                reasons.append(f"Artifact integrity failure: {'; '.join(integ['errors'])}")

    # 7 & 8. Real VisualCriticAdapter & provider provenance check (Section 3 & 4 - R3)
    if not visual_adapter:
        reasons.append("No VisualCriticAdapter executed")
    elif visual_adapter.__class__.__name__ == "FakeVisualCriticAdapter":
        reasons.append("FakeVisualCriticAdapter used (test-only, cannot grant human-ready)")
    else:
        origin = getattr(visual_adapter, "evidence_origin", None)
        if not origin:
            reasons.append("Visual adapter missing declared evidence origin")
        elif origin != EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW:
            reasons.append(f"Visual adapter evidence origin '{origin}' is not REAL_PROVIDER_VISUAL_REVIEW")
        
        vis_finding = next((f for f in scorecard.critic_findings if f.dimension == "pixel_visual_quality"), None)
        if not vis_finding:
            reasons.append("No pixel_visual_quality finding present")
        elif vis_finding.verdict != JudgmentVerdict.PASS:
            reasons.append("Real visual critic evaluation did not PASS")
        elif vis_finding.evidence_modality != EvidenceModality.PIXEL_VISUAL:
            reasons.append(f"Visual finding evidence modality '{vis_finding.evidence_modality}' is not PIXEL_VISUAL")

    is_ready = len(reasons) == 0
    return {
        "is_ready": is_ready,
        "state": HumanReviewReadinessState.HUMAN_VISUAL_REVIEW_READY if is_ready else HumanReviewReadinessState.DESIGN_DISCOVERY_COMPLETE,
        "reasons": reasons,
    }


class AutonomousDesignLoopPipeline:
    """Multi-role autonomous design loop pipeline (V1.1)."""

    NO_DEFAULT_CONCEPT_WINNER = "YES"
    NO_FORCED_LEAST_BAD_RECOMMENDATION = "YES"
    AUTOMATED_HUMAN_ACCEPTED_TRANSITION_COUNT = 0

    def __init__(
        self,
        ref_intel: Optional[ReferenceIntelligence] = None,
        dna_engine: Optional[DesignDNAEngine] = None,
        critic_ensemble: Optional[DesignCriticEnsemble] = None,
        taste_memory: Optional[TasteMemory] = None,
        max_design_review_cycles: int = 3,
    ):
        self.ref_intel = ref_intel or ReferenceIntelligence()
        self.dna_engine = dna_engine or DesignDNAEngine()
        self.critic_ensemble = critic_ensemble or DesignCriticEnsemble()
        self.taste_memory = taste_memory or TasteMemory()
        self.max_design_review_cycles = max_design_review_cycles

    def run_pipeline(
        self,
        brief: DesignProjectBrief,
        initial_html: str,
        initial_css: str,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
        content_manifest: Optional[GroundedContentManifest] = None,
        concept_id: Optional[str] = None,
    ) -> DesignLoopResult:
        pid = f"loop-{uuid.uuid4().hex[:8]}"

        # Grounded Fact Ledger extraction / binding
        fact_ledger = brief.fact_ledger or GroundedFactLedger(
            ledger_id=f"ledger-{uuid.uuid4().hex[:6]}",
            project_id=brief.project_id,
        )
        if not fact_ledger.facts:
            fact_ledger.add_fact(GroundedFact(
                fact_id=f"f-ten-{uuid.uuid4().hex[:4]}",
                fact_type=FactType.CANONICAL_TENANT_FACT,
                value=brief.tenant_name,
                source_type="DesignProjectBrief",
                source_reference="brief.tenant_name",
            ))
            for sc in brief.supported_claims:
                fact_ledger.add_fact(GroundedFact(
                    fact_id=f"f-claim-{uuid.uuid4().hex[:4]}",
                    fact_type=FactType.CANONICAL_PRODUCT_FACT,
                    value=sc,
                    source_type="DesignProjectBrief",
                    source_reference="brief.supported_claims",
                ))

        # Role 1: REFERENCE_RESEARCHER
        sources = self.ref_intel.query_signals()

        # Role 2: PRODUCT_STORY_DESIGNER & VISUAL_DESIGNER
        dna = self.dna_engine.generate_dna(brief)
        story = self.dna_engine.generate_product_story(brief)

        html_current = initial_html
        css_current = initial_css

        current_cycle = 0
        last_scorecard = None

        while current_cycle < self.max_design_review_cycles:
            current_cycle += 1

            # Role 4-7: CRITIC ENSEMBLE evaluation
            scorecard = self.critic_ensemble.evaluate_project(
                project_id=brief.project_id,
                html_content=html_current,
                css_content=css_current,
                dna=dna,
                story=story,
                evidence_manifest=evidence_manifest,
                fact_ledger=fact_ledger,
                content_manifest=content_manifest,
            )
            last_scorecard = scorecard

            # Evaluate central human review readiness gate (Section 12)
            gate_res = evaluate_human_visual_review_readiness(
                scorecard=scorecard,
                fact_ledger=fact_ledger,
                content_manifest=content_manifest,
                ref_intel=self.ref_intel,
                evidence_manifest=evidence_manifest,
                visual_adapter=self.critic_ensemble.visual_adapter,
            )

            # Role 8: FINAL_DESIGN_REVIEWER check
            if scorecard.overall_verdict == JudgmentVerdict.PASS:
                rec = self.taste_memory.generate_explainable_recommendation(brief.project_id, dna, story)
                
                # Section 2 & 8 (R3): Remove synthetic default winner.
                # gate FAIL + concept_id supplied => NONE
                # gate PASS + concept_id missing => NONE
                # gate PASS + actual evaluated concept_id supplied => concept_id
                if not gate_res["is_ready"]:
                    rec.recommended_concept = "NONE"
                else:
                    rec.recommended_concept = concept_id if concept_id else "NONE"

                # Section 9: Blocker reporting when gate is not ready
                has_blockers = not gate_res["is_ready"]
                blockers = gate_res["reasons"] if not gate_res["is_ready"] else []

                return DesignLoopResult(
                    pipeline_id=pid,
                    project_id=brief.project_id,
                    overall_verdict=JudgmentVerdict.PASS,
                    cycles_completed=current_cycle,
                    max_cycles=self.max_design_review_cycles,
                    final_scorecard=scorecard,
                    recommendation=rec,
                    human_review_state=gate_res["state"],
                    human_review_required_with_blockers=has_blockers,
                    blockers=blockers,
                )

            # Perform revision if issues remain
            for finding in scorecard.critic_findings:
                if finding.verdict == JudgmentVerdict.FAIL:
                    if "v2 pilot" in html_current.lower():
                        html_current = html_current.replace("v2 pilot", "")
                    if "aos-runtime" in html_current.lower():
                        html_current = html_current.replace("aos-runtime", "")

        # If max cycles reached with remaining failures
        # Section 8 & 9: NO_FORCED_LEAST_BAD_RECOMMENDATION=YES -> RECOMMENDED_CONCEPT = NONE
        blockers = [f.details for f in last_scorecard.critic_findings if f.verdict == JudgmentVerdict.FAIL]  # type: ignore
        return DesignLoopResult(
            pipeline_id=pid,
            project_id=brief.project_id,
            overall_verdict=JudgmentVerdict.FAIL,
            cycles_completed=current_cycle,
            max_cycles=self.max_design_review_cycles,
            final_scorecard=last_scorecard,  # type: ignore
            recommendation=DesignRecommendation(
                recommendation_id=f"rec-none-{uuid.uuid4().hex[:6]}",
                project_id=brief.project_id,
                recommended_dna=None,
                product_story=None,
                recommended_concept="NONE",
                rationale="No concept recommended: Critic ensemble contains failing findings (NO_FORCED_LEAST_BAD_RECOMMENDATION=YES).",
            ),
            human_review_state=HumanReviewReadinessState.DESIGN_DISCOVERY_COMPLETE,
            human_review_required_with_blockers=len(blockers) > 0,
            blockers=blockers,
        )



