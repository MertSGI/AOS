"""Unit tests for 14 Design Intelligence JSON Schemas (Correction R1)."""

import pytest
import json
import os
from dataclasses import asdict
from extensions.design_intelligence.contracts import (
    DesignProjectBrief,
    DesignDNA,
    ReferenceSource,
    ReferenceSignal,
    ProductStorySpec,
    SalesFoldSpec,
    VisualEvidenceManifest,
    CritiqueFinding,
    CritiqueScorecard,
    JudgmentVerdict,
    HumanDesignFeedback,
    FeedbackRating,
    DesignPreferenceProfile,
    ProductDemoVideoSpec,
    DesignRecommendation,
    BenchmarkResult,
)

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "schemas", "design-intelligence")


def load_schema(schema_name: str) -> dict:
    path = os.path.join(SCHEMA_DIR, schema_name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_schema_files_exist_and_are_valid_json():
    expected_schemas = [
        "DesignProjectBrief.schema.json",
        "DesignDNA.schema.json",
        "ReferenceSource.schema.json",
        "ReferenceSignal.schema.json",
        "ProductStorySpec.schema.json",
        "SalesFoldSpec.schema.json",
        "VisualEvidenceManifest.schema.json",
        "CritiqueFinding.schema.json",
        "CritiqueScorecard.schema.json",
        "HumanDesignFeedback.schema.json",
        "DesignPreferenceProfile.schema.json",
        "ProductDemoVideoSpec.schema.json",
        "DesignRecommendation.schema.json",
        "BenchmarkResult.schema.json",
    ]
    for schema_file in expected_schemas:
        schema = load_schema(schema_file)
        assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "properties" in schema
        assert "required" in schema


def test_contracts_serialize_to_valid_json_schema_structures():
    brief = DesignProjectBrief(
        brief_id="b-101",
        project_id="p-1",
        tenant_name="Test Tenant",
        industry="SaaS",
        target_audience="Devs",
        core_job_to_be_done="Automate builds",
        brand_posture="Modern",
    )
    brief_dict = brief.__dict__
    brief_schema = load_schema("DesignProjectBrief.schema.json")
    for req in brief_schema["required"]:
        assert req in brief_dict

    scorecard = CritiqueScorecard(
        scorecard_id="sc-1",
        project_id="p-1",
        overall_verdict=JudgmentVerdict.PASS,
        critic_findings=[],
    )
    scorecard_dict = asdict(scorecard)
    scorecard_dict["overall_verdict"] = scorecard.overall_verdict.value
    scorecard_schema = load_schema("CritiqueScorecard.schema.json")
    for req in scorecard_schema["required"]:
        assert req in scorecard_dict
