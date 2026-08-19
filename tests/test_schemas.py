"""Unit tests for AOS JSON Schemas and deterministic validator."""

from pathlib import Path
import pytest
from aos.validate import validate_document, validate_file, TYPE_TO_SCHEMA

FIXTURES_DIR = Path(__file__).parent / "fixtures"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"
CANONICAL_DIR = Path(__file__).resolve().parent.parent / "docs" / "project-control"


class TestValidFixtures:
    @pytest.mark.parametrize(
        "doc_type,fixture_file",
        [
            ("state", "state.valid.json"),
            ("project_descriptor", "project_descriptor.valid.json"),
            ("task", "task.valid.json"),
            ("lease", "lease.valid.json"),
            ("evidence", "evidence.valid.json"),
            ("decision_event", "decision_event.valid.json"),
            ("escalation", "escalation.valid.json"),
        ],
    )
    def test_valid_fixtures_pass(self, doc_type: str, fixture_file: str):
        path = VALID_DIR / fixture_file
        res, code = validate_file(doc_type, path)
        assert code == 0, f"Expected {fixture_file} to pass validation, got errors: {[e.message for e in res.errors]}"
        assert res.is_valid is True
        assert len(res.errors) == 0


class TestInvalidFixtures:
    @pytest.mark.parametrize(
        "doc_type,fixture_file,expected_error_substr",
        [
            ("state", "state.missing_version.json", "'schema_version' is a required property"),
            ("task", "task.invalid_risk_class.json", "'R99_SUPER_HIGH' is not one of"),
            ("task", "task.malformed_sha.json", "does not match '^[0-9a-f]{40}$'"),
            ("task", "task.missing_scope.json", "'allowed_scope' is a required property"),
            ("lease", "lease.missing_expiry.json", "'expires_at' is a required property"),
            ("evidence", "evidence.malformed_timestamp.json", "is not a 'date-time'"),
            ("evidence", "evidence.missing_revision_proven.json", "is not valid under any of the given schemas"),
            ("project_descriptor", "project_descriptor.missing_identity.json", "'project_id' is a required property"),
            ("decision_event", "decision_event.missing_version.json", "'schema_version' is a required property"),
            ("escalation", "escalation.missing_required_decision.json", "'required_decision' is a required property"),
        ],
    )
    def test_invalid_fixtures_rejected(
        self, doc_type: str, fixture_file: str, expected_error_substr: str
    ):
        path = INVALID_DIR / fixture_file
        res, code = validate_file(doc_type, path)
        assert code != 0, f"Expected {fixture_file} to fail validation"
        assert res.is_valid is False
        assert len(res.errors) > 0
        all_messages = " ".join([e.message for e in res.errors])
        assert expected_error_substr in all_messages, f"Expected '{expected_error_substr}' in '{all_messages}'"


class TestCanonicalProjectControlData:
    def test_canonical_state_is_valid(self):
        state_file = CANONICAL_DIR / "STATE.json"
        res, code = validate_file("state", state_file)
        assert code == 0, f"STATE.json failed validation: {[e.message for e in res.errors]}"

    def test_canonical_evidence_is_valid(self):
        evidence_file = CANONICAL_DIR / "EVIDENCE.jsonl"
        res, code = validate_file("evidence", evidence_file)
        assert code == 0, f"EVIDENCE.jsonl failed validation: {[e.message for e in res.errors]}"