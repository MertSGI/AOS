"""Unit tests for Design Intelligence V1.1 Real-World Grounding & Visual Judgment (R19 / V1.1 / Correction R1)."""

import pytest
import tempfile
import hashlib
from pathlib import Path

from extensions.design_intelligence.contracts import (
    GroundedFactLedger,
    GroundedFact,
    GroundedContentBlock,
    GroundedContentManifest,
    ContentBlockCategory,
    FactType,
    JudgmentVerdict,
    EvidenceModality,
    VisualQACoverage,
    HumanReviewReadinessState,
    VisualEvidenceManifest,
    ReferenceSource,
    DesignProjectBrief,
)
from extensions.design_intelligence.critics import (
    GroundingIntegrityCritic,
    AntiGenericDesignCritic,
    FakeVisualCriticAdapter,
    VisualCriticAdapter,
    DesignCriticEnsemble,
    STATIC_CRITIC_MAY_GRANT_PIXEL_VISUAL_PASS,
)
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence
from extensions.design_intelligence.visual_qa import (
    VisualQAEvaluator,
    FakeBrowserScreenshotAdapter,
    validate_real_artifact_integrity,
    REQUIRED_VIEWPORTS,
)
from extensions.design_intelligence.design_loop import (
    AutonomousDesignLoopPipeline,
    evaluate_human_visual_review_readiness,
)


def test_generic_ledger_driven_grounding_and_manifest_validation():
    # Section 5: Grounding works without knowing invented words in advance
    ledger = GroundedFactLedger(ledger_id="led-gen", project_id="p-gen")
    ledger.add_fact(GroundedFact(
        fact_id="f-name",
        fact_type=FactType.CANONICAL_TENANT_FACT,
        value="Example Nail Studio",
        source_type="CanonicalTenantRecord",
        source_reference="tenant.name",
    ))
    ledger.add_fact(GroundedFact(
        fact_id="f-loc",
        fact_type=FactType.CANONICAL_TENANT_FACT,
        value="Istanbul",
        source_type="CanonicalTenantRecord",
        source_reference="tenant.location",
    ))

    critic = GroundingIntegrityCritic()

    # Valid supported block => PASS
    valid_manifest = GroundedContentManifest(
        manifest_id="m-valid",
        project_id="p-gen",
        blocks=[
            GroundedContentBlock(
                text="Example Nail Studio - Istanbul",
                semantic_role="hero_title",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-name", "f-loc"],
                category=ContentBlockCategory.FACTUAL,
                is_customer_facing=True,
            )
        ],
    )
    res_valid = critic.evaluate("", "", fact_ledger=ledger, content_manifest=valid_manifest)
    assert res_valid.verdict == JudgmentVerdict.PASS

    # Arbitrary unsupported Set 1: Kadıköy Flagship Beauty Lab, Laser Epilation
    bad_manifest_1 = GroundedContentManifest(
        manifest_id="m-bad-1",
        project_id="p-gen",
        blocks=[
            GroundedContentBlock(
                text="Kadıköy Flagship Beauty Lab - Laser Epilation",
                semantic_role="hero_title",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-unknown-999"],  # Non-existent fact ID
                category=ContentBlockCategory.FACTUAL,
                is_customer_facing=True,
            )
        ],
    )
    res_bad_1 = critic.evaluate("", "", fact_ledger=ledger, content_manifest=bad_manifest_1)
    assert res_bad_1.verdict == JudgmentVerdict.FAIL
    assert "non-existent fact ID" in res_bad_1.details

    # Arbitrary unsupported Set 2: Senior Colorist, Wedding Makeup, Ankara Branch
    bad_manifest_2 = GroundedContentManifest(
        manifest_id="m-bad-2",
        project_id="p-gen",
        blocks=[
            GroundedContentBlock(
                text="Senior Colorist Wedding Makeup Ankara Branch",
                semantic_role="services_list",
                provenance_kind=FactType.DESIGN_INFERENCE,  # Non-canonical fact type for factual copy
                source_fact_ids=["f-name"],
                category=ContentBlockCategory.FACTUAL,
                is_customer_facing=True,
            )
        ],
    )
    res_bad_2 = critic.evaluate("", "", fact_ledger=ledger, content_manifest=bad_manifest_2)
    assert res_bad_2.verdict == JudgmentVerdict.FAIL
    assert "non-canonical" in res_bad_2.details.lower()


    # Unmanifested customer facing text detected => FAIL (Section 4)
    incomplete_manifest = GroundedContentManifest(
        manifest_id="m-incomp",
        project_id="p-gen",
        blocks=[],
        unmanifested_customer_facing_text_detected=True,
    )
    res_incomp = critic.evaluate("", "", fact_ledger=ledger, content_manifest=incomplete_manifest)
    assert res_incomp.verdict == JudgmentVerdict.FAIL
    assert "INCOMPLETE GroundedContentManifest" in res_incomp.details


