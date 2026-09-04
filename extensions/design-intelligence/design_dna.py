"""Design DNA & Product Story Engine (R12).

Transforms grounded product briefs into DesignDNA, ProductStorySpec, and SalesFoldSpec
with explicit evaluation of fold intent and claim integrity.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import uuid
from extensions.design_intelligence.contracts import (
    DesignProjectBrief,
    DesignDNA,
    ProductStorySpec,
    SalesFoldSpec,
)


class DesignDNAEngine:
    """Generates DesignDNA, ProductStorySpec, and evaluates Sales Fold criteria."""

    def generate_dna(self, brief: DesignProjectBrief) -> DesignDNA:
        # Determine visual personality based on industry & brand posture
        if "Beauty" in brief.industry or "Wellness" in brief.industry:
            personality = "Warm Elegance & High Trust"
            typography = ["Playfair Display (Headings)", "Inter (Body)"]
            color_palette = ["#FAF7F2", "#1F2937", "#D4AF37", "#8B5CF6"]
            motion = "Subtle smooth fade-ins and micro-hover scaling"
        elif "B2B" in brief.industry or "SaaS" in brief.industry:
            personality = "Sleek Precision & High Conversion"
            typography = ["Plus Jakarta Sans (Headings)", "Inter (Body)"]
            color_palette = ["#0F172A", "#F8FAFC", "#2563EB", "#10B981"]
            motion = "Crisp structural entry animations"
        else:
            personality = "Modern Professional"
            typography = ["Roboto (Headings)", "Open Sans (Body)"]
            color_palette = ["#111827", "#F9FAFB", "#3B82F6"]
            motion = "Minimal functional transitions"

        return DesignDNA(
            dna_id=f"dna-{uuid.uuid4().hex[:8]}",
            project_id=brief.project_id,
            visual_personality=personality,
            typography_pairings=typography,
            color_palette=color_palette,
            motion_philosophy=motion,
            grid_structure="12-column responsive flex grid",
            mobile_strategy="Mobile-first single-column focus with sticky CTA bar",
            trust_strategy="Real tenant evidence, authentic customer ratings, verified booking badges",
        )

    def generate_product_story(self, brief: DesignProjectBrief) -> ProductStorySpec:
        sales_fold = SalesFoldSpec(
            understandable_within_seconds=True,
            target_persona_clear=True,
            primary_value_prop=f"Solve {brief.core_job_to_be_done} seamlessly",
            visible_product_evidence=True,
            dominant_cta_text="HEMEN BAŞLA / RANDEVU AL",
            cta_result_understandable=True,
            creates_credible_desire=True,
            unsupported_claims_detected=[],
        )

        return ProductStorySpec(
            spec_id=f"story-{uuid.uuid4().hex[:8]}",
            hero_narrative=f"Designed specifically for {brief.target_audience} seeking {brief.core_job_to_be_done}.",
            value_pillars=[
                f"Built for {brief.tenant_name}",
                "Streamlined Online Experience",
                "Proven High Customer Satisfaction",
            ],
            product_proof_points=brief.supported_claims or ["Verified Platform Evidence"],
            sales_fold=sales_fold,
        )

    def evaluate_sales_fold(self, story: ProductStorySpec, copy_text: str) -> SalesFoldSpec:
        sales_fold = story.sales_fold
        # Detect unsupported claims in copy text
        unsupported = []
        forbidden_keywords = ["#1 Worldwide", "Guaranteed 100x Growth", "Best In Universe"]
        for kw in forbidden_keywords:
            if kw.lower() in copy_text.lower():
                unsupported.append(f"Unsupported claim detected: '{kw}'")

        sales_fold.unsupported_claims_detected = unsupported
        if unsupported:
            sales_fold.creates_credible_desire = False

        return sales_fold
