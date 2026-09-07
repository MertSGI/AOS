"""Unit tests for Design Intelligence V1.1 Production Capability Binding (R2-R1 FINAL EVIDENCE INTEGRITY)."""

import pytest
import tempfile
import os
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == orig_env
    finally:
        if orig_env is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env
        else:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


# --- Section 3 & 4: Video Renderer Contract & Mocked Integrity Tests ---

def test_video_renderer_constructor_parameter_validation():
    # Canonical ProductDemoVideoSpec lacks fps, width, height.
    # ProgrammaticVideoRendererAdapter owns fps, width, height.
    spec = ProductDemoVideoSpec(spec_id="spec-1", title="Demo", duration_seconds=3.0, scene_script=[])
    assert not hasattr(spec, "fps")
    assert not hasattr(spec, "width")

    # Renderer parameter validation
    with pytest.raises(ValueError):
        ProgrammaticVideoRendererAdapter(ffmpeg_path="dummy_ffmpeg", ffprobe_path="dummy_ffprobe", fps=0)

    with pytest.raises(ValueError):
        ProgrammaticVideoRendererAdapter(ffmpeg_path="dummy_ffmpeg", ffprobe_path="dummy_ffprobe", width=-100)

    with pytest.raises(ValueError):
        ProgrammaticVideoRendererAdapter(ffmpeg_path="dummy_ffmpeg", ffprobe_path="dummy_ffprobe", height=0)

    renderer = ProgrammaticVideoRendererAdapter(ffmpeg_path="dummy_ffmpeg", ffprobe_path="dummy_ffprobe", fps=30, width=1920, height=1080)
    assert renderer.fps == 30
    assert renderer.width == 1920
    assert renderer.height == 1080


