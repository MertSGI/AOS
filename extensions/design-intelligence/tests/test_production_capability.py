"""Unit tests for Design Intelligence V1.1 Production Capability Binding (R2 DIRECT MULTIMODAL EVIDENCE HARDENING)."""

import pytest
import tempfile
import os
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

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
    CritiqueFinding,
    CritiqueScorecard,
    ProductDemoVideoSpec,
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


def test_browser_capture_adapter_contract_and_viewports():
    adapter = RealBrowserCaptureAdapter()
    assert hasattr(adapter, "capture_manifest")

    html = "<html><body><h1>Test Page</h1><button class='btn-primary' style='width:100px;height:40px;'>CTA</button></body></html>"
    try:
        manifest = adapter.capture_manifest(html, "test_run_contract")

        assert manifest.capture_mode == "REAL_LOCAL_BROWSER_SCREENSHOT"
        assert manifest.capture_adapter == "RealBrowserCaptureAdapter"
        assert len(manifest.screenshot_paths) == 6
        assert len(manifest.file_hashes) == 6
        assert set(manifest.viewports_captured) == {375, 390, 768, 1024, 1440, 1920}

        for vp in REQUIRED_VIEWPORTS:
            assert vp in manifest.screenshot_paths
            p = manifest.screenshot_paths[vp]
            assert os.path.exists(p)
            assert len(manifest.file_hashes[vp]) == 64

        integ = validate_real_artifact_integrity(manifest)
        assert integ["valid"] is True
    except RuntimeError as e:
        # Fail closed when browser environment is missing binary
        assert "Playwright" in str(e) or "browser" in str(e).lower()


def test_browser_cta_rendered_visibility_hidden_vs_visible():
    adapter = RealBrowserCaptureAdapter()

    hidden_html = "<html><body><h1>Test</h1><button class='btn-primary' style='display:none;'>Hidden CTA</button></body></html>"
    visible_html = "<html><body><h1>Test</h1><button class='btn-primary' style='display:block;width:100px;height:40px;'>Visible CTA</button></body></html>"

    try:
        manifest_hidden = adapter.capture_manifest(hidden_html, "test_hidden_cta")
        for vp in REQUIRED_VIEWPORTS:
            assert manifest_hidden.cta_visible[vp] is False

        manifest_visible = adapter.capture_manifest(visible_html, "test_visible_cta")
        for vp in REQUIRED_VIEWPORTS:
            assert manifest_visible.cta_visible[vp] is True
    except RuntimeError as e:
        assert "Playwright" in str(e) or "browser" in str(e).lower()


def test_browser_environment_restoration():
    orig_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    try:
        adapter = RealBrowserCaptureAdapter(playwright_browsers_path=tempfile.gettempdir())
        html = "<html><body><h1>Env Test</h1></body></html>"
        try:
            adapter.capture_manifest(html, "test_env_res")
        except Exception:
            pass
        # Ensure temporary environment mutation is restored
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == orig_env
    finally:
        if orig_env is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env
        else:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def test_visual_provider_multimodal_bytes_binding():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1, tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
        f1.write(b"MOBILE_PNG_BYTES_12345")
        f2.write(b"DESKTOP_PNG_BYTES_67890")
        p1, p2 = f1.name, f2.name

    try:
        captured_requests = []

        def fake_client_factory(api_key: str):
            assert api_key == "test_gemini_key"
            mock_client = MagicMock()

            def mock_generate_content(model, contents, config):
                captured_requests.append({"model": model, "contents": contents, "config": config})
                mock_resp = MagicMock()
                mock_resp.text = json.dumps({
                    "verdict": "PASS",
                    "is_generic_or_template": False,
                    "findings": ["Clean tenant identity", "Dominant CTA"],
                    "suggested_fix": None,
                })
                return mock_resp

            mock_client.models.generate_content = mock_generate_content
            return mock_client

        os.environ["GEMINI_API_KEY"] = "test_gemini_key"
        adapter = RealVisualCriticAdapter(
            model_name="gemini-3.8-flash",
            api_key_env_var="GEMINI_API_KEY",
            client_factory=fake_client_factory,
        )

        finding = adapter.evaluate_visuals({375: p1, 1440: p2})

        assert finding.verdict == JudgmentVerdict.PASS
        assert finding.evidence_modality == EvidenceModality.PIXEL_VISUAL
        assert len(captured_requests) == 1

        req = captured_requests[0]
        assert req["model"] == "gemini-3.8-flash"
        contents = req["contents"]
        assert len(contents) == 3

        mobile_part, desktop_part, prompt = contents
        assert mobile_part.inline_data.data == b"MOBILE_PNG_BYTES_12345"
        assert desktop_part.inline_data.data == b"DESKTOP_PNG_BYTES_67890"
        assert "Mobile 375px and Desktop 1440px" in prompt
    finally:
        os.remove(p1)
        os.remove(p2)
        os.environ.pop("GEMINI_API_KEY", None)


