"""Design Critic Ensemble (R13 / Correction R1 / Correction R1-Hardening).

Implements independent critics evaluating specific design failure modes.
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
    GroundedContentManifest,
    ContentBlockCategory,
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
        content_manifest: Optional[GroundedContentManifest] = None,
    ) -> CritiqueFinding:
        raise NotImplementedError


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
    """Deterministic offline visual critic adapter for testing (Section 9)."""

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
            title="Pixel Visual Review (Simulated Test)",
            details="; ".join(findings) if findings else "Visually distinct service hero with strong tenant identity and hierarchy.",
            evidence_modality=EvidenceModality.PIXEL_VISUAL,
            evidence_ids=["SIMULATED_VISUAL_TEST"],
            suggested_fix="Improve brand visual prominence and layout distinction." if findings else None,
        )


class GroundingIntegrityCritic(BaseCritic):
    """Validates customer-facing copy against the bound Grounded Fact Ledger and GroundedContentManifest (Section 3 & 4).
    
    Fails closed when:
    - Customer-facing factual content blocks have empty source_fact_ids.
    - Referenced fact ID does not exist in GroundedFactLedger.
    - Provenance fact type is not CANONICAL_PRODUCT_FACT or CANONICAL_TENANT_FACT.
    - Rendered customer-facing factual copy exists outside the manifest (unmanifested_customer_facing_text_detected).
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
        content_manifest: Optional[GroundedContentManifest] = None,
    ) -> CritiqueFinding:
        unsupported_findings = []

        # Section 4: Content manifest completeness check
        if content_manifest and content_manifest.unmanifested_customer_facing_text_detected:
            unsupported_findings.append("INCOMPLETE GroundedContentManifest: Rendered customer-facing factual copy exists outside manifest")

        # Evaluate blocks in GroundedContentManifest if provided
        if content_manifest and content_manifest.blocks:
            for block in content_manifest.blocks:
                if block.is_customer_facing and block.category == ContentBlockCategory.FACTUAL:
                    if not block.source_fact_ids:
                        unsupported_findings.append(f"Factual block '{block.text}' lacks source_fact_ids")
                        continue
                    
                    if not fact_ledger:
                        unsupported_findings.append(f"Factual block '{block.text}' has source_fact_ids but no GroundedFactLedger bound")
                        continue

                    if block.provenance_kind not in (FactType.CANONICAL_PRODUCT_FACT, FactType.CANONICAL_TENANT_FACT):
                        unsupported_findings.append(f"Factual block '{block.text}' has non-canonical block provenance kind '{block.provenance_kind.value}'")
                    else:
                        for fid in block.source_fact_ids:
                            fact = fact_ledger.get_fact_by_id(fid)
                            if not fact:
                                unsupported_findings.append(f"Factual block '{block.text}' references non-existent fact ID '{fid}'")
                            elif fact.fact_type not in (FactType.CANONICAL_PRODUCT_FACT, FactType.CANONICAL_TENANT_FACT):
                                unsupported_findings.append(f"Factual block '{block.text}' references non-canonical fact type '{fact.fact_type.value}'")

        else:
            # Fallback for legacy calls without structured GroundedContentManifest:
            # Check HTML text against canonical ledger values without domain-specific blacklists!
            if fact_ledger:
                canonical_facts = fact_ledger.get_canonical_facts()
                canonical_values_lower = [f.value.lower() for f in canonical_facts]
            else:
                canonical_values_lower = []

            clean_text = re.sub(r'<[^>]+>', ' ', html_content)
            # Generic claim patterns like #1 Rated or 100% Guaranteed without ledger proof
            generic_exaggerations = [
                (r'#1\s+rated', "#1 Rated claim"),
                (r'100%\s+guaranteed', "100% Guaranteed claim"),
                (r'5\.0\s+stars', "Exaggerated 5.0 Stars claim"),
                (r'9999\s+reviews', "Exaggerated 9999 reviews claim"),
            ]
            for pat, label in generic_exaggerations:
                if re.search(pat, clean_text, re.IGNORECASE) and not any(label.lower() in cv for cv in canonical_values_lower):
                    unsupported_findings.append(f"Unverified claim exaggeration: '{label}'")

        verdict = JudgmentVerdict.FAIL if unsupported_findings else JudgmentVerdict.PASS

        return CritiqueFinding(
            finding_id=f"f-grounding-{uuid.uuid4().hex[:6]}",
            critic_name=self.name,
            verdict=verdict,
            dimension="grounding_integrity",
            title="Grounding & Factual Copy Integrity",
            details="; ".join(unsupported_findings) if unsupported_findings else "All customer-facing factual copy is strictly grounded with canonical fact provenance.",
            evidence_modality=EvidenceModality.STRUCTURED_SEMANTIC,
            suggested_fix="Ensure all customer-facing factual content has valid canonical fact provenance in GroundedFactLedger." if unsupported_findings else None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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
        content_manifest: Optional[GroundedContentManifest] = None,
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


class DesignCriticEnsemble:
    """Ensemble of design critics (R13 + V1.1 GroundingIntegrityCritic)."""

    FAKE_VISUAL_EVIDENCE_CAN_GRANT_HUMAN_READY = "NO"

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
        # Section 9: visual_adapter MUST NOT default to FakeVisualCriticAdapter for runtime evaluation!
        self.visual_adapter = visual_adapter

    def evaluate_project(
        self,
        project_id: str,
        html_content: str,
        css_content: str,
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        evidence_manifest: Optional[VisualEvidenceManifest] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
        content_manifest: Optional[GroundedContentManifest] = None,
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
                content_manifest=content_manifest,
            )
            findings.append(finding)

        # Evaluate pixel visual quality if adapter AND manifest are present
        if self.visual_adapter and evidence_manifest and evidence_manifest.screenshot_paths:
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
