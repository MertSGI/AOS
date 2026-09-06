# Design Intelligence V1.1 — Grounded Visual Review Architecture

## Overview

Design Intelligence V1.1 introduces real-world fact grounding, strict reference provenance, evidence modality distinctions, pixel visual critic interfaces, responsive QA coverage classification, and human review readiness gates.

---

## 3-Tier Judgment Distinction (Section 16)

Design evaluation explicitly separates three distinct levels of validation:

```
+-----------------------------------------------------------------------------+
| 1. STATIC QUALITY CHECK (EvidenceModality.STATIC_SOURCE_HEURISTIC)          |
|    - HTML/CSS regex, heading tags, alt text presence, font counts.         |
|    - MAY NOT grant pixel visual pass or human design approval.              |
+-----------------------------------------------------------------------------+
                                     |
                                     v
+-----------------------------------------------------------------------------+
| 2. REAL VISUAL JUDGMENT (EvidenceModality.PIXEL_VISUAL / VisualCritic)      |
|    - Evaluates actual rendered viewport screenshots via VisualCriticAdapter. |
|    - Evaluates business identity prominence, CTA dominance, composition.     |
|    - Proves distinction between business-first hero and generic SaaS hero.  |
+-----------------------------------------------------------------------------+
                                     |
                                     v
+-----------------------------------------------------------------------------+
| 3. HUMAN DESIGN ACCEPTANCE (HumanReviewReadinessState.HUMAN_ACCEPTED)      |
|    - Requires real inspectable screenshot artifacts on disk.                |
|    - GroundingIntegrityCritic PASS + Reference Provenance PASS.            |
|    - Full 6-viewport responsive QA coverage satisfied.                      |
|    - ZERO automated numeric score can produce HUMAN_ACCEPTED.              |
+-----------------------------------------------------------------------------+
```

---

## Required Evidence Per Level

| Level | Required Evidence & Manifest Details |
|---|---|
| **Static Quality Check** | DOM structure, CSS declarations, `STATIC_SOURCE_HEURISTIC` findings. |
| **Real Visual Judgment** | Real rendered screenshot files, `VisualEvidenceManifest` with recorded hashes and viewport paths, `VisualCriticAdapter` findings. |
| **Human Design Acceptance** | `HUMAN_VISUAL_REVIEW_READY` state, clean 6-viewport coverage (`FULL_PASS`), zero critical critic fails, explicit human review action. |

---

## Core Components

1. **GroundedFactLedger & GroundingIntegrityCritic**: Registers canonical tenant/product facts and fails closed if customer-facing copy introduces unverified names, locations, services, or claims.
2. **Real Reference Provenance**: Enforces non-placeholder source registration (`REFERENCE_SOURCE_PLACEHOLDER_REJECTED=YES`).
3. **VisualCriticAdapter Interface**: Abstract interface for visual pixel evaluation with offline `FakeVisualCriticAdapter`.
4. **Visual QA Responsive Truthfulness**: Classifies multi-viewport QA into `FULL_PASS` (all 6 clean), `PARTIAL_COVERAGE` (subset rendered without defects), or `FAIL`.
5. **No Forced Recommendation**: Enforces `NO_FORCED_LEAST_BAD_RECOMMENDATION=YES` (`RECOMMENDED_CONCEPT=NONE` when weak/failing).
