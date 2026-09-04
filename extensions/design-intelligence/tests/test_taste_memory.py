"""Unit tests for Taste Memory (R15)."""

import pytest
from extensions.design_intelligence.contracts import (
    FeedbackRating,
    DesignDNA,
    ProductStorySpec,
    SalesFoldSpec,
)
from extensions.design_intelligence.taste_memory import TasteMemory


def test_taste_memory_record_revert_and_explainable_recommendation():
    tm = TasteMemory()

    fb1 = tm.record_feedback(
        project_id="p-melis",
        source_attribution="human_user:mozcelikbas",
        rating=FeedbackRating.PREMIUM,
        target_element="hero_section",
        comments="Love the warm gold accent typography",
    )

    fb2 = tm.record_feedback(
        project_id="p-melis",
        source_attribution="human_user:mozcelikbas",
        rating=FeedbackRating.TOO_GENERIC,
        target_element="card_grid",
        comments="SaaS card grid feels generic",
    )

    profile = tm.get_or_create_profile("p-melis")
    assert len(profile.feedback_history) == 2
    assert "PREMIUM on hero_section" in profile.liked_principles
    assert "TOO_GENERIC on card_grid" in profile.disliked_anti_patterns

    # Revert fb2
    reverted = tm.revert_feedback("p-melis", fb2.feedback_id)
    assert reverted is True
    assert len(profile.feedback_history) == 1
    assert "TOO_GENERIC on card_grid" not in profile.disliked_anti_patterns

    # Explainable recommendation
    dna = DesignDNA("dna-1", "p-melis", "Warm Elegance", [], [], "", "", "", "")
    story = ProductStorySpec("story-1", "Hero", [], [], SalesFoldSpec(True, True, "", True, "", True, True))

    rec = tm.generate_explainable_recommendation("p-melis", dna, story)
    assert rec.provenance_feedback_ids == [fb1.feedback_id]
    assert "PREMIUM on hero_section" in rec.rationale
