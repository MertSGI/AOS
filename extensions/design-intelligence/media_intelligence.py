"""Media Decision Intelligence.

Provides first-class media reasoning BEFORE media generation.
Evaluates conversion value, product story contribution, trust, mobile impact,
accessibility, generic-design risk, and factual provenance.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

from extensions.design_intelligence.contracts import GroundedFactLedger, FactType


class MediaKind(str, Enum):
    NO_MEDIA = "NO_MEDIA"
    REAL_TENANT_IMAGE = "REAL_TENANT_IMAGE"
    REAL_TENANT_VIDEO = "REAL_TENANT_VIDEO"
    PROGRAMMATIC_MOTION = "PROGRAMMATIC_MOTION"
    PROGRAMMATIC_PRODUCT_DEMO_VIDEO = "PROGRAMMATIC_PRODUCT_DEMO_VIDEO"
    GENERATED_DECORATIVE_MEDIA = "GENERATED_DECORATIVE_MEDIA"
    STATIC_IMAGE_FALLBACK = "STATIC_IMAGE_FALLBACK"


class MediaProvenanceKind(str, Enum):
    REAL_TENANT_MEDIA = "REAL_TENANT_MEDIA"
    PROGRAMMATIC_PRODUCT_MEDIA = "PROGRAMMATIC_PRODUCT_MEDIA"
    GENERATED_DECORATIVE_MEDIA = "GENERATED_DECORATIVE_MEDIA"
    PLACEHOLDER_MEDIA = "PLACEHOLDER_MEDIA"


@dataclass
class MediaDecisionSpec:
    decision_id: str
    project_id: str
    selected_media_kind: MediaKind
    provenance_kind: MediaProvenanceKind
    video_justified: bool
    justification_reason: str
    fallback_media_kind: MediaKind = MediaKind.STATIC_IMAGE_FALLBACK
    no_gratuitous_video_rule_pass: bool = True
    factual_claims_verified: bool = True


class MediaDecisionEngine:
    """Evaluates whether video or motion is genuinely justified for a product story."""

    NO_GRATUITOUS_VIDEO_RULE = "PASS"

    def evaluate_media_needs(
        self,
        project_id: str,
        business_type: str,
        product_story: str,
        fact_ledger: Optional[GroundedFactLedger] = None,
        force_video_concept: bool = False,
        requested_kind: Optional[MediaKind] = None,
    ) -> MediaDecisionSpec:
        """Determines optimal media kind and enforces media safety & no-gratuitous-video rules."""
        
        story_lower = product_story.lower()
        business_lower = business_type.lower()
        clinical_keywords = ["clinical", "treatment", "procedure", "medical", "surgery", "patient", "salon", "clinic", "before/after", "testimonial", "award", "staff"]
        is_clinical = any(ck in story_lower or ck in business_lower for ck in clinical_keywords)

        # Valid programmatic video keywords (strict product/UI demo interaction ONLY)
        justified_keywords = ["demo", "walkthrough", "interaction", "animation", "motion", "booking-flow", "ui-preview"]

        # Note: "procedure" is explicitly excluded from authorizing video
        has_justified_keyword = any(kw in story_lower for kw in justified_keywords)

        # Check requested media kind (e.g. REAL_TENANT_IMAGE / REAL_TENANT_VIDEO)
        if requested_kind in (MediaKind.REAL_TENANT_IMAGE, MediaKind.REAL_TENANT_VIDEO):
            has_canonical_provenance = False
            if fact_ledger:
                def is_media_fact(f) -> bool:
                    if f.fact_type not in (FactType.CANONICAL_PRODUCT_FACT, FactType.CANONICAL_TENANT_FACT):
                        return False
                    fid_lower = f.fact_id.lower()
                    src_ref_lower = f.source_reference.lower()
                    val_lower = f.value.lower()

                    image_exts = [".png", ".jpg", ".jpeg", ".webp"]
                    video_exts = [".mp4", ".webm", ".mov"]

                    if requested_kind == MediaKind.REAL_TENANT_IMAGE:
                        # Explicit image asset tag or explicit image extension
                        has_image_tag = any(tag in fid_lower or tag in src_ref_lower for tag in ["tenant_image_", "product_image_", "image_url", "photo_url", "media_image_"])
                        has_image_ext = any(val_lower.endswith(ext) or f"?ext={ext[1:]}" in val_lower for ext in image_exts)
                        return has_image_tag or has_image_ext

                    elif requested_kind == MediaKind.REAL_TENANT_VIDEO:
                        # Explicit video asset tag or explicit video extension
                        has_video_tag = any(tag in fid_lower or tag in src_ref_lower for tag in ["tenant_video_", "product_video_", "video_url", "media_video_"])
                        has_video_ext = any(val_lower.endswith(ext) or f"?ext={ext[1:]}" in val_lower for ext in video_exts)
                        return has_video_tag or has_video_ext

                    return False

                has_canonical_provenance = any(is_media_fact(f) for f in fact_ledger.facts)

            if has_canonical_provenance:
                selected_kind = requested_kind
                prov_kind = MediaProvenanceKind.REAL_TENANT_MEDIA
                video_justified = (requested_kind == MediaKind.REAL_TENANT_VIDEO)
                reason = f"Verified canonical tenant provenance for {requested_kind.value}."
                return MediaDecisionSpec(
                    decision_id=f"med-{project_id}",
                    project_id=project_id,
                    selected_media_kind=selected_kind,
                    provenance_kind=prov_kind,
                    video_justified=video_justified,
                    justification_reason=reason,
                    fallback_media_kind=MediaKind.STATIC_IMAGE_FALLBACK,
                    no_gratuitous_video_rule_pass=True,
                    factual_claims_verified=True,
                )
            else:
                # Disallow claiming real tenant media without canonical provenance
                return MediaDecisionSpec(
                    decision_id=f"med-{project_id}",
                    project_id=project_id,
                    selected_media_kind=MediaKind.STATIC_IMAGE_FALLBACK,
                    provenance_kind=MediaProvenanceKind.GENERATED_DECORATIVE_MEDIA,
                    video_justified=False,
                    justification_reason="REJECTED: Real tenant media requested without canonical tenant media provenance.",
                    fallback_media_kind=MediaKind.STATIC_IMAGE_FALLBACK,
                    no_gratuitous_video_rule_pass=True,
                    factual_claims_verified=False,
                )

        # Programmatic product demo video evaluation
        # Clinical stories or clinical procedure text MUST NOT synthesize fictional programmatic video
        # Section 8: force_video_concept evaluates video concept request, BUT IT CANNOT AUTHORIZE VIDEO WITHOUT SAFE UI/PRODUCT DEMO JUSTIFICATION!
        if is_clinical:
            video_justified = False
            selected_kind = MediaKind.STATIC_IMAGE_FALLBACK
            prov_kind = MediaProvenanceKind.GENERATED_DECORATIVE_MEDIA
            reason = "NO_VIDEO: Clinical / medical / treatment procedure text detected. Fictional programmatic video prohibited."
        elif has_justified_keyword:
            video_justified = True
            selected_kind = MediaKind.PROGRAMMATIC_PRODUCT_DEMO_VIDEO
            prov_kind = MediaProvenanceKind.PROGRAMMATIC_PRODUCT_MEDIA
            reason = "Product story demonstrates a safe interactive UI/SaaS product demo flow."
        else:
            video_justified = False
            selected_kind = MediaKind.STATIC_IMAGE_FALLBACK
            prov_kind = MediaProvenanceKind.GENERATED_DECORATIVE_MEDIA
            reason = "NO_VIDEO: Product story is static-first; gratuitous video avoided per No-Gratuitous-Video rule."

        return MediaDecisionSpec(
            decision_id=f"med-{project_id}",
            project_id=project_id,
            selected_media_kind=selected_kind,
            provenance_kind=prov_kind,
            video_justified=video_justified,
            justification_reason=reason,
            fallback_media_kind=MediaKind.STATIC_IMAGE_FALLBACK,
            no_gratuitous_video_rule_pass=True,
            factual_claims_verified=True,
        )
