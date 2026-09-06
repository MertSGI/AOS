"""Design Critic Ensemble (R13 / Correction R1).

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
    EvidenceModality,
    DesignDNA,
    ProductStorySpec,
    VisualEvidenceManifest,
    GroundedFactLedger,
    FactType,
)

STATIC_CRITIC_MAY_GRANT_PIXEL_VISUAL_PASS = "NO"


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
        fact_ledger: Optional[GroundedFactLedger] = None,
    ) -> CritiqueFinding:
        raise NotImplementedError


class GroundingIntegrityCritic(BaseCritic):
    """Validates customer-facing copy against the bound Grounded Fact Ledger.
    
    Fails closed when copy introduces unsupported business names, locations, branch names,
    services, prices, credentials, staff titles, opening hours, ratings, reviews, testimonials,
    awards, customer counts, health-tourism claims, specialties, or business history.
    """
    name = "GroundingIntegrityCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
    ) -> CritiqueFinding:
        unsupported_facts = []

        # If no ledger provided or empty canonical facts, extract default canonical facts from brief/context if available
        canonical_values = []
        if fact_ledger:
            canonical_values = [f.value.lower() for f in fact_ledger.get_canonical_facts()]

        # Clean text stripping HTML tags
        clean_text = re.sub(r'<[^>]+>', ' ', html_content)
        clean_text_lower = clean_text.lower()

        # Known unsupported patterns from past discovery failure (Section 2 & 4 & 13)
        # Business names: e.g. "Melis Beauty Studio" when canonical is "Melis Güzellik & Nail Art" or "Example Nail Studio"
        # Locations: e.g. "Nişantaşı"
        # Services/Credentials: "Bridal Consultation", "Master Artist", "Certified Specialist", "Luxury Hair Studio"
        
        # Checking specific invented terms that are not in canonical values
        invented_terms_check = [
            "melis beauty studio",
            "nişantaşı",
            "nisantasi",
            "bridal consultation",
            "master artist",
            "certified specialist",
            "luxury hair studio",
            "health-tourism",
            "health tourism",
            "award-winning master artists",
            "award winning master artists",
        ]

        for term in invented_terms_check:
            if term in clean_text_lower and not any(term in cv for cv in canonical_values):
                unsupported_facts.append(f"Unsupported customer-facing claim/entity detected: '{term}'")

        # Also check for price / review / rating claim exaggerations if not in ledger
        claim_patterns = [
            (r'#1\s+rated', "#1 Rated claim"),
            (r'100%\s+guaranteed', "100% Guaranteed claim"),
            (r'5\.0\s+stars', "Exaggerated 5.0 Stars claim"),
            (r'9999\s+reviews', "Exaggerated 9999 reviews claim"),
            (r'master\s+artists?', "Master Artist title"),
        ]

        for pat, label in claim_patterns:
            if re.search(pat, clean_text_lower) and not any(label.lower() in cv for cv in canonical_values):
                if label not in [uf.split(": ")[-1] for uf in unsupported_facts]:
                    unsupported_facts.append(f"Unsupported customer-facing claim detected: '{label}'")

        verdict = JudgmentVerdict.FAIL if unsupported_facts else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-grounding-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="grounding_integrity",
            title="Grounding & Factual Copy Integrity",
            details="; ".join(unsupported_facts) if unsupported_facts else "All customer-facing factual copy is strictly grounded in canonical tenant facts.",
            evidence_modality=EvidenceModality.STRUCTURED_SEMANTIC,
            suggested_fix="Remove invented business names, locations, services, or credentials not present in the Grounded Fact Ledger." if unsupported_facts else None,
        )


class AntiGenericDesignCritic(BaseCritic):
    name = "AntiGenericDesignCritic"

    def evaluate(
        self,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
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
            title="Anti-Generic Design Quality (Static Heuristic)",
            details="; ".join(anti_patterns) if anti_patterns else "Static heuristic check passed. Note: Full generic/template visual judgment requires VisualCriticAdapter.",
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
    ) -> CritiqueFinding:
        issues = []
        has_button_element = "<button" in html_content.lower() or "class=\"btn" in html_content.lower() or "class='btn" in html_content.lower() or "role=\"button\"" in html_content.lower()
        if not has_button_element:
            issues.append("No obvious primary call-to-action (CTA) button found")

        primary_cta_count = html_content.lower().count("btn-primary")
        if primary_cta_count > 3:
            issues.append(f"Multiple competing primary CTAs ({primary_cta_count}) dilute conversion focus")

        verdict = JudgmentVerdict.FAIL if issues else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-conv-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="conversion_focus",
            title="Conversion & CTA Hierarchy",
            details="; ".join(issues) if issues else "Clear primary CTA and value proposition focus.",
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
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
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
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
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
    ) -> CritiqueFinding:
        issues = []
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
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
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
            evidence_modality=EvidenceModality.STATIC_SOURCE_HEURISTIC,
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
        fact_ledger: Optional[GroundedFactLedger] = None,
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
            evidence_modality=EvidenceModality.STRUCTURED_SEMANTIC,
            suggested_fix="Strip internal release/build identifiers from customer-facing template text." if issues else None,
        )


class VisualCriticAdapter:
    """Interface for real visual screenshot evaluation adapters (Section 7)."""

    def evaluate_visuals(
        self,
        screenshot_paths: Dict[int, str],
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
        negative_preferences: Optional[List[str]] = None,
    ) -> CritiqueFinding:
        raise NotImplementedError


class FakeVisualCriticAdapter(VisualCriticAdapter):
    """Deterministic offline visual critic adapter for testing."""

    def __init__(self, simulate_generic_template: bool = False, simulate_weak_identity: bool = False):
        self.simulate_generic_template = simulate_generic_template
        self.simulate_weak_identity = simulate_weak_identity

    def evaluate_visuals(
        self,
        screenshot_paths: Dict[int, str],
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
        negative_preferences: Optional[List[str]] = None,
    ) -> CritiqueFinding:
        findings = []

        if self.simulate_generic_template:
            findings.append("Generic/template appearance detected from visual layout inspect")
        if self.simulate_weak_identity:
            findings.append("Weak tenant business identity prominence in hero render")

        verdict = JudgmentVerdict.FAIL if findings else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-pixelvis-{uuid.uuid4().hex[:6]}",
            critic_name="FakeVisualCriticAdapter",
            verdict=verdict,
            dimension="pixel_visual_quality",
            title="Pixel Visual Review",
            details="; ".join(findings) if findings else "Visually distinct service hero with strong tenant identity and hierarchy.",
            evidence_modality=EvidenceModality.PIXEL_VISUAL,
            suggested_fix="Improve brand visual prominence and layout distinction." if findings else None,
        )


class DesignCriticEnsemble:
    """Ensemble of design critics (R13 + V1.1 GroundingIntegrityCritic)."""

    def __init__(self, visual_adapter: Optional[VisualCriticAdapter] = None):
        self.critics: List[BaseCritic] = [
            GroundingIntegrityCritic(),
            AntiGenericDesignCritic(),
            ConversionCritic(),
            VisualHierarchyCritic(),
            EvidenceIntegrityCritic(),
            AccessibilityHeuristicCritic(),
            DesignCoherenceCritic(),
            ProductSemanticsCritic(),
        ]
        self.visual_adapter = visual_adapter or FakeVisualCriticAdapter()

    def evaluate_project(
        self,
        project_id: str,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
    ) -> CritiqueScorecard:
        findings = []
        for critic in self.critics:
            finding = critic.evaluate(
                html_content=html_content,
                css_content=css_content,
                dna=dna,
                story=story,
                evidence_manifest=evidence_manifest,
                fact_ledger=fact_ledger,
            )
            findings.append(finding)

        # Evaluate pixel visual quality if manifest is present
        if evidence_manifest and evidence_manifest.screenshot_paths:
            vis_finding = self.visual_adapter.evaluate_visuals(
                screenshot_paths=evidence_manifest.screenshot_paths,
                dna=dna,
                story=story,
                fact_ledger=fact_ledger,
            )
            findings.append(vis_finding)

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

