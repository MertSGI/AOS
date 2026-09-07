"""Unit tests for Design Intelligence V1.1 Real-World Grounding & Visual Judgment (R19 / V1.1 / Correction R2)."""

import pytest
import tempfile
import hashlib
import datetime
from pathlib import Path

from extensions.design_intelligence.contracts import (
    GroundedFactLedger,
    GroundedFact,
    GroundedContentBlock,
    GroundedContentManifest,
    RenderedFactBinding,
    ContentBlockCategory,
    FactType,
    JudgmentVerdict,
    EvidenceModality,
    VisualQACoverage,
    HumanReviewReadinessState,
    VisualEvidenceManifest,
    EvidenceOrigin,
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


def test_fact_render_binding_and_unsupported_rendered_text():
    # Section 4 (Correction R2): Valid fact ID alone MUST NOT authorize arbitrary text
    ledger = GroundedFactLedger(ledger_id="led-1", project_id="p-1")
    ledger.add_fact(GroundedFact(
        fact_id="f-name",
        fact_type=FactType.CANONICAL_TENANT_FACT,
        value="Example Nail Studio",
        source_type="Catalog",
        source_reference="name",
    ))

    critic = GroundingIntegrityCritic()

    # 1. Valid canonical fact ID + contradictory text => FAIL
    bad_manifest_1 = GroundedContentManifest(
        manifest_id="m-bad-1",
        project_id="p-1",
        blocks=[
            GroundedContentBlock(
                text="Award Winning Laser Clinic in Ankara",
                semantic_role="hero_title",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-name"],  # Valid ID, but text contradicts!
                category=ContentBlockCategory.FACTUAL,
            )
        ],
    )
    res_bad_1 = critic.evaluate("", "", fact_ledger=ledger, content_manifest=bad_manifest_1)
    assert res_bad_1.verdict == JudgmentVerdict.FAIL
    assert "does not contain canonical fact value" in res_bad_1.details

    # 2. Valid canonical fact ID + second unrelated text => FAIL
    bad_manifest_2 = GroundedContentManifest(
        manifest_id="m-bad-2",
        project_id="p-1",
        blocks=[
            GroundedContentBlock(
                text="Kadıköy Flagship Studio",
                semantic_role="location",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-name"],
                category=ContentBlockCategory.FACTUAL,
            )
        ],
    )
    res_bad_2 = critic.evaluate("", "", fact_ledger=ledger, content_manifest=bad_manifest_2)
    assert res_bad_2.verdict == JudgmentVerdict.FAIL

    # 3. Explicit RenderedFactBinding with contradicting value => FAIL
    bad_manifest_3 = GroundedContentManifest(
        manifest_id="m-bad-3",
        project_id="p-1",
        blocks=[
            GroundedContentBlock(
                text="Example Laser Clinic",
                semantic_role="hero",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-name"],
                category=ContentBlockCategory.FACTUAL,
                rendered_fact_bindings=[
                    RenderedFactBinding(fact_id="f-name", rendered_value="Example Laser Clinic")
                ],
            )
        ],
    )
    res_bad_3 = critic.evaluate("", "", fact_ledger=ledger, content_manifest=bad_manifest_3)
    assert res_bad_3.verdict == JudgmentVerdict.FAIL
    assert "contradicts canonical fact value" in res_bad_3.details

    # 4. Correct exact canonical rendering => PASS
    good_manifest = GroundedContentManifest(
        manifest_id="m-good",
        project_id="p-1",
        blocks=[
            GroundedContentBlock(
                text="Welcome to Example Nail Studio",
                semantic_role="hero",
                provenance_kind=FactType.CANONICAL_TENANT_FACT,
                source_fact_ids=["f-name"],
                category=ContentBlockCategory.FACTUAL,
                rendered_fact_bindings=[
                    RenderedFactBinding(fact_id="f-name", rendered_value="Example Nail Studio")
                ],
            )
        ],
    )
    res_good = critic.evaluate("", "", fact_ledger=ledger, content_manifest=good_manifest)
    assert res_good.verdict == JudgmentVerdict.PASS


def test_mandatory_content_manifest_and_reference_evidence_for_human_ready():
    scorecard = DesignCriticEnsemble().evaluate_project("p-1", "<h1>Test</h1><button class='btn btn-primary'>CTA</button>", "")
    ledger = GroundedFactLedger(ledger_id="l-1", project_id="p-1")
    ledger.add_fact(GroundedFact("f-1", FactType.CANONICAL_TENANT_FACT, "Test", "Cat", "ref"))

    # Section 2: content_manifest is None => HUMAN_READY NO
    gate_no_manifest = evaluate_human_visual_review_readiness(
        scorecard=scorecard,
        fact_ledger=ledger,
        content_manifest=None,
        ref_intel=ReferenceIntelligence(),
        evidence_manifest=None,
        visual_adapter=None,
    )
    assert gate_no_manifest["is_ready"] is False
    assert any("GROUNDING_CONTENT_MANIFEST_MISSING" in r for r in gate_no_manifest["reasons"])

    # Section 3: ref_intel is None => HUMAN_READY NO
    gate_no_ref = evaluate_human_visual_review_readiness(
        scorecard=scorecard,
        fact_ledger=ledger,
        content_manifest=GroundedContentManifest("m-1", "p-1", [GroundedContentBlock("Test", "h1", FactType.CANONICAL_TENANT_FACT, ["f-1"])]),
        ref_intel=None,
        evidence_manifest=None,
        visual_adapter=None,
    )
    assert gate_no_ref["is_ready"] is False
    assert any("REFERENCE_PROVENANCE_EVIDENCE_MISSING" in r for r in gate_no_ref["reasons"])

    # Section 3: Zero reference signals => HUMAN_READY NO
    ref_empty = ReferenceIntelligence()
    gate_zero_signals = evaluate_human_visual_review_readiness(
        scorecard=scorecard,
        fact_ledger=ledger,
        content_manifest=GroundedContentManifest("m-1", "p-1", [GroundedContentBlock("Test", "h1", FactType.CANONICAL_TENANT_FACT, ["f-1"])]),
        ref_intel=ref_empty,
        evidence_manifest=None,
        visual_adapter=None,
    )
    assert gate_zero_signals["is_ready"] is False
    assert any("REFERENCE_PROVENANCE_EVIDENCE_MISSING: Zero evidence-eligible" in r for r in gate_zero_signals["reasons"])


def test_full_sha256_timestamp_and_capture_adapter_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "test.png"
        content = b"test_screenshot_data_12345"
        file_path.write_bytes(content)
        correct_hash = hashlib.sha256(content).hexdigest()  # 64 chars

        paths = {vp: str(file_path) for vp in REQUIRED_VIEWPORTS}

        # 1. 16-char prefix => FAIL (Section 5)
        hashes_16 = {vp: correct_hash[:16] for vp in REQUIRED_VIEWPORTS}
        m_16 = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="PlaywrightAdapter", file_hashes=hashes_16)
        res_16 = validate_real_artifact_integrity(m_16)
        assert res_16["valid"] is False
        assert any("expected EXACT 64 full SHA256" in e for e in res_16["errors"])

        # 2. 63-char hash => FAIL (Section 5)
        hashes_63 = {vp: correct_hash[:63] for vp in REQUIRED_VIEWPORTS}
        m_63 = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="PlaywrightAdapter", file_hashes=hashes_63)
        res_63 = validate_real_artifact_integrity(m_63)
        assert res_63["valid"] is False

        # 3. Wrong 64-char hash => FAIL (Section 5)
        hashes_wrong = {vp: "a" * 64 for vp in REQUIRED_VIEWPORTS}
        m_wrong = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="PlaywrightAdapter", file_hashes=hashes_wrong)
        res_wrong = validate_real_artifact_integrity(m_wrong)
        assert res_wrong["valid"] is False
        assert any("SHA256 mismatch" in e for e in res_wrong["errors"])

        # 4. Correct 64-char hash + valid ISO offset-aware timestamp => PASS
        hashes_64 = {vp: correct_hash for vp in REQUIRED_VIEWPORTS}
        m_valid = VisualEvidenceManifest(
            "v1", "r1", REQUIRED_VIEWPORTS, paths,
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="PlaywrightAdapter",
            file_hashes=hashes_64,
            captured_at="2026-09-07T14:00:00+03:00",
        )
        res_valid = validate_real_artifact_integrity(m_valid)
        assert res_valid["valid"] is True

        # 5. Invalid captured_at => FAIL (Section 6)
        m_invalid_ts = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="PlaywrightAdapter", file_hashes=hashes_64, captured_at="not-a-timestamp")
        res_invalid_ts = validate_real_artifact_integrity(m_invalid_ts)
        assert res_invalid_ts["valid"] is False

        # 6. Naive timestamp without offset => FAIL (Section 6)
        m_naive_ts = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="PlaywrightAdapter", file_hashes=hashes_64, captured_at="2026-09-07T14:00:00")
        res_naive_ts = validate_real_artifact_integrity(m_naive_ts)
        assert res_naive_ts["valid"] is False
        assert any("Naive timestamp" in e for e in res_naive_ts["errors"])

        # 7. FakeBrowserScreenshotAdapter + REAL capture mode => FAIL (Section 7)
        m_fake_adapter_real_mode = VisualEvidenceManifest("v1", "r1", REQUIRED_VIEWPORTS, paths, capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT", capture_adapter="FakeBrowserScreenshotAdapter", file_hashes=hashes_64, captured_at="2026-09-07T14:00:00+03:00")
        res_fake_adapter = validate_real_artifact_integrity(m_fake_adapter_real_mode)
        assert res_fake_adapter["valid"] is False
        assert any("FAKE_CAPTURE_ADAPTER_WITH_REAL_MODE" in e for e in res_fake_adapter["errors"])


def test_concept_id_gate_bypass_prevention_and_blocker_reporting():
    # Section 8 & 9: concept_id + gate FAIL => recommended_concept MUST be NONE, blockers returned
    pipeline = AutonomousDesignLoopPipeline()

    brief = DesignProjectBrief("b-1", "p-test", "Test Tenant", "Saas", "User", "Job", "Posture")

    # Run pipeline without evidence manifest / real visual critic => Gate FAIL
    res = pipeline.run_pipeline(
        brief=brief,
        initial_html="<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>",
        initial_css="",
        concept_id="SUPPLIED_CONCEPT_HERO_V2",
    )

    assert res.overall_verdict == JudgmentVerdict.PASS
    # Gate failed because evidence/manifest missing => recommended_concept MUST be NONE!
    assert res.recommendation.recommended_concept == "NONE"
    assert res.human_review_required_with_blockers is True
    assert len(res.blockers) > 0
    assert any("GROUNDING_CONTENT_MANIFEST_MISSING" in b or "REFERENCE_PROVENANCE_EVIDENCE_MISSING" in b for b in res.blockers)


def test_r3_visual_provider_provenance_and_concept_id_matrix():
    # R3 Section 6 Test Matrix requirements
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "test.png"
        content = b"test_screenshot_data_67890"
        file_path.write_bytes(content)
        correct_hash = hashlib.sha256(content).hexdigest()
        paths = {vp: str(file_path) for vp in REQUIRED_VIEWPORTS}
        hashes_64 = {vp: correct_hash for vp in REQUIRED_VIEWPORTS}

        evidence_manifest = VisualEvidenceManifest(
            "v1", "r1", REQUIRED_VIEWPORTS, paths,
            capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
            capture_adapter="PlaywrightAdapter",
            file_hashes=hashes_64,
            captured_at="2026-09-07T14:00:00+03:00",
        )

        ref_intel = ReferenceIntelligence()
        src = ref_intel.register_candidate_source(
            url_or_name="https://example-saas.com/metadata",
            purpose="Hero composition analysis",
            strength="Clear visual hierarchy & immediate value proposition",
            integration_cost="LOW",
            dependency_cost="ZERO",
            license_provenance="MIT / Metadata-Only",
            supply_chain_risk="LOW",
            generic_design_risk="LOW",
            recommended_use="Inspiration for sales fold structuring",
            do_not_use_conditions=["Do not copy raw CSS"],
            observation="Product screenshot is placed side-by-side with primary CTA",
        )
        sig = ref_intel.extract_design_signal(
            source_id=src.source_id,
            category="hero_composition",
            observation="Product screenshot is placed side-by-side with primary CTA",
            extracted_principle="Show real UI preview immediately above the fold",
        )

        ledger = GroundedFactLedger("l-1", "p-1")
        ledger.add_fact(GroundedFact("f-1", FactType.CANONICAL_TENANT_FACT, "Test Tenant", "Brief", "ref"))

        content_manifest = GroundedContentManifest(
            manifest_id="m-1",
            project_id="p-1",
            blocks=[
                GroundedContentBlock(
                    text="Welcome to Test Tenant",
                    semantic_role="hero",
                    provenance_kind=FactType.CANONICAL_TENANT_FACT,
                    source_fact_ids=["f-1"],
                    category=ContentBlockCategory.FACTUAL,
                    rendered_fact_bindings=[RenderedFactBinding("f-1", "Test Tenant")],
                )
            ],
        )

        # 1. FakeVisualCriticAdapter => HUMAN_READY NO
        scorecard = DesignCriticEnsemble().evaluate_project("p-1", "<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>", "")
        gate_fake = evaluate_human_visual_review_readiness(
            scorecard=scorecard,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=evidence_manifest,
            visual_adapter=FakeVisualCriticAdapter(),
        )
        assert gate_fake["is_ready"] is False
        assert any("FakeVisualCriticAdapter used" in r for r in gate_fake["reasons"])

        # 2. RenamedSimulatedAdapter with SIMULATED_VISUAL_TEST => HUMAN_READY NO
        class RenamedSimulatedAdapter(VisualCriticAdapter):
            evidence_origin = EvidenceOrigin.SIMULATED_VISUAL_TEST
        
        gate_renamed = evaluate_human_visual_review_readiness(
            scorecard=scorecard,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=evidence_manifest,
            visual_adapter=RenamedSimulatedAdapter(),
        )
        assert gate_renamed["is_ready"] is False
        assert any("is not REAL_PROVIDER_VISUAL_REVIEW" in r for r in gate_renamed["reasons"])

        # 3. NoOriginAdapter => HUMAN_READY NO
        class NoOriginAdapter:
            evidence_origin = None
        no_orig = NoOriginAdapter()
        
        gate_no_orig = evaluate_human_visual_review_readiness(
            scorecard=scorecard,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=evidence_manifest,
            visual_adapter=no_orig,
        )
        assert gate_no_orig["is_ready"] is False
        assert any("missing declared evidence origin" in r or "is not REAL_PROVIDER_VISUAL_REVIEW" in r for r in gate_no_orig["reasons"])

        # 4. RealOriginAdapter + STATIC_SOURCE_HEURISTIC finding => HUMAN_READY NO
        from extensions.design_intelligence.contracts import CritiqueFinding
        class RealOriginAdapter(VisualCriticAdapter):
            evidence_origin = EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW
            def evaluate_visuals(self, screenshot_paths, dna=None, story=None, fact_ledger=None, negative_preferences=None):
                return CritiqueFinding(
                    finding_id="f-pixel-pass",
                    critic_name="RealOriginAdapter",
                    verdict=JudgmentVerdict.PASS,
                    dimension="pixel_visual_quality",
                    title="Visual Quality",
                    details="Pass",
                    evidence_modality=EvidenceModality.PIXEL_VISUAL,
                )

        scorecard_static_modality = DesignCriticEnsemble().evaluate_project("p-1", "<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>", "")
        scorecard_static_modality.critic_findings.append(CritiqueFinding(
            finding_id="f-wrong-modality",
            critic_name="RealOriginAdapter",
            verdict=JudgmentVerdict.PASS,
            dimension="pixel_visual_quality",
            title="Visual Quality",
            details="Pass",
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,  # WRONG MODALITY!
        ))

        gate_static_modality = evaluate_human_visual_review_readiness(
            scorecard=scorecard_static_modality,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=evidence_manifest,
            visual_adapter=RealOriginAdapter(),
        )
        assert gate_static_modality["is_ready"] is False
        assert any("evidence modality 'EvidenceModality.STATIC_SOURCE_HEURISTIC' is not PIXEL_VISUAL" in r for r in gate_static_modality["reasons"])

        # 5. Test Double: RealOriginAdapter + PIXEL_VISUAL PASS => HUMAN_VISUAL_REVIEW_READY
        scorecard_valid_pixel = DesignCriticEnsemble().evaluate_project("p-1", "<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>", "")
        scorecard_valid_pixel.critic_findings.append(CritiqueFinding(
            finding_id="f-pixel-pass",
            critic_name="RealOriginAdapter",
            verdict=JudgmentVerdict.PASS,
            dimension="pixel_visual_quality",
            title="Visual Quality",
            details="Pass",
            evidence_modality=EvidenceModality.PIXEL_VISUAL,
        ))

        gate_valid_pixel = evaluate_human_visual_review_readiness(
            scorecard=scorecard_valid_pixel,
            fact_ledger=ledger,
            content_manifest=content_manifest,
            ref_intel=ref_intel,
            evidence_manifest=evidence_manifest,
            visual_adapter=RealOriginAdapter(),
        )
        assert gate_valid_pixel["is_ready"] is True
        assert gate_valid_pixel["state"] == HumanReviewReadinessState.HUMAN_VISUAL_REVIEW_READY

        # 6. Pipeline concept_id matrix checks with gate PASS
        ensemble_valid = DesignCriticEnsemble()
        ensemble_valid.visual_adapter = RealOriginAdapter()

        pipeline_pass = AutonomousDesignLoopPipeline(
            ref_intel=ref_intel,
            critic_ensemble=ensemble_valid,
        )
        brief = DesignProjectBrief("b-1", "p-1", "Test Tenant", "Saas", "User", "Job", "Posture", fact_ledger=ledger)

        # gate PASS + concept_id=None => recommendation NONE (Section 2 R3)
        res_no_concept = pipeline_pass.run_pipeline(
            brief=brief,
            initial_html="<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>",
            initial_css="",
            evidence_manifest=evidence_manifest,
            content_manifest=content_manifest,
            concept_id=None,
        )
        assert res_no_concept.human_review_state == HumanReviewReadinessState.HUMAN_VISUAL_REVIEW_READY
        assert res_no_concept.recommendation.recommended_concept == "NONE"

        # gate PASS + concept_id="CONCEPT_B" => recommendation CONCEPT_B (Section 2 R3)
        res_concept_b = pipeline_pass.run_pipeline(
            brief=brief,
            initial_html="<h1>Test Tenant</h1><button class='btn btn-primary'>CTA</button>",
            initial_css="",
            evidence_manifest=evidence_manifest,
            content_manifest=content_manifest,
            concept_id="CONCEPT_B",
        )
        assert res_concept_b.human_review_state == HumanReviewReadinessState.HUMAN_VISUAL_REVIEW_READY
        assert res_concept_b.recommendation.recommended_concept == "CONCEPT_B"

