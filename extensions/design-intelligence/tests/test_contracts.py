"""Unit tests for Design Intelligence Contracts (R10)."""

import pytest
from extensions.design_intelligence.contracts import (
    DesignProjectBrief,
    DesignDNA,
    ProductStorySpec,
    SalesFoldSpec,
    CritiqueScorecard,
    CritiqueFinding,
    JudgmentVerdict,
    HumanDesignFeedback,
    FeedbackRating,
)


def test_contracts_creation_and_validation():
    brief = DesignProjectBrief(
        brief_id="b-1",
        project_id="p-lari",
        tenant_name="Melis Guzellik",
        industry="Beauty & Wellness",
        target_audience="Salon Clients",
        core_job_to_be_done="Book online appointments quickly",
        brand_posture="Premium & Elegant",
        supported_claims=["Online Booking 24/7", "Verified Stylists"],
    )
    assert brief.tenant_name == "Melis Guzellik"

    sales_fold = SalesFoldSpec(
        understandable_within_seconds=True,
        target_persona_clear=True,
        primary_value_prop="Hızlı ve Kolay Güzellik Randevusu",
        visible_product_evidence=True,
        dominant_cta_text="HEMEN RANDEVU AL",
        cta_result_understandable=True,
        creates_credible_desire=True,
    )

    finding = CritiqueFinding(
        finding_id="f-1",
        critic_name="AntiGenericDesignCritic",
        verdict=JudgmentVerdict.PASS,
        dimension="visual_personality",
        title="Custom Palette Used",
        details="Tailored warm beige and gold accents",
    )

    scorecard = CritiqueScorecard(
        scorecard_id="sc-1",
        project_id="p-lari",
        overall_verdict=JudgmentVerdict.PASS,
        critic_findings=[finding],
    )

    assert not scorecard.has_critical_failure()