def test_reference_provenance_structural_validation():
    ref_intel = ReferenceIntelligence()
    assert ref_intel.REFERENCE_SOURCE_PLACEHOLDER_REJECTED == "YES"

    # Blank purpose rejection
    with pytest.raises(ValueError):
        ref_intel.register_candidate_source(
            url_or_name="https://stripe.com/design",
            purpose="",  # Blank purpose
            strength="High",
            integration_cost="LOW",
            dependency_cost="ZERO",
            license_provenance="MIT",
            supply_chain_risk="LOW",
            generic_design_risk="LOW",
            recommended_use="UI inspiration",
            observation="Observed layout",
        )

    # Valid named-source identity
    src = ref_intel.register_candidate_source(
        url_or_name="Stripe Design System 2026",
        purpose="Layout and micro-animation guidance",
        strength="High brand focus",
        integration_cost="LOW",
        dependency_cost="ZERO",
        license_provenance="Public Design Guidelines",
        supply_chain_risk="LOW",
        generic_design_risk="LOW",
        recommended_use="Sales fold alignment",
        observation="Clean hierarchy with dominant CTA",
    )
    assert src.source_id.startswith("src-")

    # Extracting signal from invalid source fails
    invalid_src = ReferenceSource(
        source_id="src-bad",
        url_or_name="REF-001 Editorial Luxury",  # Invalid identity
        purpose="None",
        strength="None",
        integration_cost="HIGH",
        dependency_cost="HIGH",
        license_provenance="None",
        supply_chain_risk="HIGH",
        generic_design_risk="HIGH",
        recommended_use="None",
        observation="None",
    )
    ref_intel._sources["src-bad"] = invalid_src
    with pytest.raises(ValueError):
        ref_intel.extract_design_signal("src-bad", "hero", "obs", "principle")


def test_fake_visual_adapter_and_fake_manifest_cannot_grant_human_ready():
    # Section 9: FakeVisualCriticAdapter cannot grant human-ready
    assert DesignCriticEnsemble.FAKE_VISUAL_EVIDENCE_CAN_GRANT_HUMAN_READY == "NO"

    fake_adapter = FakeBrowserScreenshotAdapter()
    manifest = fake_adapter.capture_manifest("http://localhost:3000", "run-fake")
    assert manifest.capture_mode == "FAKE_TEST_ARTIFACT"

    scorecard = DesignCriticEnsemble(visual_adapter=FakeVisualCriticAdapter()).evaluate_project("p-fake", "<h1>Title</h1>", "")
    
    gate_res = evaluate_human_visual_review_readiness(
        scorecard=scorecard,
        fact_ledger=GroundedFactLedger(ledger_id="led-1", project_id="p-fake"),
        content_manifest=GroundedContentManifest(manifest_id="man-1", project_id="p-fake", blocks=[GroundedContentBlock("Title", "h1", FactType.CANONICAL_TENANT_FACT, ["f-1"])]),
        ref_intel=ReferenceIntelligence(),
        evidence_manifest=manifest,
        visual_adapter=FakeVisualCriticAdapter(),
    )

    assert gate_res["is_ready"] is False
    assert gate_res["state"] == HumanReviewReadinessState.DESIGN_DISCOVERY_COMPLETE
    assert any("FakeVisualCriticAdapter used" in r for r in gate_res["reasons"])
    assert any("Capture origin 'FAKE_TEST_ARTIFACT' is not REAL_LOCAL_BROWSER_SCREENSHOT" in r for r in gate_res["reasons"])