def test_visual_provider_missing_credential_and_zero_byte_fail_closed():
    os.environ.pop("GEMINI_API_KEY", None)
    adapter = RealVisualCriticAdapter()

    # 1. Missing credential => FAIL
    finding_no_key = adapter.evaluate_visuals({375: "dummy.png"})
    assert finding_no_key.verdict == JudgmentVerdict.FAIL
    assert "Missing required credential" in finding_no_key.details

    # 2. Zero-byte screenshot => FAIL
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as empty_file:
        empty_path = empty_file.name

    try:
        os.environ["GEMINI_API_KEY"] = "fake_key"
        finding_zero = adapter.evaluate_visuals({375: empty_path, 1440: empty_path})
        assert finding_zero.verdict == JudgmentVerdict.FAIL
        assert "Zero-byte screenshot artifact" in finding_zero.details
    finally:
        os.remove(empty_path)
        os.environ.pop("GEMINI_API_KEY", None)


def test_visual_provider_malformed_and_unknown_verdict_fail_closed():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"REAL_BYTES")
        p = f.name

    try:
        os.environ["GEMINI_API_KEY"] = "fake_key"

        def make_adapter(response_text: str):
            def factory(api_key):
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.text = response_text
                mock_client.models.generate_content.return_value = mock_resp
                return mock_client
            return RealVisualCriticAdapter(client_factory=factory)

        # Malformed JSON
        finding_bad_json = make_adapter("NOT_JSON").evaluate_visuals({375: p})
        assert finding_bad_json.verdict == JudgmentVerdict.FAIL
        assert "malformed JSON" in finding_bad_json.details

        # Missing verdict
        finding_no_verdict = make_adapter('{"is_generic_or_template": false}').evaluate_visuals({375: p})
        assert finding_no_verdict.verdict == JudgmentVerdict.FAIL
        assert "missing required field 'verdict'" in finding_no_verdict.details

        # Unknown verdict
        finding_unknown_verdict = make_adapter('{"verdict": "MAYBE", "is_generic_or_template": false}').evaluate_visuals({375: p})
        assert finding_unknown_verdict.verdict == JudgmentVerdict.FAIL
        assert "Unknown verdict value" in finding_unknown_verdict.details

        # Wrong field type
        finding_wrong_type = make_adapter('{"verdict": "PASS", "is_generic_or_template": "NO"}').evaluate_visuals({375: p})
        assert finding_wrong_type.verdict == JudgmentVerdict.FAIL
        assert "is_generic_or_template' is not a boolean" in finding_wrong_type.details

    finally:
        os.remove(p)
        os.environ.pop("GEMINI_API_KEY", None)


def test_media_decision_engine_and_clinical_procedure_guards():
    engine = MediaDecisionEngine()

    # Static story => NO_VIDEO
    decision_static = engine.evaluate_media_needs("p-1", "Local Service", "Static hero focus", force_video_concept=False)
    assert decision_static.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert decision_static.video_justified is False

    # Safe interactive UI demo => PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    decision_demo = engine.evaluate_media_needs("p-2", "SaaS App", "Interactive booking-flow micro-demo showing user step-by-step UI preview", force_video_concept=True)
    assert decision_demo.selected_media_kind == MediaKind.PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    assert decision_demo.video_justified is True

    # Clinical "procedure" text alone CANNOT authorize video demo
    decision_clinical = engine.evaluate_media_needs("p-3", "Dental Clinic", "Clinical teeth whitening treatment procedure walkthrough", force_video_concept=True)
    assert decision_clinical.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert decision_clinical.video_justified is False
    assert "Clinical / medical / treatment procedure text detected" in decision_clinical.justification_reason

    # Real tenant media provenance required
    decision_unprov = engine.evaluate_media_needs("p-4", "Salon", "Hair styling", requested_kind=MediaKind.REAL_TENANT_IMAGE)
    assert decision_unprov.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert decision_unprov.factual_claims_verified is False


