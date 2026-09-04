"""Generator script for 14 Design Intelligence JSON Schemas (Correction R1)."""

import json
import os

SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))

SCHEMAS = {
    "DesignProjectBrief.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DesignProjectBrief",
        "type": "object",
        "required": ["brief_id", "project_id", "tenant_name", "industry", "target_audience", "core_job_to_be_done", "brand_posture"],
        "properties": {
            "brief_id": {"type": "string"},
            "project_id": {"type": "string"},
            "tenant_name": {"type": "string"},
            "industry": {"type": "string"},
            "target_audience": {"type": "string"},
            "core_job_to_be_done": {"type": "string"},
            "brand_posture": {"type": "string"},
            "supported_claims": {"type": "array", "items": {"type": "string"}},
            "version": {"type": "string"},
            "created_at": {"type": "string"}
        },
        "additionalProperties": False
    },
    "DesignDNA.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DesignDNA",
        "type": "object",
        "required": ["dna_id", "project_id", "visual_personality", "typography_pairings", "color_palette", "motion_philosophy", "grid_structure", "mobile_strategy", "trust_strategy"],
        "properties": {
            "dna_id": {"type": "string"},
            "project_id": {"type": "string"},
            "visual_personality": {"type": "string"},
            "typography_pairings": {"type": "array", "items": {"type": "string"}},
            "color_palette": {"type": "array", "items": {"type": "string"}},
            "motion_philosophy": {"type": "string"},
            "grid_structure": {"type": "string"},
            "mobile_strategy": {"type": "string"},
            "trust_strategy": {"type": "string"},
            "version": {"type": "string"}
        },
        "additionalProperties": False
    },
    "ReferenceSource.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ReferenceSource",
        "type": "object",
        "required": ["source_id", "url_or_name", "purpose", "strength", "integration_cost", "dependency_cost", "license_provenance", "supply_chain_risk", "generic_design_risk", "recommended_use"],
        "properties": {
            "source_id": {"type": "string"},
            "url_or_name": {"type": "string"},
            "purpose": {"type": "string"},
            "strength": {"type": "string"},
            "integration_cost": {"type": "string"},
            "dependency_cost": {"type": "string"},
            "license_provenance": {"type": "string"},
            "supply_chain_risk": {"type": "string"},
            "generic_design_risk": {"type": "string"},
            "recommended_use": {"type": "string"},
            "do_not_use_conditions": {"type": "array", "items": {"type": "string"}}
        },
        "additionalProperties": False
    },
    "ReferenceSignal.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ReferenceSignal",
        "type": "object",
        "required": ["signal_id", "source_id", "category", "observation", "extracted_principle"],
        "properties": {
            "signal_id": {"type": "string"},
            "source_id": {"type": "string"},
            "category": {"type": "string"},
            "observation": {"type": "string"},
            "extracted_principle": {"type": "string"},
            "confidence_score": {"type": "number"}
        },
        "additionalProperties": False
    },
    "SalesFoldSpec.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "SalesFoldSpec",
        "type": "object",
        "required": ["understandable_within_seconds", "target_persona_clear", "primary_value_prop", "visible_product_evidence", "dominant_cta_text", "cta_result_understandable", "creates_credible_desire"],
        "properties": {
            "understandable_within_seconds": {"type": "boolean"},
            "target_persona_clear": {"type": "boolean"},
            "primary_value_prop": {"type": "string"},
            "visible_product_evidence": {"type": "boolean"},
            "dominant_cta_text": {"type": "string"},
            "cta_result_understandable": {"type": "boolean"},
            "creates_credible_desire": {"type": "boolean"},
            "unsupported_claims_detected": {"type": "array", "items": {"type": "string"}}
        },
        "additionalProperties": False
    },
    "ProductStorySpec.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ProductStorySpec",
        "type": "object",
        "required": ["spec_id", "hero_narrative", "value_pillars", "product_proof_points", "sales_fold"],
        "properties": {
            "spec_id": {"type": "string"},
            "hero_narrative": {"type": "string"},
            "value_pillars": {"type": "array", "items": {"type": "string"}},
            "product_proof_points": {"type": "array", "items": {"type": "string"}},
            "sales_fold": {"$ref": "SalesFoldSpec.schema.json"},
            "version": {"type": "string"}
        },
        "additionalProperties": False
    },
    "VisualEvidenceManifest.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "VisualEvidenceManifest",
        "type": "object",
        "required": ["manifest_id", "run_id", "viewports_captured", "screenshot_paths"],
        "properties": {
            "manifest_id": {"type": "string"},
            "run_id": {"type": "string"},
            "viewports_captured": {"type": "array", "items": {"type": "integer"}},
            "screenshot_paths": {"type": "object"},
            "horizontal_overflow_detected": {"type": "object"},
            "cta_visible": {"type": "object"},
            "captured_at": {"type": "string"}
        },
        "additionalProperties": False
    },
    "CritiqueFinding.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CritiqueFinding",
        "type": "object",
        "required": ["finding_id", "critic_name", "verdict", "dimension", "title", "details"],
        "properties": {
            "finding_id": {"type": "string"},
            "critic_name": {"type": "string"},
            "verdict": {"type": "string", "enum": ["PASS", "WARN", "FAIL"]},
            "dimension": {"type": "string"},
            "title": {"type": "string"},
            "details": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_fix": {"type": ["string", "null"]}
        },
        "additionalProperties": False
    },
    "CritiqueScorecard.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CritiqueScorecard",
        "type": "object",
        "required": ["scorecard_id", "project_id", "overall_verdict", "critic_findings"],
        "properties": {
            "scorecard_id": {"type": "string"},
            "project_id": {"type": "string"},
            "overall_verdict": {"type": "string", "enum": ["PASS", "WARN", "FAIL"]},
            "critic_findings": {"type": "array", "items": {"$ref": "CritiqueFinding.schema.json"}},
            "scored_at": {"type": "string"}
        },
        "additionalProperties": False
    },
    "HumanDesignFeedback.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "HumanDesignFeedback",
        "type": "object",
        "required": ["feedback_id", "project_id", "source_attribution", "rating", "target_element"],
        "properties": {
            "feedback_id": {"type": "string"},
            "project_id": {"type": "string"},
            "source_attribution": {"type": "string"},
            "rating": {"type": "string"},
            "target_element": {"type": "string"},
            "comments": {"type": ["string", "null"]},
            "is_reversible": {"type": "boolean"},
            "recorded_at": {"type": "string"}
        },
        "additionalProperties": False
    },
    "DesignPreferenceProfile.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DesignPreferenceProfile",
        "type": "object",
        "required": ["profile_id", "project_id"],
        "properties": {
            "profile_id": {"type": "string"},
            "project_id": {"type": "string"},
            "liked_principles": {"type": "array", "items": {"type": "string"}},
            "disliked_anti_patterns": {"type": "array", "items": {"type": "string"}},
            "feedback_history": {"type": "array", "items": {"$ref": "HumanDesignFeedback.schema.json"}}
        },
        "additionalProperties": False
    },
    "ProductDemoVideoSpec.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ProductDemoVideoSpec",
        "type": "object",
        "required": ["spec_id", "title", "duration_seconds", "scene_script"],
        "properties": {
            "spec_id": {"type": "string"},
            "title": {"type": "string"},
            "duration_seconds": {"type": "number"},
            "scene_script": {"type": "array", "items": {"type": "object"}},
            "renderer_adapter_type": {"type": "string"},
            "version": {"type": "string"}
        },
        "additionalProperties": False
    },
    "DesignRecommendation.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DesignRecommendation",
        "type": "object",
        "required": ["recommendation_id", "project_id", "recommended_dna", "product_story"],
        "properties": {
            "recommendation_id": {"type": "string"},
            "project_id": {"type": "string"},
            "recommended_dna": {"$ref": "DesignDNA.schema.json"},
            "product_story": {"$ref": "ProductStorySpec.schema.json"},
            "provenance_feedback_ids": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"}
        },
        "additionalProperties": False
    },
    "BenchmarkResult.schema.json": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "BenchmarkResult",
        "type": "object",
        "required": ["benchmark_id", "fixture_name", "passed", "detected_failures", "false_positives", "false_negatives", "duration_seconds"],
        "properties": {
            "benchmark_id": {"type": "string"},
            "fixture_name": {"type": "string"},
            "passed": {"type": "boolean"},
            "detected_failures": {"type": "array", "items": {"type": "string"}},
            "false_positives": {"type": "array", "items": {"type": "string"}},
            "false_negatives": {"type": "array", "items": {"type": "string"}},
            "duration_seconds": {"type": "number"}
        },
        "additionalProperties": False
    }
}

def generate_all():
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    for filename, content in SCHEMAS.items():
        filepath = os.path.join(SCHEMA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(content, f, indent=2)
            f.write("\n")
    print(f"Generated {len(SCHEMAS)} JSON Schema files in {SCHEMA_DIR}")

if __name__ == "__main__":
    generate_all()
