import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest
import jsonschema

CONTRACTS_DIR = Path(__file__).resolve().parent

def load_json(filename):
    with open(CONTRACTS_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)

@pytest.fixture
def request_data():
    return load_json("aos6_controlled_pilot_request.json")

@pytest.fixture
def request_schema():
    return load_json("aos6_controlled_pilot_request.schema.json")

@pytest.fixture
def report_schema():
    return load_json("aos6_controlled_pilot_report.schema.json")

@pytest.fixture
def manifest_schema():
    return load_json("aos6_controlled_pilot_runtime_manifest.schema.json")

@pytest.fixture
def attestation_schema():
    return load_json("aos6_controlled_pilot_attestation.schema.json")


class TestAOS6ControlledPilotContracts:

    def test_exact_request_accepts_authorized_sha(self, request_data, request_schema):
        jsonschema.validate(instance=request_data, schema=request_schema)
        assert request_data["source_sha"] == "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"

    def test_wrong_sha_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["source_sha"] = "0000000000000000000000000000000000000000"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_branch_ref_substitution_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["source_sha"] = "feature/lari-health-tourism-slice3-lead-ops-ai-assist"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_attempt_limit_greater_than_one_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["attempt_limit"] = 2
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_retry_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["automatic_retry_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_canonical_mutation_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["canonical_lari_mutation_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_stage12c_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["stage12c_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_production_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["production_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_real_customer_data_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["real_customer_data_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_external_communications_true_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["real_external_communications_allowed"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_unknown_field_rejected(self, request_data, request_schema):
        bad_data = copy.deepcopy(request_data)
        bad_data["arbitrary_shell_command"] = "rm -rf /"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_data, schema=request_schema)

    def test_workflow_definition_dispatch_only(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in content
        assert "push:" not in content
        assert "pull_request:" not in content
        assert "schedule:" not in content
        assert "permissions:" in content
        assert "contents: read" in content
        assert "secrets." not in content

    def test_driver_script_isolation_contract(self):
        driver_path = Path(__file__).resolve().parent.parent / "scripts" / "aos6_controlled_pilot_driver.mjs"
        content = driver_path.read_text(encoding="utf-8")
        assert "isProviderReplyGrounded" in content
        assert "buildGroundedReplacementResponse" in content
        assert "executeProviderCall" in content
        assert "fetchOverride" in content
        assert "AI_PROVIDER_UNAVAILABLE" in content
        assert "503" in content

    def test_secret_scrubber_logic(self):
        from scripts.aos6_controlled_pilot_harness import sanitize_env
        host_env = {
            "PATH": "/usr/bin",
            "SUPABASE_SERVICE_ROLE_KEY": "secret123",
            "OPENAI_API_KEY": "sk-secret",
            "VERCEL_TOKEN": "v-secret",
            "SAFE_VAR": "safe_value"
        }
        cleaned = sanitize_env(host_env)
        assert "PATH" in cleaned
        assert "SAFE_VAR" in cleaned
        assert "SUPABASE_SERVICE_ROLE_KEY" not in cleaned
        assert "OPENAI_API_KEY" not in cleaned
        assert "VERCEL_TOKEN" not in cleaned
