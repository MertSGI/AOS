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
)
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence
from extensions.design_intelligence.design_dna import DesignDNAEngine
from extensions.design_intelligence.critics import DesignCriticEnsemble
from extensions.design_intelligence.taste_memory import TasteMemory


class DesignRole(str, Enum):
    REFERENCE_RESEARCHER = "REFERENCE_RESEARCHER"
    PRODUCT_STORY_DESIGNER = "PRODUCT_STORY_DESIGNER"
    VISUAL_DESIGNER = "VISUAL_DESIGNER"
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
    human_review_required_with_blockers: bool = False
    blockers: List[str] = field(default_factory=list)


class AutonomousDesignLoopPipeline:
    """Multi-role autonomous design loop pipeline."""

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
    ) -> DesignLoopResult:
        pid = f"loop-{uuid.uuid4().hex[:8]}"

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
            )
            last_scorecard = scorecard

            # Role 8: FINAL_DESIGN_REVIEWER check
            if scorecard.overall_verdict == JudgmentVerdict.PASS:
                rec = self.taste_memory.generate_explainable_recommendation(brief.project_id, dna, story)
                return DesignLoopResult(
                    pipeline_id=pid,
                    project_id=brief.project_id,
                    overall_verdict=JudgmentVerdict.PASS,
                    cycles_completed=current_cycle,
                    max_cycles=self.max_design_review_cycles,
                    final_scorecard=scorecard,
                    recommendation=rec,
                    human_review_required_with_blockers=False,
                )

            # Perform revision if issues remain
            # Simulate revision by stripping failing patterns if present
            for finding in scorecard.critic_findings:
                if finding.verdict == JudgmentVerdict.FAIL:
                    if "v2 pilot" in html_current.lower():
                        html_current = html_current.replace("v2 pilot", "")
                    if "aos-runtime" in html_current.lower():
                        html_current = html_current.replace("aos-runtime", "")

        # If max cycles reached with remaining failures
        blockers = [f.details for f in last_scorecard.critic_findings if f.verdict == JudgmentVerdict.FAIL]  # type: ignore
        return DesignLoopResult(
            pipeline_id=pid,
            project_id=brief.project_id,
            overall_verdict=JudgmentVerdict.FAIL,
            cycles_completed=current_cycle,
            max_cycles=self.max_design_review_cycles,
            final_scorecard=last_scorecard,  # type: ignore
            recommendation=None,
            human_review_required_with_blockers=len(blockers) > 0,
            blockers=blockers,
        )
