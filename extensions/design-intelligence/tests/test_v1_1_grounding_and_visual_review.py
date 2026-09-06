"""Unit tests for Design Intelligence V1.1 Real-World Grounding & Visual Judgment (R19 / V1.1)."""

import pytest
from extensions.design_intelligence.contracts import (
    GroundedFactLedger,
    GroundedFact,
    FactType,
    GroundedContentBlock,
    JudgmentVerdict,
    EvidenceModality,
    VisualQACoverage,
    HumanReviewReadinessState,
)
from extensions.design_intelligence.critics import (
    GroundingIntegrityCritic,
    AntiGenericDesignCritic,
    FakeVisualCriticAdapter,
    DesignCriticEnsemble,
    STATIC_CRITIC_MAY_GRANT_PIXEL_VISUAL_PASS,
)
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence
from extensions.design_intelligence.visual_qa import VisualQAEvaluator, FakeBrowserScreenshotAdapter


def test_grounded_fact_ledger_creation_and_filtering():
    ledger = GroundedFactLedger(ledger_id="led-1", project_id="p-101")
    f1 = GroundedFact(
        fact_id="f-1",
        fact_type=FactType.CANONICAL_TENANT_FACT,
        value="Melis Güzellik & Nail Art",
        source_type="CanonicalTenantRecord",
        source_reference="tenant_profile.name",
    )
    f2 = GroundedFact(
        fact_id="f-2",
        fact_type=FactType.CANONICAL_PRODUCT_FACT,
        value="Manicure & Pedicure Services",
        source_type="CanonicalProductCatalog",
        source_reference="catalog.services",
    )
    f3 = GroundedFact(
        fact_id="f-3",
        fact_type=FactType.PLACEHOLDER_VISUAL_CONTENT,
        value="Hero Background Image Placeholder",
        source_type="DesignSystem",
        source_reference="ui.hero",
    )

    ledger.add_fact(f1)
    ledger.add_fact(f2)
    ledger.add_fact(f3)

    assert len(ledger.facts) == 3
    canonical_facts = ledger.get_canonical_facts()
    assert len(canonical_facts) == 2
    assert all(f.fact_type in (FactType.CANONICAL_PRODUCT_FACT, FactType.CANONICAL_TENANT_FACT) for f in canonical_facts)


def test_grounding_integrity_critic_catches_unsupported_facts():
    critic = GroundingIntegrityCritic()
    ledger = GroundedFactLedger(ledger_id="led-2", project_id="p-melis")
    ledger.add_fact(GroundedFact(
        fact_id="f-1",
        fact_type=FactType.CANONICAL_TENANT_FACT,
        value="Melis Güzellik & Nail Art",
        source_type="TenantProfile",
        source_reference="name",
    ))

    # Supported tenant name => PASS
    good_html = "<h1>Melis Güzellik & Nail Art</h1><p>Manicure & Pedicure</p>"
    finding_good = critic.evaluate(good_html, "", fact_ledger=ledger)
    assert finding_good.verdict == JudgmentVerdict.PASS
    assert finding_good.evidence_modality == EvidenceModality.STRUCTURED_SEMANTIC

    # Invented business name => FAIL
    bad_name_html = "<h1>Melis Beauty Studio</h1><p>Nail Art</p>"
    finding_bad_name = critic.evaluate(bad_name_html, "", fact_ledger=ledger)
    assert finding_bad_name.verdict == JudgmentVerdict.FAIL
    assert "melis beauty studio" in finding_bad_name.details.lower()


    # Invented location => FAIL
    bad_loc_html = "<h1>Melis Güzellik</h1><p>Nişantaşı Branch</p>"
    finding_bad_loc = critic.evaluate(bad_loc_html, "", fact_ledger=ledger)
    assert finding_bad_loc.verdict == JudgmentVerdict.FAIL
    assert "nişantaşı" in finding_bad_loc.details.lower()

    # Invented services/credentials => FAIL
    bad_service_html = "<h1>Melis Güzellik</h1><p>Bridal Consultation by Award-Winning Master Artists</p>"
    finding_bad_service = critic.evaluate(bad_service_html, "", fact_ledger=ledger)
    assert finding_bad_service.verdict == JudgmentVerdict.FAIL


def test_reference_provenance_rejects_placeholders():
    ref_intel = ReferenceIntelligence()
    assert ref_intel.REFERENCE_SOURCE_PLACEHOLDER_REJECTED == "YES"

    # Valid real source
    valid_source = ref_intel.register_candidate_source(
        url_or_name="https://stripe.com/checkout",
        purpose="Checkout flow UX",
        strength="Clear conversion pacing",
        integration_cost="LOW",
        dependency_cost="ZERO",
        license_provenance="Public UX Pattern",
        supply_chain_risk="LOW",
        generic_design_risk="LOW",
        recommended_use="CTA alignment inspiration",
    )
    assert valid_source.source_id.startswith("src-")

    # Generic placeholder source => FAIL CLOSED
    with pytest.raises(ValueError) as exc:
        ref_intel.register_candidate_source(
            url_or_name="REF-001 Editorial Luxury Beauty",
            purpose="Placeholder editorial concept",
            strength="Unknown",
            integration_cost="HIGH",
            dependency_cost="HIGH",
            license_provenance="None",
            supply_chain_risk="HIGH",
            generic_design_risk="HIGH",
            recommended_use="None",
        )
    assert "REFERENCE_SOURCE_PLACEHOLDER_REJECTED=YES" in str(exc.value)


def test_critic_evidence_modality_and_static_restriction():
    assert STATIC_CRITIC_MAY_GRANT_PIXEL_VISUAL_PASS == "NO"

    anti_generic = AntiGenericDesignCritic()
    finding = anti_generic.evaluate("<h1>Clean Title</h1>", "h1 { color: #333; }")
    assert finding.evidence_modality == EvidenceModality.STATIC_SOURCE_HEURISTIC
    assert finding.evidence_modality != EvidenceModality.PIXEL_VISUAL


def test_visual_qa_partial_vs_full_coverage():
    evaluator = VisualQAEvaluator()

    # Partial coverage: only 375 and 1440
    adapter_partial = FakeBrowserScreenshotAdapter(viewports_to_capture=[375, 1440])
    manifest_partial = adapter_partial.capture_manifest("http://localhost:3000/", "run-partial")
    res_partial = evaluator.evaluate_manifest(manifest_partial)

    assert res_partial.coverage_status == VisualQACoverage.PARTIAL_COVERAGE
    assert res_partial.overall_pass is False
    assert set(res_partial.missing_viewports) == {390, 768, 1024, 1920}

    # Full coverage: all six clean viewports
    adapter_full = FakeBrowserScreenshotAdapter(viewports_to_capture=[375, 390, 768, 1024, 1440, 1920])
    manifest_full = adapter_full.capture_manifest("http://localhost:3000/", "run-full")
    res_full = evaluator.evaluate_manifest(manifest_full)

    assert res_full.coverage_status == VisualQACoverage.FULL_PASS
    assert res_full.overall_pass is True