def test_real_artifact_integrity_and_human_ready_gate():
    # Create temp files on disk to test real artifact integrity
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = {}
        hashes = {}

        for vp in REQUIRED_VIEWPORTS:
            file_path = Path(tmpdir) / f"screenshot_{vp}.png"
            content = f"fake_image_bytes_{vp}".encode("utf-8")
            file_path.write_bytes(content)
            paths[vp] = str(file_path)
            hashes[vp] = hashlib.sha256(content).hexdigest()

        real_manifest = VisualEvidenceManifest(
            manifest_id="vis-real-001",
            run_id="run-real-001",
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths=paths,
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="PlaywrightRealBrowserScreenshotAdapter",
            file_hashes=hashes,
        )

        # 1. Valid real artifacts pass integrity
        integ_valid = validate_real_artifact_integrity(real_manifest)
        assert integ_valid["valid"] is True

        # 2. Missing screenshot file => FAIL
        paths_missing = dict(paths)
        paths_missing[375] = str(Path(tmpdir) / "nonexistent.png")
        manifest_missing = VisualEvidenceManifest(
            manifest_id="vis-real-002",
            run_id="run-real-002",
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths=paths_missing,
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="PlaywrightRealBrowserScreenshotAdapter",
            file_hashes=hashes,
        )
        integ_missing = validate_real_artifact_integrity(manifest_missing)
        assert integ_missing["valid"] is False
        assert any("does not exist" in err for err in integ_missing["errors"])

        # 3. Hash mismatch => FAIL
        hashes_bad = dict(hashes)
        hashes_bad[1440] = "0000000000000000000000000000000000000000000000000000000000000000"
        manifest_bad_hash = VisualEvidenceManifest(
            manifest_id="vis-real-003",
            run_id="run-real-003",
            viewports_captured=REQUIRED_VIEWPORTS,
            screenshot_paths=paths,
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="PlaywrightRealBrowserScreenshotAdapter",
            file_hashes=hashes_bad,
        )
        integ_bad_hash = validate_real_artifact_integrity(manifest_bad_hash)
        assert integ_bad_hash["valid"] is False
        assert any("SHA256 mismatch" in err for err in integ_bad_hash["errors"])

        # 4. Injected real visual adapter double => HUMAN_VISUAL_REVIEW_READY = TRUE
        from extensions.design_intelligence.contracts import CritiqueFinding

        class RealVisualCriticAdapterDouble(VisualCriticAdapter):
            def evaluate_visuals(self, screenshot_paths, dna=None, story=None, fact_ledger=None, negative_preferences=None):
                return CritiqueFinding(
                    finding_id="f-real-vis",
                    critic_name="RealVisualCriticAdapterDouble",
                    verdict=JudgmentVerdict.PASS,
                    dimension="pixel_visual_quality",
                    title="Real Visual Review",
                    details="Verified real visual layout",
                    evidence_modality=EvidenceModality.PIXEL_VISUAL,
                )

        ledger = GroundedFactLedger(ledger_id="led-r", project_id="p-real")
        ledger.add_fact(GroundedFact("f-r1", FactType.CANONICAL_TENANT_FACT, "Real Studio", "Catalog", "name"))

        content_manifest = GroundedContentManifest(
            manifest_id="m-r",
            project_id="p-real",
            blocks=[GroundedContentBlock("Real Studio", "h1", FactType.CANONICAL_TENANT_FACT, ["f-r1"], ContentBlockCategory.FACTUAL)],
        )

        scorecard = DesignCriticEnsemble(visual_adapter=RealVisualCriticAdapterDouble()).evaluate_project(
            project_id="p-real",
            html_content="<h1>Real Studio</h1><button class='btn btn-primary'>Randevu Al</button>",
            css_content="",
            evidence_manifest=real_manifest,
            fact_ledger=ledger,
            content_manifest=content_manifest,
        )

        gate_res = evaluate_human_visual_review_readiness(
            scorecard=scorecard,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ReferenceIntelligence(),
            evidence_manifest=real_manifest,
            visual_adapter=RealVisualCriticAdapterDouble(),
        )

        if not gate_res["is_ready"]:
            print(f"Debug reasons: {gate_res['reasons']}")
        assert gate_res["reasons"] == []
        assert gate_res["is_ready"] is True
        assert gate_res["state"] == HumanReviewReadinessState.HUMAN_VISUAL_REVIEW_READY


def test_no_forced_recommendation_and_no_default_winner():
    pipeline = AutonomousDesignLoopPipeline()
    assert pipeline.NO_DEFAULT_CONCEPT_WINNER == "YES"
    assert pipeline.NO_FORCED_LEAST_BAD_RECOMMENDATION == "YES"
    assert pipeline.AUTOMATED_HUMAN_ACCEPTED_TRANSITION_COUNT == 0

    brief = DesignProjectBrief(
        brief_id="b-1",
        project_id="p-weak",
        tenant_name="Weak Salon",
        industry="Beauty",
        target_audience="Clients",
        core_job_to_be_done="Booking",
        brand_posture="Generic",
    )

    # Initial HTML leaking internal build causing FAIL
    res = pipeline.run_pipeline(brief, "<h1>Weak Salon (v2 pilot)</h1>", "")
    assert res.overall_verdict == JudgmentVerdict.FAIL
    assert res.recommendation.recommended_concept == "NONE"
