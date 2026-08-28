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
            ("control_request", "control_request.valid.json"),
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
            ("project_descriptor", "project_descriptor.missing_control_ref.json", "'control_ref' is a required property"),
            ("decision_event", "decision_event.missing_version.json", "'schema_version' is a required property"),
            ("escalation", "escalation.missing_required_decision.json", "'required_decision' is a required property"),
            ("control_request", "control_request.force_pass.json", "is not one of"),
            ("control_request", "control_request.missing_type.json", "'request_type' is a required property"),
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


class TestUnknownFieldRejection:
    """Core contract objects must reject unknown top-level and nested fields."""

    @pytest.mark.parametrize("doc_type,valid_fixture", [
        ("state", "state.valid.json"),
        ("task", "task.valid.json"),
        ("lease", "lease.valid.json"),
        ("evidence", "evidence.valid.json"),
        ("decision_event", "decision_event.valid.json"),
        ("escalation", "escalation.valid.json"),
        ("project_descriptor", "project_descriptor.valid.json"),
    ])
    def test_unknown_top_level_field_rejected(self, doc_type: str, valid_fixture: str):
        import json
        path = VALID_DIR / valid_fixture
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_unknown_bogus_field"] = "should be rejected"
        res = validate_document(doc_type, data)
        assert res.is_valid is False, f"{doc_type} should reject unknown top-level field"
        assert any("_unknown_bogus_field" in e.message for e in res.errors)

    def test_unknown_nested_field_in_task_scope_rejected(self):
        import json
        path = VALID_DIR / "task.valid.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["allowed_scope"]["_bogus_nested"] = True
        res = validate_document("task", data)
        assert res.is_valid is False, "task.allowed_scope should reject unknown nested field"

    def test_unknown_nested_field_in_lease_heartbeat_rejected(self):
        import json
        path = VALID_DIR / "lease.valid.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["heartbeat"]["_bogus_heartbeat_field"] = 999
        res = validate_document("lease", data)
        assert res.is_valid is False, "lease.heartbeat should reject unknown nested field"


class TestExtensionsAccepted:
    """Extensions object must accept arbitrary fields where explicitly supported."""

    @pytest.mark.parametrize("doc_type,valid_fixture", [
        ("task", "task.valid.json"),
        ("lease", "lease.valid.json"),
        ("evidence", "evidence.valid.json"),
        ("decision_event", "decision_event.valid.json"),
        ("escalation", "escalation.valid.json"),
        ("project_descriptor", "project_descriptor.valid.json"),
        ("state", "state.valid.json"),
    ])
    def test_extensions_object_accepted(self, doc_type: str, valid_fixture: str):
        import json
        path = VALID_DIR / valid_fixture
        data = json.loads(path.read_text(encoding="utf-8"))
        data["extensions"] = {"custom_field": "allowed", "nested": {"deep": True}}
        res = validate_document(doc_type, data)
        assert res.is_valid is True, f"{doc_type} should accept extensions: {[e.message for e in res.errors]}"


class TestSchemaMetaValidation:
    """All seven AOS schemas must satisfy AOS schema metadata requirements and Draft 2020-12 meta-validation."""

    ALL_SCHEMAS = [
        "state.schema.json",
        "project_descriptor.schema.json",
        "task.schema.json",
        "lease.schema.json",
        "evidence.schema.json",
        "decision_event.schema.json",
        "escalation.schema.json",
        "planner_decision.schema.json",
        "shadow_trace.schema.json",
        "canonical_project_snapshot.schema.json",
        "shadow_expectation.schema.json",
        "planner_routing_policy.schema.json",
        "control_request.schema.json",
        "controlled_execution_result.schema.json",
        "worker_capability_attestation.schema.json",
    ]

    @pytest.mark.parametrize("schema_file", ALL_SCHEMAS)
    def test_schema_is_valid_draft_2020_12(self, schema_file: str):
        import json
        from jsonschema import Draft202012Validator
        from aos.validate import SCHEMA_DIR
        schema_path = SCHEMA_DIR / schema_file
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize("schema_file", ALL_SCHEMAS)
    def test_schema_metadata_identity(self, schema_file: str):
        import json
        from aos.validate import SCHEMA_DIR
        schema_path = SCHEMA_DIR / schema_file
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        # 1. $schema exists
        assert "$schema" in schema, f"$schema missing in {schema_file}"
        # 2. $schema equals exact draft 2020-12 URL
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", f"Incorrect $schema in {schema_file}"

        # 3. $id exists
        assert "$id" in schema, f"$id missing in {schema_file}"
        # 4. $id equals exact expected URI
        expected_id = f"https://schemas.mertsgi.org/aos/v0.1/{schema_file}"
        assert schema["$id"] == expected_id, f"Incorrect $id in {schema_file}, expected {expected_id}"

        # 6. No empty-string top-level property key
        assert "" not in schema, f"Empty-string top-level property key found in {schema_file}"

    def test_schema_ids_unique(self):
        import json
        from aos.validate import SCHEMA_DIR
        ids = set()
        for schema_file in self.ALL_SCHEMAS:
            schema_path = SCHEMA_DIR / schema_file
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema_id = schema.get("$id")
            assert schema_id not in ids, f"Duplicate $id '{schema_id}' found in {schema_file}"
            ids.add(schema_id)
        assert len(ids) == len(self.ALL_SCHEMAS)

    def test_openai_strict_structured_output_compatibility(self):
        """Prove planner_decision.schema.json satisfies OpenAI strict Structured Outputs requirements."""
        import json
        from jsonschema import Draft202012Validator
        from aos.validate import SCHEMA_DIR

        path = SCHEMA_DIR / "planner_decision.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))

        Draft202012Validator.check_schema(schema)
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False

        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        # Every property MUST be listed in required
        for prop_name in properties.keys():
            assert prop_name in required, f"Property '{prop_name}' must be listed in required for OpenAI strict mode"

        # Check nested objects recursively
        def check_node(node, name):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    assert node.get("additionalProperties") is False, f"Object node '{name}' must have additionalProperties=false"
                    node_props = node.get("properties", {})
                    node_req = set(node.get("required", []))
                    for k in node_props.keys():
                        assert k in node_req, f"Nested property '{k}' in '{name}' must be listed in required"
                    for k, v in node_props.items():
                        check_node(v, f"{name}.{k}")
                elif node.get("type") == "array" and "items" in node:
                    check_node(node["items"], f"{name}[items]")

        for prop_name, prop_val in properties.items():
            check_node(prop_val, prop_name)