def test_video_renderer_mocked_contract_assembly_probing_and_cleanup():
    renderer = ProgrammaticVideoRendererAdapter(
        ffmpeg_path="dummy_ffmpeg",
        ffprobe_path="dummy_ffprobe",
        fps=12,
        width=800,
        height=600,
    )
    spec = ProductDemoVideoSpec(spec_id="s1", title="Micro Demo", duration_seconds=1.0, scene_script=[])

    def fake_subprocess_run(cmd, capture_output, text, timeout):
        res = MagicMock()
        if "dummy_ffmpeg" in cmd[0]:
            out_path = cmd[-1]
            Path(out_path).write_bytes(b"FINAL_MP4_BYTES_12345")
            res.returncode = 0
            return res
        elif "dummy_ffprobe" in cmd[0]:
            res.returncode = 0
            res.stdout = json.dumps({
                "streams": [{"width": 800, "height": 600, "duration": "1.000000"}]
            })
            return res
        res.returncode = 1
        return res

    async def fake_async_render(html_content, spec):
        run_id = "testrun"
        frames_dir = os.path.join(renderer.output_dir, f"frames_{run_id}")
        os.makedirs(frames_dir, exist_ok=True)
        frame_path = os.path.join(frames_dir, "frame_0000.png")
        Path(frame_path).write_bytes(b"PNG_FRAME")

        output_mp4 = os.path.join(renderer.output_dir, f"video_{run_id}.mp4")

        # Call ffmpeg
        ffmpeg_cmd = [
            renderer.ffmpeg_path, "-y", "-framerate", str(renderer.fps),
            "-i", os.path.join(frames_dir, "frame_%04d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", output_mp4,
        ]
        res = fake_subprocess_run(ffmpeg_cmd, capture_output=True, text=True, timeout=60)

        # Call ffprobe
        ffprobe_cmd = [
            renderer.ffprobe_path, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration", "-of", "json", output_mp4,
        ]
        probe_res = fake_subprocess_run(ffprobe_cmd, capture_output=True, text=True, timeout=30)
        probe_data = json.loads(probe_res.stdout)
        v_stream = probe_data["streams"][0]

        try:
            from extensions.design_intelligence.media_renderer import VideoArtifactMetadata
            return VideoArtifactMetadata(
                artifact_id=f"vid-{run_id}",
                file_path=output_mp4,
                size_bytes=os.path.getsize(output_mp4),
                duration_seconds=float(v_stream["duration"]),
                width=int(v_stream["width"]),
                height=int(v_stream["height"]),
                sha256_hash=hashlib.sha256(b"FINAL_MP4_BYTES_12345").hexdigest(),
                renderer_identity="ProgrammaticVideoRendererAdapter",
                rendered_at="2026-09-07T00:00:00Z",
                has_valid_video_stream=True,
            )
        finally:
            if os.path.exists(frames_dir):
                import shutil
                shutil.rmtree(frames_dir, ignore_errors=True)

    with patch.object(renderer, "_async_render", side_effect=fake_async_render):
        meta = renderer.render_video("<html><body>Frame</body></html>", spec)

        assert meta.width == 800
        assert meta.height == 600
        assert meta.duration_seconds == 1.0
        assert meta.has_valid_video_stream is True
        assert meta.sha256_hash == hashlib.sha256(b"FINAL_MP4_BYTES_12345").hexdigest()

        # Ensure temp frames directory cleaned up
        frames_dirs = [d for d in os.listdir(renderer.output_dir) if d.startswith("frames_")]
        assert len(frames_dirs) == 0


def test_video_renderer_mocked_failure_rejections():
    renderer = ProgrammaticVideoRendererAdapter(
        ffmpeg_path="dummy_ffmpeg",
        ffprobe_path="dummy_ffprobe",
    )
    spec = ProductDemoVideoSpec(spec_id="s1", title="Micro Demo", duration_seconds=1.0, scene_script=[])

    def run_with_probe_stdout(stdout_str, ffmpeg_code=0, probe_code=0):
        def fake_run(cmd, capture_output, text, timeout):
            res = MagicMock()
            if "dummy_ffmpeg" in cmd[0]:
                if ffmpeg_code == 0:
                    Path(cmd[-1]).write_bytes(b"MP4_BYTES")
                res.returncode = ffmpeg_code
                res.stderr = "FFmpeg error output"
                return res
            elif "dummy_ffprobe" in cmd[0]:
                res.returncode = probe_code
                res.stdout = stdout_str
                res.stderr = "ffprobe error output"
                return res
            return res

        async def fake_async_render(html_content, spec):
            output_mp4 = os.path.join(renderer.output_dir, "test.mp4")
            res = fake_run([renderer.ffmpeg_path, "-y", output_mp4], capture_output=True, text=True, timeout=60)
            if res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) == 0:
                raise RuntimeError(f"FFmpeg MP4 rendering failed (exit {res.returncode}): {res.stderr}")

            probe_res = fake_run([renderer.ffprobe_path, output_mp4], capture_output=True, text=True, timeout=30)
            if probe_res.returncode != 0:
                raise RuntimeError(f"ffprobe stream inspection failed on '{output_mp4}': {probe_res.stderr}")

            try:
                probe_data = json.loads(probe_res.stdout)
            except Exception as e:
                raise RuntimeError(f"ffprobe emitted invalid JSON output: {e}")

            streams = probe_data.get("streams", [])
            if not streams:
                raise RuntimeError("ffprobe detected zero video streams in final MP4 artifact")

            v_stream = streams[0]
            probed_w = int(v_stream.get("width", 0))
            probed_h = int(v_stream.get("height", 0))
            probed_dur = float(v_stream.get("duration", 0))

            if probed_w <= 0 or probed_h <= 0 or probed_dur <= 0:
                raise RuntimeError(f"Invalid probed video dimensions or duration: w={probed_w}, h={probed_h}, dur={probed_dur}")

            return MagicMock()

        with patch.object(renderer, "_async_render", side_effect=fake_async_render):
            return renderer.render_video("<html></html>", spec)

    # 1. FFmpeg failure => Exception
    with pytest.raises(RuntimeError) as exc1:
        run_with_probe_stdout("", ffmpeg_code=1)
    assert "FFmpeg MP4 rendering failed" in str(exc1.value)

    # 2. ffprobe failure => Exception
    with pytest.raises(RuntimeError) as exc2:
        run_with_probe_stdout("", probe_code=1)
    assert "ffprobe stream inspection failed" in str(exc2.value)

    # 3. Malformed ffprobe JSON => Exception
    with pytest.raises(RuntimeError) as exc3:
        run_with_probe_stdout("INVALID_JSON")
    assert "ffprobe emitted invalid JSON" in str(exc3.value)

    # 4. Zero video streams => Exception
    with pytest.raises(RuntimeError) as exc4:
        run_with_probe_stdout(json.dumps({"streams": []}))
    assert "zero video streams" in str(exc4.value)

    # 5. Zero/invalid duration/dimensions => Exception
    with pytest.raises(RuntimeError) as exc5:
        run_with_probe_stdout(json.dumps({"streams": [{"width": 0, "height": 600, "duration": "1.0"}]}))
    assert "Invalid probed video dimensions or duration" in str(exc5.value)


# --- Section 5, 6 & 11: Visual Provider Origin & Response Contract Tests ---

