"""Unit tests for Reference Intelligence (R11)."""

import pytest
from extensions.design_intelligence.reference_intelligence import ReferenceIntelligence


def test_reference_intelligence_registration_and_analysis():
    ref_intel = ReferenceIntelligence()
    source = ref_intel.register_candidate_source(
        url_or_name="https://example-saas.com/metadata",
        purpose="Hero composition analysis",
        strength="Clear visual hierarchy & immediate value proposition",
        integration_cost="LOW",
        dependency_cost="ZERO",
        license_provenance="MIT / Metadata-Only",
        supply_chain_risk="LOW",
        generic_design_risk="LOW",
        recommended_use="Inspiration for sales fold structuring",
        do_not_use_conditions=["Do not copy raw CSS", "Do not clone brand assets"],
    )
    assert source.source_id.startswith("src-")

    signal = ref_intel.extract_design_signal(
        source_id=source.source_id,
        category="hero_composition",
        observation="Product screenshot is placed side-by-side with primary CTA and social proof badge",
        extracted_principle="Show real UI preview immediately above the fold alongside primary action",
    )
    assert signal.category == "hero_composition"

    results = ref_intel.query_signals("hero_composition")
    assert len(results) == 1
    assert results[0].signal_id == signal.signal_id