class TestStrictDuplicateJSONKeyRejection:
    """Regression tests for strict duplicate JSON key detection in validate_file."""

    def test_top_level_duplicate_key_rejected(self, tmp_path):
        json_content = '{\n  "schema_version": "0.1.0",\n  "schema_version": "0.1.0"\n}'
        f = tmp_path / "dup_top.json"
        f.write_text(json_content, encoding="utf-8")
        res, code = validate_file("state", f)
        assert code != 0
        assert res.is_valid is False
        assert len(res.errors) == 1
        assert res.errors[0].validator == "duplicate_json_key"
        assert "Duplicate JSON object key: schema_version" in res.errors[0].message

    def test_nested_duplicate_key_rejected(self, tmp_path):
        json_content = '{\n  "schema_version": "0.1.0",\n  "project": {\n    "name": "AOS",\n    "name": "AOS_DUP"\n  }\n}'
        f = tmp_path / "dup_nested.json"
        f.write_text(json_content, encoding="utf-8")
        res, code = validate_file("state", f)
        assert code != 0
        assert res.is_valid is False
        assert len(res.errors) == 1
        assert res.errors[0].validator == "duplicate_json_key"
        assert "Duplicate JSON object key: name" in res.errors[0].message

    def test_jsonl_line_duplicate_key_rejected(self, tmp_path):
        jsonl_content = '{"schema_version": "0.1.0", "evidence_id": "AOS-EV-0001"}\n{"schema_version": "0.1.0", "schema_version": "0.1.0"}'
        f = tmp_path / "dup.jsonl"
        f.write_text(jsonl_content, encoding="utf-8")
        res, code = validate_file("evidence", f)
        assert code != 0
        assert res.is_valid is False
        assert len(res.errors) == 1
        assert res.errors[0].validator == "duplicate_json_key"

    def test_canonical_state_strict_passes(self):
        state_file = CANONICAL_DIR / "STATE.json"
        res, code = validate_file("state", state_file)
        assert code == 0, f"STATE.json failed strict validation: {[e.message for e in res.errors]}"

    def test_canonical_evidence_strict_passes(self):
        evidence_file = CANONICAL_DIR / "EVIDENCE.jsonl"
        res, code = validate_file("evidence", evidence_file)
        assert code == 0, f"EVIDENCE.jsonl failed strict validation: {[e.message for e in res.errors]}"

    def test_exact_historical_collision_pattern_rejected(self, tmp_path):
        collision_json = '''{
          "current_machine_capability_independent_review_status": "PENDING_CURRENT_IDENTITY_REVIEW",
          "current_machine_capability_independent_review_status": "INDEPENDENTLY_ACCEPTED"
        }'''
        f = tmp_path / "collision.json"
        f.write_text(collision_json, encoding="utf-8")
        res, code = validate_file("state", f)
        assert code != 0
        assert res.is_valid is False
        assert res.errors[0].validator == "duplicate_json_key"

    def test_state_raw_text_key_uniqueness(self):
        import json
        state_path = CANONICAL_DIR / "STATE.json"
        state_text = state_path.read_text(encoding="utf-8")
        key_target = '"current_machine_capability_independent_review_status"'
        count = state_text.count(key_target)
        assert count == 1, f"Expected exactly 1 occurrence of {key_target} in STATE.json, got {count}"