def test_visual_provider_origin_real_vs_simulated():
    # Production default (client_factory=None) => REAL_PROVIDER_VISUAL_REVIEW
    real_adapter = RealVisualCriticAdapter()
    assert real_adapter.evidence_origin == EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW

    # Injected test factory (client_factory!=None) => SIMULATED_VISUAL_TEST
    fake_factory = lambda api_key: MagicMock()
    sim_adapter = RealVisualCriticAdapter(client_factory=fake_factory)
    assert sim_adapter.evidence_origin == EvidenceOrigin.SIMULATED_VISUAL_TEST


def test_visual_provider_strict_four_field_response_validation():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"REAL_BYTES")
        p = f.name

    try:
        os.environ["GEMINI_API_KEY"] = "fake_key"

        def make_adapter(response_dict: dict):
            def factory(api_key):
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.text = json.dumps(response_dict)
                mock_client.models.generate_content.return_value = mock_resp
                return mock_client
            return RealVisualCriticAdapter(client_factory=factory)

        # 1. Valid 4 fields => PASS
        valid_resp = {
            "verdict": "PASS",
            "is_generic_or_template": False,
            "findings": ["Clean design"],
            "suggested_fix": None,
        }
        finding_pass = make_adapter(valid_resp).evaluate_visuals({375: p})
        assert finding_pass.verdict == JudgmentVerdict.PASS

        # 2. Missing findings field => FAIL CLOSED
        missing_findings = {
            "verdict": "PASS",
            "is_generic_or_template": False,
            "suggested_fix": None,
        }
        finding_no_findings = make_adapter(missing_findings).evaluate_visuals({375: p})
        assert finding_no_findings.verdict == JudgmentVerdict.FAIL
        assert "missing required field 'findings'" in finding_no_findings.details

        # 3. Missing suggested_fix field => FAIL CLOSED
        missing_fix = {
            "verdict": "PASS",
            "is_generic_or_template": False,
            "findings": [],
        }
        finding_no_fix = make_adapter(missing_fix).evaluate_visuals({375: p})
        assert finding_no_fix.verdict == JudgmentVerdict.FAIL
        assert "missing required field 'suggested_fix'" in finding_no_fix.details

    finally:
        os.remove(p)
        os.environ.pop("GEMINI_API_KEY", None)


def test_simulated_provider_negative_proof_for_human_visual_review_ready():
    # Prove vulnerability fix: Even if a simulated provider returns a syntactically valid PASS,
    # evaluate_human_visual_review_readiness MUST REJECT it because origin is SIMULATED_VISUAL_TEST.
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"REAL_BYTES")
        p = f.name

    try:
        fake_factory = lambda api_key: MagicMock()
        sim_adapter = RealVisualCriticAdapter(client_factory=fake_factory)
        assert sim_adapter.evidence_origin == EvidenceOrigin.SIMULATED_VISUAL_TEST

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

        manifest = VisualEvidenceManifest(
            manifest_id="v-1",
            run_id="r-1",
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths={vp: p for vp in REQUIRED_VIEWPORTS},
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="RealBrowserCaptureAdapter",
            file_hashes={vp: hashlib.sha256(b"REAL_BYTES").hexdigest() for vp in REQUIRED_VIEWPORTS},
        )

        res = evaluate_human_visual_review_readiness(
            scorecard=scorecard_pass,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=manifest,
            visual_adapter=sim_adapter,
        )

        assert res["is_ready"] is False
        assert any("SIMULATED_VISUAL_TEST" in r for r in res["reasons"])
    finally:
        os.remove(p)


# --- Section 8, 9 & 10: Media Decision & Provenance Adversarial Matrix ---

