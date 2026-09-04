"""Design Critic Ensemble (R13).

Implements 7 independent critics evaluating specific design failure modes.
Enforces the rule that an overall score NEVER hides a critical FAIL.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import re
import uuid
from extensions.design_intelligence.contracts import (
    CritiqueFinding,
    CritiqueScorecard,
    JudgmentVerdict,
    DesignDNA,
    ProductStorySpec,
    VisualEvidenceManifest,
)


class BaseCritic:
    """Interface for design critics."""
    name: str = "BaseCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        raise NotImplementedError


class AntiGenericDesignCritic(BaseCritic):
    name = "AntiGenericDesignCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        anti_patterns = []

        # Generic purple/indigo AI gradients
        if "linear-gradient" in css_content and ("#6366f1" in css_content or "#8b5cf6" in css_content or "indigo" in css_content):
            anti_patterns.append("Generic indigo/purple AI gradient detected in CSS")

        # Decorative blobs / gratuitous glassmorphism
        if "backdrop-filter" in css_content and "blur" in css_content:
            anti_patterns.append("Gratuitous glassmorphism / decorative blob detected")

        # Repetitive card grids
        card_matches = len(re.findall(r'class=["\']card["\']', html_content)) + len(re.findall(r'class=["\']feature-card["\']', html_content))
        if card_matches >= 4:
            anti_patterns.append("Repetitive SaaS card grid repetition (>=4 uniform cards)")

        verdict = JudgmentVerdict.FAIL if len(anti_patterns) >= 2 else (
            JudgmentVerdict.WARN if anti_patterns else JudgmentVerdict.PASS
        )

        return CritiqueFinding(
            finding_id=f"f-antigeneric-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="anti_generic_design",
            title="Anti-Generic Design Quality",
            details="; ".join(anti_patterns) if anti_patterns else "Distinctive visual design with no generic anti-patterns.",
            suggested_fix="Replace generic SaaS cards/gradients with custom tenant-tailored typography and palette." if anti_patterns else None,
        )


class ConversionCritic(BaseCritic):
    name = "ConversionCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []
        if "btn" not in html_content and "button" not in html_content and "cta" not in html_content.lower():
            issues.append("No obvious primary call-to-action (CTA) button found")

        if html_content.lower().count("btn-primary") > 3:
            issues.append("Multiple competing primary CTAs dilute conversion focus")

        verdict = JudgmentVerdict.FAIL if "No obvious primary" in str(issues) else (
            JudgmentVerdict.WARN if issues else JudgmentVerdict.PASS
        )

        return CritiqueFinding(
            finding_id=f"f-conv-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="conversion_focus",
            title="Conversion & CTA Hierarchy",
            details="; ".join(issues) if issues else "Clear primary CTA and value proposition focus.",
            suggested_fix="Ensure a single dominant CTA above the fold." if issues else None,
        )


class VisualHierarchyCritic(BaseCritic):
    name = "VisualHierarchyCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []
        if "<h1" not in html_content:
            issues.append("Missing single primary H1 heading")

        verdict = JudgmentVerdict.FAIL if "<h1" not in html_content else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-hier-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="visual_hierarchy",
            title="Visual Hierarchy & Typography Pacing",
            details="; ".join(issues) if issues else "Strong visual hierarchy with structured headings.",
            suggested_fix="Add a prominent H1 title in the hero section." if issues else None,
        )


class EvidenceIntegrityCritic(BaseCritic):
    name = "EvidenceIntegrityCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []
        unsupported = ["#1 Rated", "100% Guaranteed", "5.0 Stars (9999 reviews)"]
        for un in unsupported:
            if un.lower() in html_content.lower():
                issues.append(f"Unverified exaggerated claim detected: '{un}'")

        verdict = JudgmentVerdict.FAIL if issues else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-evint-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="evidence_integrity",
            title="Evidence & Claim Integrity",
            details="; ".join(issues) if issues else "All tenant claims are grounded and authentic.",
            suggested_fix="Remove unverified claims or replace with real tenant proof." if issues else None,
        )


class AccessibilityHeuristicCritic(BaseCritic):
    name = "AccessibilityHeuristicCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []
        # Check if img tag exists without alt attribute
        for img_match in re.finditer(r'<img\s+[^>]*>', html_content, re.IGNORECASE):
            if 'alt=' not in img_match.group(0).lower():
                issues.append("Image tag missing alt attribute")
                break

        verdict = JudgmentVerdict.WARN if issues else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-a11y-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="accessibility_heuristics",
            title="Accessibility Heuristics",
            details="; ".join(issues) if issues else "Proper accessibility attributes present.",
            suggested_fix="Add descriptive alt text to all image tags." if issues else None,
        )


class DesignCoherenceCritic(BaseCritic):
    name = "DesignCoherenceCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []
        font_count = css_content.count("font-family")
        if font_count > 5:
            issues.append("Too many conflicting font-family declarations (>5)")

        verdict = JudgmentVerdict.WARN if issues else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-coherence-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="design_coherence",
            title="Design Coherence & Consistency",
            details="; ".join(issues) if issues else "Unified typography and layout rules.",
            suggested_fix="Consolidate font-family definitions into CSS design tokens." if issues else None,
        )


class ProductSemanticsCritic(BaseCritic):
    name = "ProductSemanticsCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueFinding:
        issues = []

        # Internal version labels shown to customers
        internal_labels = ["v2 pilot", "v1.8.14", "internal build", "canary release", "aos-runtime"]
        for label in internal_labels:
            if label.lower() in html_content.lower():
                issues.append(f"Internal version label leak detected: '{label}'")

        # Fictional ratings/reviews or wrong persona
        if "fictional" in html_content.lower() or "dummy rating" in html_content.lower():
            issues.append("Fictional rating / review text detected in tenant UI")

        verdict = JudgmentVerdict.FAIL if issues else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-semantics-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="product_semantics",
            title="Product & Tenant Semantics Integrity",
            details="; ".join(issues) if issues else "Zero internal label leaks or wrong persona copy.",
            suggested_fix="Strip internal release/build identifiers from customer-facing template text." if issues else None,
        )


class DesignCriticEnsemble:
    """Ensemble of all 7 design critics."""

    def __init__(self):
        self.critics: List[BaseCritic] = [
            AntiGenericDesignCritic(),
            ConversionCritic(),
            VisualHierarchyCritic(),
            EvidenceIntegrityCritic(),
            AccessibilityHeuristicCritic(),
            DesignCoherenceCritic(),
            ProductSemanticsCritic(),
        ]

    def evaluate_project(
        self,
        project_id: str,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
    ) -> CritiqueScorecard:
        findings = []
        for critic in self.critics:
            finding = critic.evaluate(
                html_content=html_content,
                css_content=css_content,
                dna=dna,
                story=story,
                evidence_manifest=evidence_manifest,
            )
            findings.append(finding)

        # Overall verdict rule: ANY critical FAIL -> overall FAIL
        if any(f.verdict == JudgmentVerdict.FAIL for f in findings):
            overall = JudgmentVerdict.FAIL
        elif any(f.verdict == JudgmentVerdict.WARN for f in findings):
            overall = JudgmentVerdict.WARN
        else:
            overall = JudgmentVerdict.PASS

        return CritiqueScorecard(
            scorecard_id=f"sc-{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            overall_verdict=overall,
            critic_findings=findings,
        )
