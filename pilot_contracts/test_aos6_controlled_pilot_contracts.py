import copy
import hashlib
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


class MockCommandResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeCommandRunner:
    def __init__(self):
        self.commands = []
        self.remotes_active = True
        self.git_clean = True
        self.fetch_head_sha = "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"
        self.head_sha = "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"
        self.tree_sha = "1111111111111111111111111111111111111111"
        self.tracked_files = ["package.json", "package-lock.json", "scripts/test-health-tourism-slice3-lead-ops-ai-assist.mjs", "supabase/functions/ht-ai-chat/provider-policy.ts"]
        self.docker_rm_returncode = 0
        self.docker_inspect_absent = True
        self.driver_output_json = {
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "PASS",
            "policy_module_boot_result": "PASS",
            "unsafe_grounding_result": "PASS",
            "safe_grounding_result": "PASS",
            "localization_result": "PASS",
            "no_key_provider_result": "PASS",
            "mock_provider_success_result": "PASS",
            "mock_provider_failure_result": "PASS",
            "mock_provider_call_count": 2,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "PASS"
        }

    def run(self, cmd, cwd=None, env=None, check=True):
        self.commands.append((cmd, cwd, env))
        cmd_str = " ".join(cmd)

        if cmd[0] == "git":
            if "checkout" in cmd:
                # Create dummy files in source dir
                if cwd:
                    for rel in self.tracked_files:
                        p = Path(cwd) / rel
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(f"dummy content for {rel}", encoding="utf-8")
                return MockCommandResult()
            if "rev-parse" in cmd:
                if "FETCH_HEAD" in cmd:
                    return MockCommandResult(stdout=self.fetch_head_sha + "\n")
                if "HEAD^{tree}" in cmd:
                    return MockCommandResult(stdout=self.tree_sha + "\n")
                return MockCommandResult(stdout=self.head_sha + "\n")
            if "status" in cmd:
                status_txt = "" if self.git_clean else "M file.txt\n"
                return MockCommandResult(stdout=status_txt)
            if "remote" in cmd:
                if "remove" in cmd:
                    self.remotes_active = False
                    return MockCommandResult()
                remotes_txt = "source\n" if self.remotes_active else ""
                return MockCommandResult(stdout=remotes_txt)
            if "ls-files" in cmd:
                return MockCommandResult(stdout="\0".join(self.tracked_files) + "\0")
            return MockCommandResult()

        if cmd[0] == "npm":
            return MockCommandResult(stdout="npm ci OK\n")

        if cmd[0] == "docker":
            if cmd[1] == "pull":
                return MockCommandResult(stdout="Pulled image\n")
            if cmd[1] == "create":
                return MockCommandResult(stdout="container123\n")
            if cmd[1] == "inspect":
                if "node:22-bookworm-slim" in cmd:
                    if "--format={{.Id}}" in cmd:
                        return MockCommandResult(stdout="sha256:targetimageid123\n")
                    return MockCommandResult(stdout="node@sha256:repodigest123\n")
                if "aos6-pilot-" in cmd_str:
                    if self.docker_inspect_absent and ("rm" in self.commands[-2][0] or "rm" in self.commands[-3][0] if len(self.commands)>=3 else False):
                        return MockCommandResult(stdout="", stderr="No such container", returncode=1)
                    # Inspect container active
                    inspect_obj = [{
                        "HostConfig": {
                            "NetworkMode": "none",
                            "ReadonlyRootfs": True,
                            "PidsLimit": 100,
                            "CapDrop": ["ALL"],
                            "SecurityOpt": ["no-new-privileges"]
                        },
                        "Mounts": [
                            {"Destination": "/workspace", "RW": False, "Source": "/tmp/ws"},
                            {"Destination": "/aos-driver/aos6_controlled_pilot_driver.mjs", "RW": False, "Source": "/tmp/drv"}
                        ]
                    }]
                    return MockCommandResult(stdout=json.dumps(inspect_obj))
            if cmd[1] == "start":
                out_line = f"AOS6_PILOT_DRIVER_RESULT={json.dumps(self.driver_output_json)}\n"
                return MockCommandResult(stdout=out_line, returncode=0)
            if cmd[1] == "rm":
                return MockCommandResult(stdout="", returncode=self.docker_rm_returncode)

        return MockCommandResult()