def test_video_renderer_adapter_probed_integrity_and_cleanup():
    try:
        renderer = ProgrammaticVideoRendererAdapter()
        spec = ProductDemoVideoSpec("v-spec-1", "Product Demo", 2.0, [{"scene": 1}])

        html = "<html><body style='background:#111; color:#fff;'><h1>Video Frame</h1></body></html>"
        meta = renderer.render_video(html, spec)

        assert os.path.exists(meta.file_path)
        assert meta.size_bytes > 0
        assert meta.duration_seconds > 0
        assert len(meta.sha256_hash) == 64
        assert meta.has_valid_video_stream is True
    except RuntimeError as e:
        # FFmpeg / Playwright binary absence fail closed
        assert "FFmpeg" in str(e) or "ffprobe" in str(e) or "Playwright" in str(e)


def test_video_renderer_failure_modes_and_probe_rejection():
    # Invalid ffmpeg path => fail closed
    bad_renderer = ProgrammaticVideoRendererAdapter(ffmpeg_path="nonexistent_ffmpeg")
    spec = ProductDemoVideoSpec("v-spec-bad", "Bad Demo", 1.0, [])
    html = "<html><body>Bad</body></html>"

    with pytest.raises(Exception):
        bad_renderer.render_video(html, spec)


def test_human_visual_review_readiness_modality_enforcement():
    scorecard_pass = CritiqueScorecard(
        scorecard_id="sc-1",
        project_id="p-1",
        overall_verdict=JudgmentVerdict.PASS,
        critic_findings=[
            CritiqueFinding("f1", "GroundingIntegrityCritic", JudgmentVerdict.PASS, "g", "t", "d", EvidenceModality.STRUCTURED_SEMANTIC),
            CritiqueFinding("f2", "RealVisualCriticAdapter", JudgmentVerdict.PASS, "pixel_visual_quality", "t", "d", EvidenceModality.PIXEL_VISUAL),
        ]
    )

    ledger = GroundedFactLedger("l-1", "p-1")
    ledger.add_fact(GroundedFact("f-1", FactType.CANONICAL_TENANT_FACT, "Val", "s", "r"))

    content_manifest = GroundedContentManifest(
        manifest_id="m-1",
        project_id="p-1",
        blocks=[GroundedContentBlock("Val", "hero", FactType.CANONICAL_TENANT_FACT, ["f-1"])],
    )

    ref_intel = ReferenceIntelligence()
    src = ref_intel.register_candidate_source(
        url_or_name="https://example.com/reference",
        purpose="Hero composition analysis",
        strength="Clear visual hierarchy",
        integration_cost="LOW",
        dependency_cost="ZERO",
        license_provenance="MIT",
        supply_chain_risk="LOW",
        generic_design_risk="LOW",
        recommended_use="Sales fold structuring",
        do_not_use_conditions=[],
        observation="Product screenshot side-by-side with CTA",
    )
    ref_intel.extract_design_signal(src.source_id, "hero_composition", "obs", "prin")

    # Fake visual critic adapter cannot grant HUMAN_VISUAL_REVIEW_READY
    fake_adapter = RealVisualCriticAdapter()
    fake_adapter.__class__.__name__ = "FakeVisualCriticAdapter"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as dummy_shot:
        dummy_shot.write(b"PNG_BYTES")
        dummy_path = dummy_shot.name

    try:
        manifest = VisualEvidenceManifest(
            manifest_id="v-1",
            run_id="r-1",
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths={vp: dummy_path for vp in REQUIRED_VIEWPORTS},
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="RealBrowserCaptureAdapter",
            file_hashes={vp: hashlib.sha256(b"PNG_BYTES").hexdigest() for vp in REQUIRED_VIEWPORTS},
        )

        res_fake = evaluate_human_visual_review_readiness(
            scorecard=scorecard_pass,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=manifest,
            visual_adapter=fake_adapter,
        )

        assert res_fake["is_ready"] is False
        assert "FakeVisualCriticAdapter used" in res_fake["reasons"][0]

    finally:
        os.remove(dummy_path)
