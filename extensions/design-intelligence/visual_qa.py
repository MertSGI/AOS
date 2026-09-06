from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import hashlib
import uuid
from extensions.design_intelligence.contracts import (
    VisualEvidenceManifest,
    VisualQACoverage,
)

REQUIRED_VIEWPORTS = [375, 390, 768, 1024, 1440, 1920]


@dataclass
class VisualQAResult:
    qa_id: str
    run_id: str
    coverage_status: VisualQACoverage
    all_required_viewports_covered: bool
    overflow_detected_viewports: List[int]
    cta_missing_viewports: List[int]
    missing_viewports: List[int]
    nonexistent_file_viewports: List[int]
    overall_pass: bool
    evidence_ids: List[str]


class BaseBrowserScreenshotAdapter:
    """Interface for visual screenshot adapters."""

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        raise NotImplementedError


class FakeBrowserScreenshotAdapter(BaseBrowserScreenshotAdapter):
    """Deterministic offline browser screenshot adapter."""

    def __init__(self, simulate_overflow: bool = False, simulate_missing_cta: bool = False, viewports_to_capture: Optional[List[int]] = None):
        self.simulate_overflow = simulate_overflow
        self.simulate_missing_cta = simulate_missing_cta
        self.viewports_to_capture = viewports_to_capture if viewports_to_capture is not None else REQUIRED_VIEWPORTS

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        screenshots = {}
        overflows = {}
        cta_vis = {}
        hashes = {}

        for vp in self.viewports_to_capture:
            path_str = f"/artifacts/screenshots/{run_id}_{vp}px.png"
            screenshots[vp] = path_str
            overflows[vp] = self.simulate_overflow and vp < 768
            cta_vis[vp] = not (self.simulate_missing_cta and vp == 375)
            hashes[vp] = hashlib.sha256(f"{run_id}_{vp}".encode("utf-8")).hexdigest()[:16]

        return VisualEvidenceManifest(
            manifest_id=f"vis-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            viewports_captured=list(self.viewports_to_capture),
            screenshot_paths=screenshots,
            horizontal_overflow_detected=overflows,
            cta_visible=cta_vis,
            capture_mode="FAKE_TEST_ARTIFACT",
            capture_adapter="FakeBrowserScreenshotAdapter",
            file_hashes=hashes,
        )


class VisualQAEvaluator:
    """Evaluates VisualEvidenceManifest against responsive QA rules and disk integrity (Section 9)."""

    def __init__(self, check_file_existence: bool = False):
        self.check_file_existence = check_file_existence

    def evaluate_manifest(self, manifest: VisualEvidenceManifest) -> VisualQAResult:
        missing_viewports = [vp for vp in REQUIRED_VIEWPORTS if vp not in manifest.viewports_captured]
        all_covered = len(missing_viewports) == 0

        nonexistent_files = []
        if self.check_file_existence:
            for vp, p in manifest.screenshot_paths.items():
                if not Path(p).exists():
                    nonexistent_files.append(vp)

        overflows = [vp for vp, has_of in manifest.horizontal_overflow_detected.items() if has_of]
        missing_ctas = [vp for vp, is_vis in manifest.cta_visible.items() if not is_vis]

        has_defects = len(overflows) > 0 or len(missing_ctas) > 0 or len(nonexistent_files) > 0

        # Coverage classification (Section 9):
        # 375 + 1440 only => PARTIAL_COVERAGE
        # all six clean => FULL_PASS
        # any defect or empty => FAIL
        if not all_covered and len(manifest.viewports_captured) > 0 and not has_defects:
            coverage_status = VisualQACoverage.PARTIAL_COVERAGE
            overall_pass = False
        elif all_covered and not has_defects:
            coverage_status = VisualQACoverage.FULL_PASS
            overall_pass = True
        else:
            coverage_status = VisualQACoverage.FAIL
            overall_pass = False

        return VisualQAResult(
            qa_id=f"qa-{uuid.uuid4().hex[:8]}",
            run_id=manifest.run_id,
            coverage_status=coverage_status,
            all_required_viewports_covered=all_covered,
            overflow_detected_viewports=overflows,
            cta_missing_viewports=missing_ctas,
            missing_viewports=missing_viewports,
            nonexistent_file_viewports=nonexistent_files,
            overall_pass=overall_pass,
            evidence_ids=[manifest.manifest_id],
        )