class TestAOS6ControlledPilotContracts:

    # 1. REQUEST CONTRACT TESTS
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

    # 2. WORKFLOW DEFINITION TESTS
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

    # 3. DRIVER CONTRACT & API TESTS
    def test_driver_script_exact_api_names(self):
        driver_path = Path(__file__).resolve().parent.parent / "scripts" / "aos6_controlled_pilot_driver.mjs"
        content = driver_path.read_text(encoding="utf-8")

        # Exact API names check
        assert "isProviderReplyGrounded" in content
        assert "buildGroundedReplacementResponse" in content
        assert "executeProviderCall" in content
        assert "aiApiKey" in content
        assert "fetchImpl" in content
        assert "buildSystemPrompt" in content
        assert "success" in content
        assert "rawReply" in content
        assert "errorCode" in content
        assert "statusCode" in content

        # Legacy wrong API names absent
        assert "fetchOverride" not in content
        assert "result.status" not in content
        assert "result.error" not in content
        assert "result.content" not in content

    def test_driver_script_lowercase_locales(self):
        driver_path = Path(__file__).resolve().parent.parent / "scripts" / "aos6_controlled_pilot_driver.mjs"
        content = driver_path.read_text(encoding="utf-8")
        assert "['en', 'tr', 'de', 'ru', 'ar']" in content

    def test_driver_script_global_fetch_poison(self):
        driver_path = Path(__file__).resolve().parent.parent / "scripts" / "aos6_controlled_pilot_driver.mjs"
        content = driver_path.read_text(encoding="utf-8")
        assert "REAL_PROVIDER_NETWORK_PATH_FORBIDDEN" in content

    # 4. SECRET SCRUBBER TESTS
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

    def test_minimal_container_env_has_no_secrets(self):
        from scripts.aos6_controlled_pilot_harness import build_minimal_container_env, FORBIDDEN_ENV_KEYWORDS
        min_env = build_minimal_container_env()
        for k in min_env.keys():
            assert not any(keyword in k.upper() for keyword in FORBIDDEN_ENV_KEYWORDS)

    # 5. SOURCE MANIFEST & IMMUTABILITY TESTS
    def test_tracked_source_manifest_building(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import build_tracked_source_manifest
        (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file2.txt").write_text("content2", encoding="utf-8")

        fake_runner = FakeCommandRunner()
        fake_runner.tracked_files = ["file1.txt", "sub/file2.txt"]

        entries, sha256_hash = build_tracked_source_manifest(tmp_path, fake_runner)
        assert "file1.txt" in entries
        assert "sub/file2.txt" in entries
        assert len(sha256_hash) == 64

    def test_tracked_source_manifest_fails_on_symlink(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import build_tracked_source_manifest
        fake_file = tmp_path / "real.txt"
        fake_file.write_text("hello", encoding="utf-8")
        link_file = tmp_path / "link.txt"
        try:
            link_file.symlink_to(fake_file)
        except OSError:
            pytest.skip("Symlinks not supported on this OS/user privilege")

        fake_runner = FakeCommandRunner()
        fake_runner.tracked_files = ["real.txt", "link.txt"]

        with pytest.raises(ValueError, match="Unsafe symlink"):
            build_tracked_source_manifest(tmp_path, fake_runner)

    # 6. REPORT / MANIFEST BINDING & ATTACK DEFENSE TESTS
    def test_report_manifest_cryptographic_binding(self, tmp_path, report_schema, manifest_schema):
        from scripts.aos6_controlled_pilot_harness import write_json_deterministic

        manifest_obj = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "target_image_name": "node:22-bookworm-slim",
            "target_image_id": "sha256:abc",
            "target_repo_digest": "node@sha256:def",
            "container_name": "aos6-c1",
            "container_inspection": {
                "network_mode": "none",
                "readonly_rootfs": True,
                "pids_limit": 100,
                "cap_drop_has_all": True,
                "no_new_privileges": True,
                "docker_socket_mount_count": 0,
                "credential_directory_mount_count": 0,
                "workspace_source_readonly": True
            },
            "source_immutability": {
                "original_source_tree_sha256_pre": "0"*64,
                "original_source_tree_sha256_post": "0"*64,
                "immutable": True
            },
            "dependency_preparation": {
                "location": "DISPOSABLE_WORKSPACE_COPY",
                "command": "npm ci --ignore-scripts --no-audit --no-fund",
                "result": "PASS",
                "lifecycle_scripts_disabled": True
            },
            "workflow_execution": {
                "step_p1_static_qa": "PASS",
                "step_p2_policy_boot": "PASS",
                "step_p3_grounded_policy_matrix": {
                    "unsafe_promise_rejected": True,
                    "safe_request_accepted": True,
                    "localized_responses_produced": True,
                    "missing_key_503_produced": True,
                    "mock_fetch_success_produced": True,
                    "mock_fetch_exception_503_produced": True
                }
            },
            "cleanup_verification": {
                "docker_rm_return_code": 0,
                "post_cleanup_absence_proven": True,
                "surviving_resource_count": 0
            }
        }
        jsonschema.validate(manifest_obj, manifest_schema)
        manifest_bytes = write_json_deterministic(tmp_path / "pilot_runtime_manifest.json", manifest_obj)
        actual_sha = hashlib.sha256(manifest_bytes).hexdigest()

        report_obj = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "aos_canonical_binding_sha": "a"*40,
            "lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
            "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL",
            "aos6_controlled_pilot_result": "PASS",
            "exact_sha_result": "PASS",
            "environment_isolation_result": "PASS",
            "synthetic_data_only_result": "PASS",
            "runtime_boot_result": "PASS",
            "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE",
            "bounded_workflow_result": "PASS",
            "evidence_capture_result": "PASS",
            "cleanup_result": "PASS",
            "source_mutation_count": 0,
            "canonical_lari_mutation_count": 0,
            "authorized_source_acquisition_count": 1,
            "canonical_remote_access_count": 0,
            "lari_e3_project_access_count": 0,
            "shared_staging_access_count": 0,
            "production_access_count": 0,
            "vercel_access_count": 0,
            "real_customer_data_access_count": 0,
            "real_whatsapp_send_count": 0,
            "real_sms_send_count": 0,
            "real_email_send_count": 0,
            "real_payment_count": 0,
            "real_provider_network_call_count": 0,
            "mock_provider_call_count": 2,
            "surviving_disposable_resource_count": 0,
            "attempt_count": 1,
            "retry_count": 0,
            "first_failed_step_if_any": None,
            "blocker_if_any": None,
            "stage12c_authority": "NOT_AUTHORIZED",
            "production_authority": "NO",
            "controller_review_required": True,
            "runtime_evidence_binding": {
                "manifest_filename": "pilot_runtime_manifest.json",
                "manifest_sha256": actual_sha,
                "manifest_schema_version": "0.1.0"
            }
        }
        jsonschema.validate(report_obj, report_schema)
        assert report_obj["runtime_evidence_binding"]["manifest_sha256"] == actual_sha

    # 7. SCHEMA TRUTHFULNESS & FAILURE REPRESENTATION
    def test_schema_supports_failure_counters(self, report_schema):
        bad_report = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "aos_canonical_binding_sha": "a"*40,
            "lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
            "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL",
            "aos6_controlled_pilot_result": "FAIL",
            "exact_sha_result": "FAIL",
            "environment_isolation_result": "FAIL",
            "synthetic_data_only_result": "PASS",
            "runtime_boot_result": "FAIL",
            "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE",
            "bounded_workflow_result": "FAIL",
            "evidence_capture_result": "PASS",
            "cleanup_result": "FAIL",
            "source_mutation_count": 2,
            "canonical_lari_mutation_count": 0,
            "authorized_source_acquisition_count": 1,
            "canonical_remote_access_count": 1,
            "lari_e3_project_access_count": 0,
            "shared_staging_access_count": 0,
            "production_access_count": 0,
            "vercel_access_count": 0,
            "real_customer_data_access_count": 0,
            "real_whatsapp_send_count": 0,
            "real_sms_send_count": 0,
            "real_email_send_count": 0,
            "real_payment_count": 0,
            "real_provider_network_call_count": 3,
            "mock_provider_call_count": 0,
            "surviving_disposable_resource_count": 1,
            "attempt_count": 1,
            "retry_count": 0,
            "first_failed_step_if_any": "DOCKER_INSPECT_FAILED",
            "blocker_if_any": "SecurityOpt missing no-new-privileges",
            "stage12c_authority": "NOT_AUTHORIZED",
            "production_authority": "NO",
            "controller_review_required": True,
            "runtime_evidence_binding": {
                "manifest_filename": "pilot_runtime_manifest.json",
                "manifest_sha256": "b"*64,
                "manifest_schema_version": "0.1.0"
            }
        }
        # Validate that the schema DOES NOT reject non-zero failure counters
        jsonschema.validate(bad_report, report_schema)

    def test_attestation_supports_non_pass_results(self, attestation_schema):
        att_obj = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "aos_sha": "a"*40,
            "authorized_lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
            "lari_source_tree_sha": "b"*40,
            "tracked_source_manifest_sha256": "c"*64,
            "report_sha256": "d"*64,
            "runtime_manifest_sha256": "e"*64,
            "aos_worktree_immutable_result": "NOT_CHECKED",
            "original_lari_source_immutable_result": "FAIL",
            "attestation_timestamp": "2026-09-01T13:00:00Z"
        }
        jsonschema.validate(att_obj, attestation_schema)

    # 8. HARNESS ORCHESTRATION WITH FAKE COMMAND RUNNER
    def test_harness_full_execution_mocked_success(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness

        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()

        # Execute harness using fake runner
        try:
            execute_harness(req_file, tmp_path, runner=fake_runner)
        except SystemExit as e:
            pytest.fail(f"Harness exited prematurely with code {e.code}")

        report_p = tmp_path / "pilot_report.json"
        manifest_p = tmp_path / "pilot_runtime_manifest.json"
        attest_p = tmp_path / "pilot_attestation.json"

        assert report_p.exists()
        assert manifest_p.exists()
        assert attest_p.exists()

        report = json.loads(report_p.read_text(encoding="utf-8"))
        assert report["aos6_controlled_pilot_result"] == "PASS"
        assert report["authorized_source_acquisition_count"] == 1
        assert report["canonical_remote_access_count"] == 0
        assert report["real_provider_network_call_count"] == 0
        assert report["mock_provider_call_count"] == 2
        assert report["surviving_disposable_resource_count"] == 0
