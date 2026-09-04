"""Unit tests for Design DNA Engine (R12)."""

import pytest
from extensions.design_intelligence.contracts import DesignProjectBrief
from extensions.design_intelligence.design_dna import DesignDNAEngine


def test_dna_generation_and_sales_fold_evaluation():
    brief = DesignProjectBrief(
        brief_id="b-beauty",
        project_id="p-melis",
        tenant_name="Melis Guzellik",
        industry="Beauty & Wellness",
        target_audience="Salon Clients",
        core_job_to_be_done="Book beauty appointments",
        brand_posture="Warm Elegance",
        supported_claims=["24/7 Booking"],
    )

    engine = DesignDNAEngine()
    dna = engine.generate_dna(brief)
    story = engine.generate_product_story(brief)

    assert "Beauty" in brief.industry
    assert "Warm" in dna.visual_personality
    assert story.sales_fold.understandable_within_seconds is True

    # Evaluate clean copy
    sales_fold_clean = engine.evaluate_sales_fold(story, "Melis Güzellik Salonu Online Randevu")
    assert len(sales_fold_clean.unsupported_claims_detected) == 0

    # Evaluate copy with unsupported claim
    sales_fold_bad = engine.evaluate_sales_fold(story, "Melis Güzellik - #1 Worldwide Best In Universe")
    assert len(sales_fold_bad.unsupported_claims_detected) > 0
    assert sales_fold_bad.creates_credible_desire is False
