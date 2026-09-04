"""Design Feedback & Taste Memory (R15).

Stores explicit, versioned, reversible, source-attributed human design feedback
and generates explainable design recommendations.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import datetime
import uuid
from extensions.design_intelligence.contracts import (
    HumanDesignFeedback,
    FeedbackRating,
    DesignPreferenceProfile,
    DesignRecommendation,
    DesignDNA,
    ProductStorySpec,
)


class TasteMemory:
    """Versioned taste memory store for human design preferences."""

    def __init__(self):
        self._profiles: Dict[str, DesignPreferenceProfile] = {}  # project_id -> profile

    def get_or_create_profile(self, project_id: str) -> DesignPreferenceProfile:
        if project_id not in self._profiles:
            self._profiles[project_id] = DesignPreferenceProfile(
                profile_id=f"prof-{uuid.uuid4().hex[:8]}",
                project_id=project_id,
            )
        return self._profiles[project_id]

    def record_feedback(
        self,
        project_id: str,
        source_attribution: str,
        rating: FeedbackRating,
        target_element: str,
        comments: Optional[str] = None,
        is_reversible: bool = True,
    ) -> HumanDesignFeedback:
        profile = self.get_or_create_profile(project_id)
        feedback = HumanDesignFeedback(
            feedback_id=f"fb-{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            source_attribution=source_attribution,
            rating=rating,
            target_element=target_element,
            comments=comments,
            is_reversible=is_reversible,
        )
        profile.feedback_history.append(feedback)

        # Update liked principles vs disliked anti-patterns
        if rating in (FeedbackRating.LIKE, FeedbackRating.PREMIUM, FeedbackRating.GOOD_HERO, FeedbackRating.GOOD_MOTION, FeedbackRating.GOOD_PRODUCT_REVEAL):
            profile.liked_principles.append(f"{rating.value} on {target_element}")
        elif rating in (FeedbackRating.DISLIKE, FeedbackRating.TOO_GENERIC, FeedbackRating.TOO_BUSY, FeedbackRating.TOO_EMPTY, FeedbackRating.CHEAP_LOOKING, FeedbackRating.BAD_HERO, FeedbackRating.EXCESSIVE_MOTION):
            profile.disliked_anti_patterns.append(f"{rating.value} on {target_element}")

        return feedback

    def revert_feedback(self, project_id: str, feedback_id: str) -> bool:
        """Reverses a prior feedback entry if marked reversible."""
        profile = self.get_or_create_profile(project_id)
        fb_to_remove = next((f for f in profile.feedback_history if f.feedback_id == feedback_id), None)
        if fb_to_remove and fb_to_remove.is_reversible:
            profile.feedback_history.remove(fb_to_remove)
            pattern_like = f"{fb_to_remove.rating.value} on {fb_to_remove.target_element}"
            if pattern_like in profile.liked_principles:
                profile.liked_principles.remove(pattern_like)
            if pattern_like in profile.disliked_anti_patterns:
                profile.disliked_anti_patterns.remove(pattern_like)
            return True
        return False

    def generate_explainable_recommendation(
        self,
        project_id: str,
        base_dna: DesignDNA,
        story: ProductStorySpec,
    ) -> DesignRecommendation:
        profile = self.get_or_create_profile(project_id)
        provenance_ids = [f.feedback_id for f in profile.feedback_history]

        rationale_lines = [f"Recommendation for project '{project_id}':"]
        if profile.disliked_anti_patterns:
            rationale_lines.append(f"- Explicitly avoiding disliked patterns: {', '.join(profile.disliked_anti_patterns)}")
        if profile.liked_principles:
            rationale_lines.append(f"- Explicitly incorporating liked principles: {', '.join(profile.liked_principles)}")

        if not provenance_ids:
            rationale_lines.append("- Baseline recommendation (no prior explicit feedback recorded).")

        return DesignRecommendation(
            recommendation_id=f"rec-{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            recommended_dna=base_dna,
            product_story=story,
            provenance_feedback_ids=provenance_ids,
            rationale="\n".join(rationale_lines),
        )
