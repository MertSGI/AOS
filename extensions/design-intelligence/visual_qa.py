"""Visual Evidence & Responsive QA Subsystem (R14).

Analyzes multi-viewport visual artifacts (375, 390, 768, 1024, 1440, 1920 px)
via abstract browser adapters to evaluate horizontal overflow, CTA visibility, and layout hierarchy.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import uuid
from extensions.design_intelligence.contracts import VisualEvidenceManifest


REQUIRED_VIEWPORTS = [375, 390, 768, 1024, 1440, 1920]


@dataclass
class VisualQAResult:
    qa_id: str
    run_id: str
    all_required_viewports_covered: bool
    overflow_detected_viewports: List[int]
    cta_missing_viewports: List[int]
    overall_pass: bool
    evidence_ids: List[str]


class BaseBrowserScreenshotAdapter:
    """Interface for visual screenshot adapters."""

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        raise NotImplementedError


class FakeBrowserScreenshotAdapter(BaseBrowserScreenshotAdapter):
    """Deterministic offline browser screenshot adapter."""

    def __init__(self, simulate_overflow: bool = False, simulate_missing_cta: bool = False):
        self.simulate_overflow = simulate_overflow
        self.simulate_missing_cta = simulate_missing_cta

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        screenshots = {}
        overflows = {}
        cta_vis = {}

        for vp in REQUIRED_VIEWPORTS:
            screenshots[vp] = f"/artifacts/screenshots/{run_id}_{vp}px.png"
            overflows[vp] = self.simulate_overflow and vp < 768
            cta_vis[vp] = not (self.simulate_missing_cta and vp == 375)

        return VisualEvidenceManifest(
            manifest_id=f"vis-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths=screenshots,
            horizontal_overflow_detected=overflows,
            cta_visible=cta_vis,
        )


class VisualQAEvaluator:
    """Evaluates VisualEvidenceManifest against responsive QA rules."""

    def evaluate_manifest(self, manifest: VisualEvidenceManifest) -> VisualQAResult:
        missing_viewports = [vp for vp in REQUIRED_VIEWPORTS if vp not in manifest.viewports_captured]
        all_covered = len(missing_viewports) == 0

        overflows = [vp for vp, has_of in manifest.horizontal_overflow_detected.items() if has_of]
        missing_ctas = [vp for vp, is_vis in manifest.cta_visible.items() if not is_vis]

        overall_pass = all_covered and len(overflows) == 0 and len(missing_ctas) == 0

        return VisualQAResult(
            qa_id=f"qa-{uuid.uuid4().hex[:8]}",
            run_id=manifest.run_id,
            all_required_viewports_covered=all_covered,
            overflow_detected_viewports=overflows,
            cta_missing_viewports=missing_ctas,
            overall_pass=overall_pass,
            evidence_ids=[manifest.manifest_id],
        )