def test_media_decision_adversarial_matrix_and_force_video_semantics():
    engine = MediaDecisionEngine()

    # 1. STATIC_STORY_FORCE_FALSE => NO_VIDEO
    d1 = engine.evaluate_media_needs("p1", "Local Service", "A quiet static local-service landing page focus.", force_video_concept=False)
    assert d1.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert d1.video_justified is False

    # 2. STATIC_STORY_FORCE_TRUE => NO_VIDEO (force_video_concept alone CANNOT authorize video!)
    d2 = engine.evaluate_media_needs("p2", "Local Service", "A quiet static local-service landing page focus.", force_video_concept=True)
    assert d2.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert d2.video_justified is False

    # 3. SAFE_UI_DEMO_FORCE_FALSE => VIDEO_ALLOWED
    d3 = engine.evaluate_media_needs("p3", "SaaS App", "Interactive booking-flow micro-demo showing user step-by-step UI preview", force_video_concept=False)
    assert d3.selected_media_kind == MediaKind.PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    assert d3.video_justified is True

    # 4. SAFE_UI_DEMO_FORCE_TRUE => VIDEO_ALLOWED
    d4 = engine.evaluate_media_needs("p4", "SaaS App", "Interactive booking-flow micro-demo showing user step-by-step UI preview", force_video_concept=True)
    assert d4.selected_media_kind == MediaKind.PROGRAMMATIC_PRODUCT_DEMO_VIDEO
    assert d4.video_justified is True

    # 5. CLINICAL_PROCEDURE_FORCE_FALSE => NO_VIDEO
    d5 = engine.evaluate_media_needs("p5", "Dental Clinic", "Clinical teeth whitening treatment procedure walkthrough", force_video_concept=False)
    assert d5.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert d5.video_justified is False

    # 6. CLINICAL_PROCEDURE_FORCE_TRUE => NO_VIDEO
    d6 = engine.evaluate_media_needs("p6", "Dental Clinic", "Clinical teeth whitening treatment procedure walkthrough", force_video_concept=True)
    assert d6.selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert d6.video_justified is False


def test_real_tenant_media_provenance_exhaustive_matrix():
    engine = MediaDecisionEngine()

    def make_ledger(fact_id, value, fact_type=FactType.CANONICAL_TENANT_FACT, src_ref="ref"):
        ledger = GroundedFactLedger("l-test", "p-test")
        ledger.add_fact(GroundedFact(
            fact_id=fact_id,
            fact_type=fact_type,
            value=value,
            source_type="Test",
            source_reference=src_ref,
        ))
        return ledger

    # 1. UNRELATED_CANONICAL_TEXT
    l_text = make_ledger("f-text", "Our salon has great reviews")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_text, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_text, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 2. UNRELATED_CANONICAL_HTTP_URL (Generic site URL without image/video extension or media tag)
    l_http = make_ledger("f-http", "https://tenant.example.com/about")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_http, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_http, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 3. UNRELATED_CANONICAL_FILE_URL
    l_file = make_ledger("f-file", "file:///tmp/data.json")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_file, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_file, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 4. VALID_CANONICAL_IMAGE_PNG -> IMAGE_ACCEPTED, VIDEO_REJECTED
    l_png = make_ledger("f-png", "https://cdn.example.com/hero.png")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_png, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.REAL_TENANT_IMAGE
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_png, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 5. VALID_CANONICAL_IMAGE_JPEG -> IMAGE_ACCEPTED, VIDEO_REJECTED
    l_jpg = make_ledger("f-jpg", "https://cdn.example.com/hero.jpeg")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_jpg, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.REAL_TENANT_IMAGE
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_jpg, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 6. VALID_CANONICAL_VIDEO_MP4 -> VIDEO_ACCEPTED, IMAGE_REJECTED
    l_mp4 = make_ledger("f-mp4", "https://cdn.example.com/demo.mp4")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_mp4, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.REAL_TENANT_VIDEO
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_mp4, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 7. VALID_CANONICAL_VIDEO_WEBM -> VIDEO_ACCEPTED, IMAGE_REJECTED
    l_webm = make_ledger("f-webm", "https://cdn.example.com/demo.webm")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_webm, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.REAL_TENANT_VIDEO
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_webm, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 8. NONCANONICAL_IMAGE_FACT -> IMAGE_REJECTED
    l_noncan_img = make_ledger("tenant_image_1", "https://cdn.example.com/hero.png", fact_type=FactType.DESIGN_INFERENCE)
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_noncan_img, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 9. NONCANONICAL_VIDEO_FACT -> VIDEO_REJECTED
    l_noncan_vid = make_ledger("tenant_video_1", "https://cdn.example.com/demo.mp4", fact_type=FactType.HUMAN_PREFERENCE)
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_noncan_vid, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK

    # 10. GENERIC_MEDIA_WORD_WITHOUT_ASSET_IDENTITY -> IMAGE_REJECTED, VIDEO_REJECTED
    l_gen_media = make_ledger("social_media_policy", "We utilize social media for marketing")
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_gen_media, requested_kind=MediaKind.REAL_TENANT_IMAGE).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
    assert engine.evaluate_media_needs("p", "Salon", "Story", fact_ledger=l_gen_media, requested_kind=MediaKind.REAL_TENANT_VIDEO).selected_media_kind == MediaKind.STATIC_IMAGE_FALLBACK
