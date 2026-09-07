"""Unit tests for Design Intelligence V1.1 Production Capability Binding (R20 / Section 20)."""

import pytest
import tempfile
import os
import hashlib
from pathlib import Path

from extensions.design_intelligence.contracts import (
    VisualEvidenceManifest,
    EvidenceOrigin,
    EvidenceModality,
    JudgmentVerdict,
    HumanReviewReadinessState,
    DesignProjectBrief,
    GroundedFactLedger,
    GroundedFact,
    GroundedContentManifest,
    GroundedContentBlock,
    RenderedFactBinding,
    ContentBlockCategory,
    FactType,
)
from extensions.design_intelligence.browser_capture import RealBrowserCaptureAdapter
from extensions.design_intelligence.visual_provider import RealVisualCriticAdapter
from extensions.design_intelligence.media_intelligence import (
    MediaDecisionEngine,
    MediaKind,
    MediaProvenanceKind,
)
from extensions.design_intelligence.media_renderer import (
    ProgrammaticVideoRendererAdapter,
    ProductDemoVideoSpec,
)
from extensions.design_intelligence.visual_qa import (
    VisualQAEvaluator,
    validate_real_artifact_integrity,
    REQUIRED_VIEWPORTS,
)
from extensions.design_intelligence.critics import DesignCriticEnsemble
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence
from extensions.design_intelligence.design_loop import (
    AutonomousDesignLoopPipeline,
    evaluate_human_visual_review_readiness,
)


def test_browser_capture_adapter_contract_and_origin():
    adapter = RealBrowserCaptureAdapter()
    assert hasattr(adapter, "capture_manifest")

    html = "<html><body><h1>Test Page</h1><button class='btn-primary'>CTA</button></body></html>"
    manifest = adapter.capture_manifest(html, "test_run_contract")

    assert manifest.capture_mode == "REAL_LOCAL_BROWSER_SCREENSHOT"
    assert manifest.capture_adapter == "RealBrowserCaptureAdapter"
    assert len(manifest.screenshot_paths) == 6
    assert len(manifest.file_hashes) == 6

    # Verify SHA256 length and file existence
    for vp in REQUIRED_VIEWPORTS:
        assert vp in manifest.screenshot_paths
        p = manifest.screenshot_paths[vp]
        assert os.path.exists(p)
        assert len(manifest.file_hashes[vp]) == 64

    # Verify artifact integrity
    integ = validate_real_artifact_integrity(manifest)
    assert integ["valid"] is True


def test_media_decision_engine_and_no_gratuitous_video_rule():
    engine = MediaDecisionEngine()

    # 1. Static-first story => NO_VIDEO / STATIC_IMAGE_FALLBACK
    decision_static = engine.evaluate_media_needs(
        project_id="p-1",
        business_type="Local Service",
        product_story="A quiet local service landing page focus.",
        force_video_concept=False,
    )
    assert decision_static.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert decision_static.video_justified is False
    assert "NO_VIDEO" in decision_static.justification_reason
    assert decision_static.no_gratuitous_video_rule_pass is True

    # 2. Interactive/Demo story => PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    decision_demo = engine.evaluate_media_needs(
        project_id="p-2",
        business_type="SaaS App",
        product_story="Interactive booking-flow micro-demo showing user step-by-step procedure.",
        force_video_concept=True,
    )
    assert decision_demo.selected_media_kind == MediaKind.PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    assert decision_demo.video_justified is True
    assert decision_demo.provenance_kind == MediaProvenanceKind.PROGRAMMATIC_PRODUCT_MEDIA


def test_video_renderer_adapter_and_artifact_integrity():
    renderer = ProgrammaticVideoRendererAdapter()
    spec = ProductDemoVideoSpec("v-spec-1", "p-video-test", duration_seconds=2, fps=10, width=400, height=300)

    html = "<html><body style='background:#111; color:#fff;'><h1>Video Frame</h1></body></html>"
    meta = renderer.render_video(html, spec)

    assert os.path.exists(meta.file_path)
    assert meta.size_bytes > 0
    assert meta.duration_seconds == 2.0
    assert meta.width == 400
    assert meta.height == 300
    assert len(meta.sha256_hash) == 64
    assert meta.has_valid_video_stream is True


def test_visual_provider_provenance_and_malformed_response_fail_closed():
    # 1. Verified RealVisualCriticAdapter origin
    adapter = RealVisualCriticAdapter()
    assert adapter.evidence_origin == EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW

    # 2. Invalid CLI path => fail closed
    bad_adapter = RealVisualCriticAdapter(cli_path=r"C:\NonExistent\antigravity.exe")
    finding = bad_adapter.evaluate_visuals({375: "dummy.png"})
    assert finding.verdict == JudgmentVerdict.FAIL
    assert finding.evidence_modality == EvidenceModality.PIXEL_VISUAL
    assert "failed or closed" in finding.details


def test_bounded_revision_loop_and_human_ready_integration():
    ref_intel = ReferenceIntelligence()
    src = ref_intel.register_candidate_source(
        url_or_name="https://example-saas.com/metadata",
        purpose="Hero composition analysis",
        strength="Clear visual hierarchy",
        integration_cost="LOW",
        dependency_cost="ZERO",
        license_provenance="MIT",
        supply_chain_risk="LOW",
        generic_design_risk="LOW",
        recommended_use="Sales fold structuring",
        do_not_use_conditions=[],
        observation="Product screenshot is side-by-side with CTA",
    )
    ref_intel.extract_design_signal(src.source_id, "hero_composition", "Side-by-side CTA", "Show real UI preview")

    ledger = GroundedFactLedger("l-rev-1", "p-rev-1")
    ledger.add_fact(GroundedFact("f-rev-1", FactType.CANONICAL_TENANT_FACT, "Melis Studio", "Brief", "ref"))

    content_manifest = GroundedContentManifest(
        manifest_id="m-rev-1",
        project_id="p-rev-1",
        blocks=[
            GroundedContentBlock(
                text="Welcome to Melis Studio",
                semantic_role="hero",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-rev-1"],
                category=ContentBlockCategory.FACTUAL,
                rendered_fact_bindings=[RenderedFactBinding("f-rev-1", "Melis Studio")],
            )
        ],
    )

    ensemble = DesignCriticEnsemble()
    ensemble.visual_adapter = RealVisualCriticAdapter()

    pipeline = AutonomousDesignLoopPipeline(
        ref_intel=ref_intel,
        critic_ensemble=ensemble,
        max_design_review_cycles=3,
    )
    brief = DesignProjectBrief("b-rev-1", "p-rev-1", "Melis Studio", "Beauty", "User", "Job", "Posture", fact_ledger=ledger)

    # Run pipeline with full evidence
    browser_adapter = RealBrowserCaptureAdapter()
    evidence_manifest = browser_adapter.capture_manifest("<h1>Welcome to Melis Studio</h1><button class='btn-primary'>CTA</button>", "run_pipeline_test")

    res = pipeline.run_pipeline(
        brief=brief,
        initial_html="<h1>Welcome to Melis Studio</h1><button class='btn-primary'>CTA</button>",
        initial_css="",
        evidence_manifest=evidence_manifest,
        content_manifest=content_manifest,
        concept_id="HERO_V1",
    )

    assert res.cycles_completed <= 3
    assert res.pipeline_id.startswith("loop-")
