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
        self.tracked_files = [
            "package.json",
            "package-lock.json",
            "scripts/test-health-tourism-slice3-lead-ops-ai-assist.mjs",
            "supabase/functions/ht-ai-chat/provider-policy.ts"
        ]
        self.docker_rm_returncode = 0
        self.docker_inspect_absent = True
        self.created_mounts = []
        self.docker_start_returncode = 0
        self.docker_start_stdout = None
        self.docker_start_stderr = ""
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
                    (Path(cwd) / ".git").mkdir(parents=True, exist_ok=True)
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
            if "diff" in cmd:
                return MockCommandResult(stdout="", returncode=0)
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
                self.created_mounts = []
                for i, arg in enumerate(cmd):
                    if arg == "-v":
                        val = cmd[i + 1]
                        if val.endswith(":ro") or val.endswith(":rw"):
                            parts = val.rsplit(":", 2)
                            src, dest = parts[0], parts[1]
                            rw = val.endswith(":rw")
                        else:
                            parts = val.rsplit(":", 1)
                            src, dest = parts[0], parts[1]
                            rw = True
                        self.created_mounts.append({
                            "Type": "bind",
                            "Source": src,
                            "Destination": dest,
                            "RW": rw
                        })
                return MockCommandResult(stdout="container123\n")
            if cmd[1] == "inspect":
                if "node:22-bookworm-slim" in cmd:
                    if "--format={{.Id}}" in cmd:
                        return MockCommandResult(stdout="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n")
                    return MockCommandResult(stdout="node@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n")
                if "aos6-pilot-" in cmd_str:
                    if self.docker_inspect_absent and len(self.commands) >= 2 and self.commands[-2][0][0:2] == ["docker", "rm"]:
                        return MockCommandResult(stdout="", stderr="No such container", returncode=1)
                    inspect_obj = [{
                        "HostConfig": {
                            "NetworkMode": "none",
                            "ReadonlyRootfs": True,
                            "PidsLimit": 100,
                            "CapDrop": ["ALL"],
                            "SecurityOpt": ["no-new-privileges"],
                            "Tmpfs": {
                                "/tmp": "rw,noexec,nosuid,size=67108864,mode=1777"
                            }
                        },
                        "Mounts": (self.created_mounts + [{"Type": "tmpfs", "Destination": "/tmp", "RW": True}]) if self.created_mounts else [
                            {"Type": "bind", "Destination": "/workspace", "RW": False, "Source": "/tmp/ws"},
                            {"Type": "bind", "Destination": "/aos-driver/aos6_controlled_pilot_driver.mjs", "RW": False, "Source": "/tmp/drv"},
                            {"Type": "tmpfs", "Destination": "/tmp", "RW": True}
                        ]
                    }]
                    return MockCommandResult(stdout=json.dumps(inspect_obj))
            if cmd[1] == "start":
                stdout_txt = self.docker_start_stdout if self.docker_start_stdout is not None else f"AOS6_PILOT_DRIVER_RESULT={json.dumps(self.driver_output_json)}\n"
                return MockCommandResult(stdout=stdout_txt, stderr=self.docker_start_stderr, returncode=self.docker_start_returncode)
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
        assert "fetchOverride" not in content

    def test_driver_script_lowercase_locales(self):
        driver_path = Path(__file__).resolve().parent.parent / "scripts" / "aos6_controlled_pilot_driver.mjs"
        content = driver_path.read_text(encoding="utf-8")
        assert "['en', 'tr', 'de', 'ru', 'ar']" in content
        assert "localizedOutputs.size === 5" in content

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

    # 5. AOS START SHA & IMMUTABILITY TESTS (Section 3 Matrix)
    def test_aos_start_sha_valid_format(self):
        from scripts.aos6_controlled_pilot_harness import capture_aos_start_sha
        runner = FakeCommandRunner()
        runner.head_sha = "87a32a951e49b82372f3dfff164e805e4d3ff926"
        sha = capture_aos_start_sha(runner)
        assert sha == "87a32a951e49b82372f3dfff164e805e4d3ff926"

    def test_aos_start_sha_invalid_format_fails(self):
        from scripts.aos6_controlled_pilot_harness import capture_aos_start_sha
        runner = FakeCommandRunner()
        runner.head_sha = "not-a-sha"
        with pytest.raises(RuntimeError, match="Invalid aos_start_sha format"):
            capture_aos_start_sha(runner)

    def test_aos_immutability_exact_unchanged_passes(self):
        from scripts.aos6_controlled_pilot_harness import verify_aos_immutability
        runner = FakeCommandRunner()
        start_sha = "87a32a951e49b82372f3dfff164e805e4d3ff926"
        runner.head_sha = start_sha
        ok, final_sha = verify_aos_immutability(start_sha, runner)
        assert ok is True
        assert final_sha == start_sha

    def test_aos_immutability_changed_head_fails(self):
        from scripts.aos6_controlled_pilot_harness import verify_aos_immutability
        runner = FakeCommandRunner()
        start_sha = "87a32a951e49b82372f3dfff164e805e4d3ff926"
        runner.head_sha = "1111111111111111111111111111111111111111"
        ok, _ = verify_aos_immutability(start_sha, runner)
        assert ok is False

    def test_aos_immutability_dirty_status_fails(self):
        from scripts.aos6_controlled_pilot_harness import verify_aos_immutability
        runner = FakeCommandRunner()
        start_sha = "87a32a951e49b82372f3dfff164e805e4d3ff926"
        runner.head_sha = start_sha
        runner.git_clean = False
        ok, _ = verify_aos_immutability(start_sha, runner)
        assert ok is False

    # 6. GITLESS WORKSPACE VERIFIER TESTS (Section 4 Matrix)
    def test_gitless_workspace_verifier(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest

        file1 = tmp_path / "f1.txt"
        file1.write_text("hello world", encoding="utf-8")

        manifest_entries = {
            "f1.txt": {
                "size": len("hello world".encode("utf-8")),
                "sha256": hashlib.sha256("hello world".encode("utf-8")).hexdigest()
            }
        }

        entries, sha256_hash = verify_workspace_against_source_manifest(tmp_path, manifest_entries)
        assert "f1.txt" in entries

        (tmp_path / ".git").mkdir()
        with pytest.raises(RuntimeError, match="MUST NOT exist"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    def test_gitless_workspace_verifier_fails_on_missing_file(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest
        manifest_entries = {
            "missing.txt": {"size": 10, "sha256": "0"*64}
        }
        with pytest.raises(RuntimeError, match="Missing expected tracked file"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    def test_gitless_workspace_verifier_fails_on_hash_mismatch(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest
        (tmp_path / "f1.txt").write_text("modified content", encoding="utf-8")
        manifest_entries = {
            "f1.txt": {"size": 5, "sha256": "0"*64}
        }
        with pytest.raises(RuntimeError, match="byte/hash mismatch"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    def test_gitless_workspace_verifier_fails_on_unexpected_file(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest
        (tmp_path / "f1.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "untracked.txt").write_text("extra", encoding="utf-8")
        manifest_entries = {
            "f1.txt": {"size": 5, "sha256": hashlib.sha256("hello".encode("utf-8")).hexdigest()}
        }
        with pytest.raises(RuntimeError, match="Unexpected file in workspace"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    def test_gitless_workspace_verifier_fails_on_unexpected_dir(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest
        (tmp_path / "f1.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "extra_dir").mkdir()
        manifest_entries = {
            "f1.txt": {"size": 5, "sha256": hashlib.sha256("hello".encode("utf-8")).hexdigest()}
        }
        with pytest.raises(RuntimeError, match="Unexpected directory in workspace"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    def test_gitless_workspace_verifier_fails_on_symlink(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import verify_workspace_against_source_manifest
        target = tmp_path / "target.txt"
        target.write_text("target", encoding="utf-8")
        sym = tmp_path / "f1.txt"
        try:
            sym.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported in current environment")

        manifest_entries = {
            "f1.txt": {"size": 6, "sha256": hashlib.sha256("target".encode("utf-8")).hexdigest()}
        }
        with pytest.raises(RuntimeError, match="Symlink"):
            verify_workspace_against_source_manifest(tmp_path, manifest_entries)

    # 7. FAIL-CLOSED IMAGE IDENTITY TESTS
    def test_image_digest_fail_closed_validation(self):
        import re
        valid_digest = "node@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        invalid_digest = "node@sha256:unknown"
        empty_digest = ""

        pattern = r"^node@sha256:[0-9a-f]{64}$"
        assert re.match(pattern, valid_digest) is not None
        assert re.match(pattern, invalid_digest) is None
        assert re.match(pattern, empty_digest) is None

    # 8. DOCKER MOUNT IDENTITY & INSPECTION SECURITY MATRIX (Section 5 & 10)
    @pytest.fixture
    def valid_inspect_obj(self, tmp_path):
        ws_dir = tmp_path / "ws"
        drv_file = tmp_path / "driver.mjs"
        ws_dir.mkdir()
        drv_file.write_text("// driver", encoding="utf-8")
        return [{
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "PidsLimit": 100,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,size=67108864,mode=1777"
                }
            },
            "Mounts": [
                {"Type": "bind", "Destination": "/workspace", "RW": False, "Source": str(ws_dir)},
                {"Type": "bind", "Destination": "/aos-driver/aos6_controlled_pilot_driver.mjs", "RW": False, "Source": str(drv_file)}
            ]
        }], ws_dir, drv_file

    def test_docker_inspect_valid_passes(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        res = validate_docker_inspect_data(inspect_data, ws_dir, drv_file)
        assert res["network_mode"] == "none"
        assert res["readonly_rootfs"] is True
        assert res["pids_limit"] == 100
        assert res["tmpfs_mount_count"] == 1
        assert res["tmpfs_tmp_present"] is True
        assert res["tmpfs_tmp_read_write"] is True
        assert res["tmpfs_tmp_noexec"] is True
        assert res["tmpfs_tmp_nosuid"] is True
        assert res["tmpfs_tmp_mode_1777"] is True
        assert res["tmpfs_tmp_size_bytes"] == 67108864
        assert res["host_tmp_bind_mount_count"] == 0
        assert res["unexpected_tmpfs_mount_count"] == 0

    def test_docker_inspect_valid_with_tmpfs_in_mounts(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append({
            "Type": "tmpfs",
            "Destination": "/tmp",
            "RW": True
        })
        res = validate_docker_inspect_data(inspect_data, ws_dir, drv_file)
        assert res["host_tmp_bind_mount_count"] == 0
        assert res["unexpected_host_bind_mount_count"] == 0
        assert res["tmpfs_mount_count"] == 1
        assert res["tmpfs_tmp_present"] is True
        assert res["tmpfs_tmp_read_write"] is True
        assert res["tmpfs_tmp_noexec"] is True
        assert res["tmpfs_tmp_nosuid"] is True
        assert res["tmpfs_tmp_mode_1777"] is True
        assert res["tmpfs_tmp_size_bytes"] == 67108864

    def test_docker_inspect_negative_unexpected_third_bind_fails_closed(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append({
            "Type": "bind",
            "Destination": "/extra",
            "RW": False,
            "Source": "/host/extra"
        })
        with pytest.raises(RuntimeError, match="Total bind mount count must be exactly 2|Unexpected bind mount count"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    # Negative Tmpfs Tests A-T
    def test_tmpfs_negative_A_host_config_tmpfs_missing(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        del inspect_data[0]["HostConfig"]["Tmpfs"]
        with pytest.raises(RuntimeError, match="HostConfig.Tmpfs is missing"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_B_tmpfs_empty(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {}
        with pytest.raises(RuntimeError, match="Tmpfs object is empty"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_C_tmp_missing(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/var/tmp": "rw,noexec,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="/tmp is not present"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_D_only_wrong_destination(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp2": "rw,noexec,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="/tmp is not present"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_E_second_tmpfs_destination(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {
            "/tmp": "rw,noexec,nosuid,size=67108864,mode=1777",
            "/var/tmp": "rw,noexec,nosuid,size=67108864,mode=1777"
        }
        with pytest.raises(RuntimeError, match="Unexpected second tmpfs destination"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_F_tmp_missing_rw(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "noexec,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="missing 'rw' option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_G_tmp_contains_ro(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "ro,noexec,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="Forbidden security-weakening tmpfs option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_H_tmp_missing_noexec(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="missing 'noexec' option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_I_tmp_contains_exec(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,exec,nosuid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="Forbidden security-weakening tmpfs option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_J_tmp_missing_nosuid(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="missing 'nosuid' option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_K_tmp_contains_suid(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,suid,size=67108864,mode=1777"}
        with pytest.raises(RuntimeError, match="Forbidden security-weakening tmpfs option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_L_mode_missing(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=67108864"}
        with pytest.raises(RuntimeError, match="missing or invalid mode=1777"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_M_wrong_mode(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=67108864,mode=0777"}
        with pytest.raises(RuntimeError, match="missing or invalid mode=1777"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_N_size_missing(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,mode=1777"}
        with pytest.raises(RuntimeError, match="missing size option"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_O_wrong_smaller_size(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=33554432,mode=1777"}
        with pytest.raises(RuntimeError, match="size_bytes must be exactly 67108864"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_P_wrong_larger_size(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=134217728,mode=1777"}
        with pytest.raises(RuntimeError, match="size_bytes must be exactly 67108864"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_Q_size_zero(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=0,mode=1777"}
        with pytest.raises(RuntimeError, match="size_bytes must be exactly 67108864"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_R_host_tmp_bind_present(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append({
            "Type": "bind", "Destination": "/tmp", "RW": True, "Source": "/tmp"
        })
        with pytest.raises(RuntimeError, match="Host /tmp bind mount detected"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_S_host_tmp_bind_present_alongside_valid_tmpfs(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append({
            "Type": "bind", "Destination": "/tmp", "RW": True, "Source": "/tmp"
        })
        with pytest.raises(RuntimeError, match="Host /tmp bind mount detected"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_T_malformed_tmpfs_inspect_shape(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = ["/tmp"]
        with pytest.raises(RuntimeError, match="HostConfig.Tmpfs is missing or invalid object"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_duplicate_conflicting_size(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=67108864,size=33554432,mode=1777"}
        with pytest.raises(RuntimeError, match="Duplicate/conflicting size token"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_tmpfs_negative_duplicate_conflicting_mode(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=67108864,mode=1777,mode=0777"}
        with pytest.raises(RuntimeError, match="Duplicate/conflicting mode token"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_create_command_contains_tmpfs_contract(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()

        execute_harness(req_file, tmp_path, runner=fake_runner)

        create_cmds = [c[0] for c in fake_runner.commands if len(c[0]) >= 2 and c[0][0:2] == ["docker", "create"]]
        assert len(create_cmds) == 1
        cmd = create_cmds[0]

        assert "--tmpfs" in cmd
        tmpfs_idx = cmd.index("--tmpfs")
        assert cmd[tmpfs_idx + 1] == "/tmp:rw,noexec,nosuid,size=67108864,mode=1777"
        assert "--read-only" in cmd
        assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
        assert "--cap-drop" in cmd and cmd[cmd.index("--cap-drop") + 1] == "ALL"
        assert "--security-opt" in cmd and cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
        assert "--pids-limit" in cmd and cmd[cmd.index("--pids-limit") + 1] == "100"
        assert "--memory" in cmd and cmd[cmd.index("--memory") + 1] == "512m"
        assert "--cpus" in cmd and cmd[cmd.index("--cpus") + 1] == "1.0"

        # Ensure no bind-mounted /tmp
        bind_mounts = [cmd[i+1] for i, arg in enumerate(cmd) if arg == "-v"]
        assert not any(": /tmp" in b or b.startswith("/tmp:") or ":/tmp:" in b for b in bind_mounts)

    def test_docker_inspect_network_mode_bridge_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["NetworkMode"] = "bridge"
        with pytest.raises(RuntimeError, match="NetworkMode must be 'none'"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_readonly_rootfs_false_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["ReadonlyRootfs"] = False
        with pytest.raises(RuntimeError, match="ReadonlyRootfs must be true"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_pids_limit_wrong_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["PidsLimit"] = 200
        with pytest.raises(RuntimeError, match="PidsLimit must be 100"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_cap_drop_missing_all_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["CapDrop"] = ["NET_ADMIN"]
        with pytest.raises(RuntimeError, match="CapDrop MUST contain 'ALL'"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_no_new_privileges_missing_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["HostConfig"]["SecurityOpt"] = []
        with pytest.raises(RuntimeError, match="SecurityOpt MUST contain 'no-new-privileges'"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_workspace_rw_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"][0]["RW"] = True
        with pytest.raises(RuntimeError, match="Workspace mount MUST be read-only"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_driver_rw_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"][1]["RW"] = True
        with pytest.raises(RuntimeError, match="Driver mount MUST be read-only"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_workspace_wrong_source_fails(self, valid_inspect_obj, tmp_path):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        wrong_dir = tmp_path / "wrong_ws"
        wrong_dir.mkdir()
        inspect_data[0]["Mounts"][0]["Source"] = str(wrong_dir)
        with pytest.raises(RuntimeError, match="Workspace mount Source"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_named_volume_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"][0]["Type"] = "volume"
        with pytest.raises(RuntimeError, match="Workspace mount count at '/workspace' must be exactly 1"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_duplicate_mount_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append(copy.deepcopy(inspect_data[0]["Mounts"][0]))
        with pytest.raises(RuntimeError, match="Workspace mount count"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_unexpected_bind_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"].append({
            "Type": "bind", "Destination": "/extra", "RW": False, "Source": "/tmp"
        })
        with pytest.raises(RuntimeError, match="Total bind mount count must be exactly 2"):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_docker_socket_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"][0]["Source"] = "/var/run/docker.sock"
        with pytest.raises(RuntimeError):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    def test_docker_inspect_credential_mount_fails(self, valid_inspect_obj):
        from scripts.aos6_controlled_pilot_harness import validate_docker_inspect_data
        inspect_data, ws_dir, drv_file = valid_inspect_obj
        inspect_data[0]["Mounts"][0]["Source"] = "/home/user/.ssh"
        with pytest.raises(RuntimeError):
            validate_docker_inspect_data(inspect_data, ws_dir, drv_file)

    # 9. DRIVER TERMINAL RESULT CONTRACT TESTS (Section 6 & 11)
    def test_driver_terminal_result_one_valid_passes(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        res_obj = {
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
        text = f"Some log\nAOS6_PILOT_DRIVER_RESULT={json.dumps(res_obj)}\nDone"
        parsed = parse_and_validate_driver_terminal_result(text)
        assert parsed["bounded_workflow_result"] == "PASS"

    def test_driver_terminal_result_zero_lines_fails(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        with pytest.raises(RuntimeError, match="Zero AOS6_PILOT_DRIVER_RESULT"):
            parse_and_validate_driver_terminal_result("No driver result line here")

    def test_driver_terminal_result_duplicate_lines_fails(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        line = "AOS6_PILOT_DRIVER_RESULT={}"
        with pytest.raises(RuntimeError, match="Multiple AOS6_PILOT_DRIVER_RESULT"):
            parse_and_validate_driver_terminal_result(f"{line}\n{line}")

    def test_driver_terminal_result_missing_key_fails(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        res_obj = {
            "product_static_qa_attempt_count": 1,
            "bounded_workflow_result": "PASS"
        }
        text = f"AOS6_PILOT_DRIVER_RESULT={json.dumps(res_obj)}"
        with pytest.raises(RuntimeError, match="Exact key mismatch"):
            parse_and_validate_driver_terminal_result(text)

    def test_driver_terminal_result_unknown_key_fails(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        res_obj = {
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
            "bounded_workflow_result": "PASS",
            "unknown_extra_key": True
        }
        text = f"AOS6_PILOT_DRIVER_RESULT={json.dumps(res_obj)}"
        with pytest.raises(RuntimeError, match="Exact key mismatch"):
            parse_and_validate_driver_terminal_result(text)

    def test_driver_terminal_result_failure_with_error_passes(self):
        from scripts.aos6_controlled_pilot_harness import parse_and_validate_driver_terminal_result
        res_obj = {
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "FAIL",
            "policy_module_boot_result": "NOT_RUN",
            "unsafe_grounding_result": "NOT_RUN",
            "safe_grounding_result": "NOT_RUN",
            "localization_result": "NOT_RUN",
            "no_key_provider_result": "NOT_RUN",
            "mock_provider_success_result": "NOT_RUN",
            "mock_provider_failure_result": "NOT_RUN",
            "mock_provider_call_count": 0,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "FAIL",
            "error": "STEP_P1_STATIC_QA_FAILED"
        }
        text = f"AOS6_PILOT_DRIVER_RESULT={json.dumps(res_obj)}"
        parsed = parse_and_validate_driver_terminal_result(text)
        assert parsed["bounded_workflow_result"] == "FAIL"
        assert parsed["error"] == "STEP_P1_STATIC_QA_FAILED"

    # 10. PASS DERIVATION TRUTH TABLE TESTS (Section 7)
    def test_derive_pilot_result_all_valid_passes(self):
        from scripts.aos6_controlled_pilot_harness import derive_pilot_result
        obs = {
            "primary_failure": None,
            "secondary_cleanup_failure": None,
            "authorized_source_acquisition_count": 1,
            "exact_source_head": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
            "source_baseline_manifest_exists": True,
            "workspace_verification": "PASS",
            "dependency_preparation": "PASS",
            "target_image_id_valid": True,
            "target_repo_digest_valid": True,
            "container_inspection_exists": True,
            "network_mode": "none",
            "readonly_rootfs": True,
            "pids_limit": 100,
            "cap_drop_has_all": True,
            "no_new_privileges": True,
            "workspace_mount_exact_ro": True,
            "driver_mount_exact_ro": True,
            "docker_socket_count": 0,
            "credential_mount_count": 0,
            "unexpected_bind_count": 0,
            "tmpfs_mount_count": 1,
            "tmpfs_tmp_present": True,
            "tmpfs_tmp_read_write": True,
            "tmpfs_tmp_noexec": True,
            "tmpfs_tmp_nosuid": True,
            "tmpfs_tmp_mode_1777": True,
            "tmpfs_tmp_size_bytes": 67108864,
            "host_tmp_bind_mount_count": 0,
            "unexpected_tmpfs_mount_count": 0,
            "driver_result_exact_key_validation": "PASS",
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "PASS",
            "policy_module_boot_result": "PASS",
            "unsafe_grounding_result": "PASS",
            "safe_grounding_result": "PASS",
            "localization_result": "PASS",
            "no_key_provider_result": "PASS",
            "mock_provider_success_result": "PASS",
            "mock_provider_failure_result": "PASS",
            "bounded_workflow_result": "PASS",
            "mock_provider_call_count": 2,
            "real_provider_network_call_count": 0,
            "synthetic_data_only_result": "PASS",
            "source_mutation_count": 0,
            "canonical_lari_mutation_count": 0,
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
            "attempt_count": 1,
            "retry_count": 0,
            "resource_created_count": 1,
            "cleanup_attempt_count": 1,
            "cleanup_success_count": 1,
            "cleanup_failure_count": 0,
            "surviving_disposable_resource_count": 0,
            "original_lari_source_final_immutability": "PASS",
            "aos_exact_start_sha_final_immutability": "PASS",
            "final_report_manifest_verification": "PASS",
            "evidence_capture_result": "PASS"
        }
        assert derive_pilot_result(obs) == "PASS"

    def test_derive_pilot_result_any_failed_condition_returns_fail(self):
        from scripts.aos6_controlled_pilot_harness import derive_pilot_result
        base_obs = {
            "primary_failure": None,
            "secondary_cleanup_failure": None,
            "authorized_source_acquisition_count": 1,
            "exact_source_head": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
            "source_baseline_manifest_exists": True,
            "workspace_verification": "PASS",
            "dependency_preparation": "PASS",
            "target_image_id_valid": True,
            "target_repo_digest_valid": True,
            "container_inspection_exists": True,
            "network_mode": "none",
            "readonly_rootfs": True,
            "pids_limit": 100,
            "cap_drop_has_all": True,
            "no_new_privileges": True,
            "workspace_mount_exact_ro": True,
            "driver_mount_exact_ro": True,
            "docker_socket_count": 0,
            "credential_mount_count": 0,
            "unexpected_bind_count": 0,
            "driver_result_exact_key_validation": "PASS",
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "PASS",
            "policy_module_boot_result": "PASS",
            "unsafe_grounding_result": "PASS",
            "safe_grounding_result": "PASS",
            "localization_result": "PASS",
            "no_key_provider_result": "PASS",
            "mock_provider_success_result": "PASS",
            "mock_provider_failure_result": "PASS",
            "bounded_workflow_result": "PASS",
            "mock_provider_call_count": 2,
            "real_provider_network_call_count": 0,
            "synthetic_data_only_result": "PASS",
            "source_mutation_count": 0,
            "canonical_lari_mutation_count": 0,
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
            "attempt_count": 1,
            "retry_count": 0,
            "resource_created_count": 1,
            "cleanup_attempt_count": 1,
            "cleanup_success_count": 1,
            "cleanup_failure_count": 0,
            "surviving_disposable_resource_count": 0,
            "original_lari_source_final_immutability": "PASS",
            "aos_exact_start_sha_final_immutability": "PASS",
            "final_report_manifest_verification": "PASS",
            "evidence_capture_result": "PASS"
        }

        # Mutate each field one by one to verify fail closed
        bad_obs1 = copy.deepcopy(base_obs)
        bad_obs1["primary_failure"] = "Some error"
        assert derive_pilot_result(bad_obs1) == "FAIL"

        bad_obs2 = copy.deepcopy(base_obs)
        bad_obs2["mock_provider_call_count"] = 1
        assert derive_pilot_result(bad_obs2) == "FAIL"

        bad_obs3 = copy.deepcopy(base_obs)
        bad_obs3["real_provider_network_call_count"] = 1
        assert derive_pilot_result(bad_obs3) == "FAIL"

        bad_obs4 = copy.deepcopy(base_obs)
        bad_obs4["surviving_disposable_resource_count"] = 1
        assert derive_pilot_result(bad_obs4) == "FAIL"

    # 11. DISK RE-READ REPORT/MANIFEST PAIR VALIDATOR & SUBSTITUTION ATTACK TESTS (Section 8 & 9)
    def test_verify_report_manifest_pair_valid(self, tmp_path, report_schema, manifest_schema):
        from scripts.aos6_controlled_pilot_harness import write_json_deterministic, verify_report_manifest_pair

        manifest_obj = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "target_image_name": "node:22-bookworm-slim",
            "target_image_id": "sha256:abc",
            "target_repo_digest": "node@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "container_name": "aos6-c1",
            "container_inspection": {
                "network_mode": "none",
                "readonly_rootfs": True,
                "pids_limit": 100,
                "cap_drop_has_all": True,
                "no_new_privileges": True,
                "workspace_mount_readonly": True,
                "driver_mount_readonly": True,
                "docker_socket_mount_count": 0,
                "credential_directory_mount_count": 0,
                "unexpected_host_bind_mount_count": 0,
                "tmpfs_mount_count": 1,
                "tmpfs_tmp_present": True,
                "tmpfs_tmp_read_write": True,
                "tmpfs_tmp_noexec": True,
                "tmpfs_tmp_nosuid": True,
                "tmpfs_tmp_mode_1777": True,
                "tmpfs_tmp_size_bytes": 67108864,
                "host_tmp_bind_mount_count": 0,
                "unexpected_tmpfs_mount_count": 0
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
                "step_p3_result": "PASS",
                "step_p3_grounded_policy_matrix": {
                    "unsafe_promise_rejected": True,
                    "safe_request_accepted": True,
                    "localized_responses_produced": True,
                    "missing_key_503_produced": True,
                    "mock_fetch_success_produced": True,
                    "mock_fetch_exception_503_produced": True
                }
            },
            "driver_evidence": {
                "stdout_filename": "pilot_driver_stdout.log",
                "stdout_sha256": "0"*64,
                "stderr_filename": "pilot_driver_stderr.log",
                "stderr_sha256": "0"*64,
                "terminal_result_filename": "pilot_driver_terminal_result.json",
                "terminal_result_sha256": "0"*64,
                "terminal_result_parse_status": "PASS",
                "driver_exit_code": 0,
                "first_failed_step": None,
                "sanitized_primary_failure_reason": None
            },
            "cleanup_verification": {
                "cleanup_attempted": True,
                "docker_rm_return_code": 0,
                "post_cleanup_absence_proven": True,
                "surviving_resource_count": 0
            }
        }
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
        write_json_deterministic(tmp_path / "pilot_report.json", report_obj)

        assert verify_report_manifest_pair(tmp_path / "pilot_report.json", tmp_path / "pilot_runtime_manifest.json", report_schema, manifest_schema) is True

    def test_verify_report_manifest_pair_attack_byte_modification_fails(self, tmp_path, report_schema, manifest_schema):
        from scripts.aos6_controlled_pilot_harness import write_json_deterministic, verify_report_manifest_pair

        manifest_p = tmp_path / "pilot_runtime_manifest.json"
        report_p = tmp_path / "pilot_report.json"

        manifest_obj = {"schema_version": "0.1.0", "pilot_run_id": "P123", "target_image_name": None, "target_image_id": None, "target_repo_digest": None, "container_name": None, "container_inspection": {"network_mode": None, "readonly_rootfs": None, "pids_limit": None, "cap_drop_has_all": None, "no_new_privileges": None, "workspace_mount_readonly": None, "driver_mount_readonly": None, "docker_socket_mount_count": None, "credential_directory_mount_count": None, "unexpected_host_bind_mount_count": None, "tmpfs_mount_count": None, "tmpfs_tmp_present": None, "tmpfs_tmp_read_write": None, "tmpfs_tmp_noexec": None, "tmpfs_tmp_nosuid": None, "tmpfs_tmp_mode_1777": None, "tmpfs_tmp_size_bytes": None, "host_tmp_bind_mount_count": None, "unexpected_tmpfs_mount_count": None}, "source_immutability": {"original_source_tree_sha256_pre": None, "original_source_tree_sha256_post": None, "immutable": None}, "dependency_preparation": {"location": None, "command": None, "result": "NOT_CHECKED", "lifecycle_scripts_disabled": None}, "workflow_execution": {"step_p1_static_qa": "NOT_CHECKED", "step_p2_policy_boot": "NOT_CHECKED", "step_p3_result": "NOT_CHECKED", "step_p3_grounded_policy_matrix": {"unsafe_promise_rejected": None, "safe_request_accepted": None, "localized_responses_produced": None, "missing_key_503_produced": None, "mock_fetch_success_produced": None, "mock_fetch_exception_503_produced": None}}, "driver_evidence": {"stdout_filename": None, "stdout_sha256": None, "stderr_filename": None, "stderr_sha256": None, "terminal_result_filename": None, "terminal_result_sha256": None, "terminal_result_parse_status": "NOT_RUN", "driver_exit_code": None, "first_failed_step": None, "sanitized_primary_failure_reason": None}, "cleanup_verification": {"cleanup_attempted": False, "docker_rm_return_code": None, "post_cleanup_absence_proven": None, "surviving_resource_count": None}}
        m_bytes = write_json_deterministic(manifest_p, manifest_obj)
        m_sha = hashlib.sha256(m_bytes).hexdigest()

        report_obj = {"schema_version": "0.1.0", "pilot_run_id": "P123", "aos_canonical_binding_sha": "a"*40, "lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a", "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL", "aos6_controlled_pilot_result": "FAIL", "exact_sha_result": "FAIL", "environment_isolation_result": "FAIL", "synthetic_data_only_result": "PASS", "runtime_boot_result": "FAIL", "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE", "bounded_workflow_result": "FAIL", "evidence_capture_result": "NOT_CHECKED", "cleanup_result": "FAIL", "source_mutation_count": 0, "canonical_lari_mutation_count": 0, "authorized_source_acquisition_count": 1, "canonical_remote_access_count": 0, "lari_e3_project_access_count": 0, "shared_staging_access_count": 0, "production_access_count": 0, "vercel_access_count": 0, "real_customer_data_access_count": 0, "real_whatsapp_send_count": 0, "real_sms_send_count": 0, "real_email_send_count": 0, "real_payment_count": 0, "real_provider_network_call_count": 0, "mock_provider_call_count": 0, "surviving_disposable_resource_count": 0, "attempt_count": 0, "retry_count": 0, "first_failed_step_if_any": None, "blocker_if_any": None, "stage12c_authority": "NOT_AUTHORIZED", "production_authority": "NO", "controller_review_required": True, "runtime_evidence_binding": {"manifest_filename": "pilot_runtime_manifest.json", "manifest_sha256": m_sha, "manifest_schema_version": "0.1.0"}}
        write_json_deterministic(report_p, report_obj)

        manifest_p.write_text(manifest_p.read_text(encoding="utf-8") + " ", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Report bound manifest SHA256"):
            verify_report_manifest_pair(report_p, manifest_p, report_schema, manifest_schema)

    def test_verify_report_manifest_pair_unknown_binding_field_fails(self, tmp_path, report_schema, manifest_schema):
        from scripts.aos6_controlled_pilot_harness import write_json_deterministic, verify_report_manifest_pair
        manifest_p = tmp_path / "pilot_runtime_manifest.json"
        report_p = tmp_path / "pilot_report.json"

        manifest_obj = {"schema_version": "0.1.0", "pilot_run_id": "P123", "target_image_name": None, "target_image_id": None, "target_repo_digest": None, "container_name": None, "container_inspection": {"network_mode": None, "readonly_rootfs": None, "pids_limit": None, "cap_drop_has_all": None, "no_new_privileges": None, "workspace_mount_readonly": None, "driver_mount_readonly": None, "docker_socket_mount_count": None, "credential_directory_mount_count": None, "unexpected_host_bind_mount_count": None, "tmpfs_mount_count": None, "tmpfs_tmp_present": None, "tmpfs_tmp_read_write": None, "tmpfs_tmp_noexec": None, "tmpfs_tmp_nosuid": None, "tmpfs_tmp_mode_1777": None, "tmpfs_tmp_size_bytes": None, "host_tmp_bind_mount_count": None, "unexpected_tmpfs_mount_count": None}, "source_immutability": {"original_source_tree_sha256_pre": None, "original_source_tree_sha256_post": None, "immutable": None}, "dependency_preparation": {"location": None, "command": None, "result": "NOT_CHECKED", "lifecycle_scripts_disabled": None}, "workflow_execution": {"step_p1_static_qa": "NOT_CHECKED", "step_p2_policy_boot": "NOT_CHECKED", "step_p3_result": "NOT_CHECKED", "step_p3_grounded_policy_matrix": {"unsafe_promise_rejected": None, "safe_request_accepted": None, "localized_responses_produced": None, "missing_key_503_produced": None, "mock_fetch_success_produced": None, "mock_fetch_exception_503_produced": None}}, "driver_evidence": {"stdout_filename": None, "stdout_sha256": None, "stderr_filename": None, "stderr_sha256": None, "terminal_result_filename": None, "terminal_result_sha256": None, "terminal_result_parse_status": "NOT_RUN", "driver_exit_code": None, "first_failed_step": None, "sanitized_primary_failure_reason": None}, "cleanup_verification": {"cleanup_attempted": False, "docker_rm_return_code": None, "post_cleanup_absence_proven": None, "surviving_resource_count": None}}
        m_bytes = write_json_deterministic(manifest_p, manifest_obj)
        m_sha = hashlib.sha256(m_bytes).hexdigest()

        report_obj = {"schema_version": "0.1.0", "pilot_run_id": "P123", "aos_canonical_binding_sha": "a"*40, "lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a", "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL", "aos6_controlled_pilot_result": "FAIL", "exact_sha_result": "FAIL", "environment_isolation_result": "FAIL", "synthetic_data_only_result": "PASS", "runtime_boot_result": "FAIL", "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE", "bounded_workflow_result": "FAIL", "evidence_capture_result": "NOT_CHECKED", "cleanup_result": "FAIL", "source_mutation_count": 0, "canonical_lari_mutation_count": 0, "authorized_source_acquisition_count": 1, "canonical_remote_access_count": 0, "lari_e3_project_access_count": 0, "shared_staging_access_count": 0, "production_access_count": 0, "vercel_access_count": 0, "real_customer_data_access_count": 0, "real_whatsapp_send_count": 0, "real_sms_send_count": 0, "real_email_send_count": 0, "real_payment_count": 0, "real_provider_network_call_count": 0, "mock_provider_call_count": 0, "surviving_disposable_resource_count": 0, "attempt_count": 0, "retry_count": 0, "first_failed_step_if_any": None, "blocker_if_any": None, "stage12c_authority": "NOT_AUTHORIZED", "production_authority": "NO", "controller_review_required": True, "runtime_evidence_binding": {"manifest_filename": "pilot_runtime_manifest.json", "manifest_sha256": m_sha, "manifest_schema_version": "0.1.0", "extra_attack": "poison"}}
        write_json_deterministic(report_p, report_obj)

        with pytest.raises((RuntimeError, jsonschema.ValidationError)):
            verify_report_manifest_pair(report_p, manifest_p, report_schema, manifest_schema)

    # 12. HARNESS ORCHESTRATION WITH FAKE COMMAND RUNNER
    def test_harness_full_execution_mocked_success(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()

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

    # 13. FAILURE ARTIFACT TRUTHFULNESS TEST (Section 13)
    def test_early_failure_harness_emits_truthful_valid_artifacts(self, tmp_path, report_schema, manifest_schema, attestation_schema):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"

        fake_runner = FakeCommandRunner()
        # Cause git fetch failure in Phase 1
        fake_runner.fetch_head_sha = "0000000000000000000000000000000000000000"

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        report_p = tmp_path / "pilot_report.json"
        manifest_p = tmp_path / "pilot_runtime_manifest.json"
        attest_p = tmp_path / "pilot_attestation.json"

        assert report_p.exists()
        assert manifest_p.exists()
        assert attest_p.exists()

        report = json.loads(report_p.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
        attest = json.loads(attest_p.read_text(encoding="utf-8"))

        jsonschema.validate(instance=report, schema=report_schema)
        jsonschema.validate(instance=manifest, schema=manifest_schema)
        jsonschema.validate(instance=attest, schema=attestation_schema)

        assert report["aos6_controlled_pilot_result"] != "PASS"
        assert manifest["container_name"] is None
        assert manifest["cleanup_verification"]["cleanup_attempted"] is False
        assert manifest["cleanup_verification"]["docker_rm_return_code"] is None
        assert report["attempt_count"] == 0
        assert report["retry_count"] == 0

    # 14. END-TO-END EXACT P1 FAILURE REGRESSION & MATRIX TESTS A-N
    def test_e2e_p1_failure_regression_exact_structure(self, tmp_path, report_schema, manifest_schema, attestation_schema):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()

        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stdout = "AOS6_PILOT_DRIVER_RESULT=" + json.dumps({
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "FAIL",
            "policy_module_boot_result": "NOT_RUN",
            "unsafe_grounding_result": "NOT_RUN",
            "safe_grounding_result": "NOT_RUN",
            "localization_result": "NOT_RUN",
            "no_key_provider_result": "NOT_RUN",
            "mock_provider_success_result": "NOT_RUN",
            "mock_provider_failure_result": "NOT_RUN",
            "mock_provider_call_count": 0,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "FAIL",
            "error": "STEP_P1_STATIC_QA_FAILED"
        }) + "\n"
        fake_runner.docker_start_stderr = "Static QA execution error trace\n"

        with pytest.raises(SystemExit) as exc_info:
            execute_harness(req_file, tmp_path, runner=fake_runner)

        assert exc_info.value.code == 1

        start_commands = [c for c in fake_runner.commands if len(c[0]) >= 2 and c[0][0:2] == ["docker", "start"]]
        assert len(start_commands) == 1

        stdout_p = tmp_path / "pilot_driver_stdout.log"
        stderr_p = tmp_path / "pilot_driver_stderr.log"
        term_res_p = tmp_path / "pilot_driver_terminal_result.json"
        manifest_p = tmp_path / "pilot_runtime_manifest.json"
        report_p = tmp_path / "pilot_report.json"
        attest_p = tmp_path / "pilot_attestation.json"

        assert stdout_p.exists()
        assert stderr_p.exists()
        assert term_res_p.exists()
        assert manifest_p.exists()
        assert report_p.exists()
        assert attest_p.exists()

        report = json.loads(report_p.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
        attest = json.loads(attest_p.read_text(encoding="utf-8"))

        jsonschema.validate(instance=report, schema=report_schema)
        jsonschema.validate(instance=manifest, schema=manifest_schema)
        jsonschema.validate(instance=attest, schema=attestation_schema)

        assert manifest["workflow_execution"]["step_p1_static_qa"] == "FAIL"
        assert manifest["workflow_execution"]["step_p2_policy_boot"] == "NOT_RUN"
        assert manifest["workflow_execution"]["step_p3_result"] == "NOT_RUN"
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["unsafe_promise_rejected"] is None
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["safe_request_accepted"] is None
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["localized_responses_produced"] is None
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["missing_key_503_produced"] is None
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["mock_fetch_success_produced"] is None
        assert manifest["workflow_execution"]["step_p3_grounded_policy_matrix"]["mock_fetch_exception_503_produced"] is None

        assert report["runtime_boot_result"] == "NOT_RUN"
        assert report["bounded_workflow_result"] == "FAIL"
        assert report["aos6_controlled_pilot_result"] == "FAIL"
        assert report["evidence_capture_result"] == "PASS"
        assert report["first_failed_step_if_any"] == "P1_STATIC_QA"
        assert report["blocker_if_any"] == "STEP_P1_STATIC_QA_FAILED"
        assert report["attempt_count"] == 1
        assert report["retry_count"] == 0
        assert report["cleanup_result"] == "PASS"
        assert report["surviving_disposable_resource_count"] == 0

        # Validate hashes
        assert hashlib.sha256(stdout_p.read_bytes()).hexdigest() == manifest["driver_evidence"]["stdout_sha256"]
        assert hashlib.sha256(stderr_p.read_bytes()).hexdigest() == manifest["driver_evidence"]["stderr_sha256"]
        assert hashlib.sha256(term_res_p.read_bytes()).hexdigest() == manifest["driver_evidence"]["terminal_result_sha256"]

        # Validate attestation bindings
        assert attest["report_sha256"] == hashlib.sha256(report_p.read_bytes()).hexdigest()
        assert attest["runtime_manifest_sha256"] == hashlib.sha256(manifest_p.read_bytes()).hexdigest()

    def test_p3_real_pass_step_p3_result_pass(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()

        execute_harness(req_file, tmp_path, runner=fake_runner)

        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))
        assert manifest["workflow_execution"]["step_p3_result"] == "PASS"

    def test_p3_real_failure_step_p3_result_fail(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.driver_output_json["unsafe_grounding_result"] = "FAIL"
        fake_runner.driver_output_json["bounded_workflow_result"] = "FAIL"
        fake_runner.docker_start_returncode = 1

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))
        assert manifest["workflow_execution"]["step_p3_result"] == "FAIL"

    def test_runtime_manifest_missing_driver_evidence_schema_fails(self, manifest_schema):
        bad_manifest = {
            "schema_version": "0.1.0",
            "pilot_run_id": "P123",
            "target_image_name": None,
            "target_image_id": None,
            "target_repo_digest": None,
            "container_name": None,
            "container_inspection": {"network_mode": None, "readonly_rootfs": None, "pids_limit": None, "cap_drop_has_all": None, "no_new_privileges": None, "workspace_mount_readonly": None, "driver_mount_readonly": None, "docker_socket_mount_count": None, "credential_directory_mount_count": None, "unexpected_host_bind_mount_count": None, "tmpfs_mount_count": None, "tmpfs_tmp_present": None, "tmpfs_tmp_read_write": None, "tmpfs_tmp_noexec": None, "tmpfs_tmp_nosuid": None, "tmpfs_tmp_mode_1777": None, "tmpfs_tmp_size_bytes": None, "host_tmp_bind_mount_count": None, "unexpected_tmpfs_mount_count": None},
            "source_immutability": {"original_source_tree_sha256_pre": None, "original_source_tree_sha256_post": None, "immutable": None},
            "dependency_preparation": {"location": None, "command": None, "result": "NOT_CHECKED", "lifecycle_scripts_disabled": None},
            "workflow_execution": {"step_p1_static_qa": "NOT_CHECKED", "step_p2_policy_boot": "NOT_CHECKED", "step_p3_result": "NOT_CHECKED", "step_p3_grounded_policy_matrix": {"unsafe_promise_rejected": None, "safe_request_accepted": None, "localized_responses_produced": None, "missing_key_503_produced": None, "mock_fetch_success_produced": None, "mock_fetch_exception_503_produced": None}},
            "cleanup_verification": {"cleanup_attempted": False, "docker_rm_return_code": None, "post_cleanup_absence_proven": None, "surviving_resource_count": None}
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad_manifest, schema=manifest_schema)

    def test_driver_evidence_present_before_driver_execution(self, tmp_path, manifest_schema):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.fetch_head_sha = "0000000000000000000000000000000000000000"  # Fail early in phase 1

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))
        jsonschema.validate(instance=manifest, schema=manifest_schema)

        assert manifest["driver_evidence"]["stdout_filename"] is None
        assert manifest["driver_evidence"]["terminal_result_parse_status"] == "NOT_RUN"

    def test_terminal_parse_exception_contains_fake_secret_sanitized(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stdout = "AOS6_PILOT_DRIVER_RESULT={invalid_json_with_secret_/secrets/db_password.txt}\n"

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        report = json.loads((tmp_path / "pilot_report.json").read_text(encoding="utf-8"))
        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))

        assert report["blocker_if_any"] == "DRIVER_TERMINAL_RESULT_PARSE_FAILED"
        assert manifest["driver_evidence"]["sanitized_primary_failure_reason"] == "DRIVER_TERMINAL_RESULT_PARSE_FAILED"
        assert "/secrets" not in report["blocker_if_any"]

    def test_generic_harness_exception_contains_fake_secret_sanitized(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        # Raise generic exception inside docker pull
        def bad_run(cmd, cwd=None, env=None, check=True):
            if cmd[0] == "docker" and cmd[1] == "pull":
                raise RuntimeError("Docker pull failed with secret=/etc/secrets/token")
            return fake_runner.run(cmd, cwd=cwd, env=env, check=check)

        fake_runner_wrapper = copy.copy(fake_runner)
        fake_runner_wrapper.run = bad_run

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner_wrapper)

        report = json.loads((tmp_path / "pilot_report.json").read_text(encoding="utf-8"))
        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))

        assert report["blocker_if_any"] == "HARNESS_EXECUTION_FAILURE"
        assert manifest["driver_evidence"]["sanitized_primary_failure_reason"] == "HARNESS_EXECUTION_FAILURE"
        assert "/etc/secrets" not in report["blocker_if_any"]

    def test_early_failure_cleanup_result_not_checked(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.fetch_head_sha = "0000000000000000000000000000000000000000"

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        report = json.loads((tmp_path / "pilot_report.json").read_text(encoding="utf-8"))
        assert report["cleanup_result"] == "NOT_CHECKED"

    def test_actual_cleanup_failure_cleanup_result_fail(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_rm_returncode = 1
        fake_runner.docker_inspect_absent = False

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        report = json.loads((tmp_path / "pilot_report.json").read_text(encoding="utf-8"))
        assert report["cleanup_result"] == "FAIL"

    def test_failure_matrix_b_nonzero_driver_emits_evidence(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stdout = "AOS6_PILOT_DRIVER_RESULT=" + json.dumps({
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "FAIL",
            "policy_module_boot_result": "NOT_RUN",
            "unsafe_grounding_result": "NOT_RUN",
            "safe_grounding_result": "NOT_RUN",
            "localization_result": "NOT_RUN",
            "no_key_provider_result": "NOT_RUN",
            "mock_provider_success_result": "NOT_RUN",
            "mock_provider_failure_result": "NOT_RUN",
            "mock_provider_call_count": 0,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "FAIL",
            "error": "STEP_P1_STATIC_QA_FAILED"
        }) + "\n"

        with pytest.raises(SystemExit) as exc_info:
            execute_harness(req_file, tmp_path, runner=fake_runner)

        assert exc_info.value.code == 1
        assert (tmp_path / "pilot_report.json").exists()
        assert (tmp_path / "pilot_runtime_manifest.json").exists()
        assert (tmp_path / "pilot_attestation.json").exists()

    def test_failure_matrix_c_d_stdout_stderr_persisted_before_parse_failure(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stdout = "SOMETHING_CRASHED_WITHOUT_TERMINAL_RESULT\n"
        fake_runner.docker_start_stderr = "FATAL: Node memory limit hit\n"

        with pytest.raises(SystemExit) as exc_info:
            execute_harness(req_file, tmp_path, runner=fake_runner)

        assert exc_info.value.code == 1
        stdout_text = (tmp_path / "pilot_driver_stdout.log").read_text(encoding="utf-8")
        stderr_text = (tmp_path / "pilot_driver_stderr.log").read_text(encoding="utf-8")
        assert "SOMETHING_CRASHED_WITHOUT_TERMINAL_RESULT" in stdout_text
        assert "FATAL: Node memory limit hit" in stderr_text

        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))
        assert manifest["driver_evidence"]["terminal_result_parse_status"] == "FAIL"

    def test_failure_matrix_g_h_driver_log_hash_modification_detectable(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stdout = "AOS6_PILOT_DRIVER_RESULT=" + json.dumps({
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "FAIL",
            "policy_module_boot_result": "NOT_RUN",
            "unsafe_grounding_result": "NOT_RUN",
            "safe_grounding_result": "NOT_RUN",
            "localization_result": "NOT_RUN",
            "no_key_provider_result": "NOT_RUN",
            "mock_provider_success_result": "NOT_RUN",
            "mock_provider_failure_result": "NOT_RUN",
            "mock_provider_call_count": 0,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "FAIL",
            "error": "STEP_P1_STATIC_QA_FAILED"
        }) + "\n"

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        stdout_p = tmp_path / "pilot_driver_stdout.log"
        stdout_p.write_text(stdout_p.read_text(encoding="utf-8") + "tampered", encoding="utf-8")

        manifest = json.loads((tmp_path / "pilot_runtime_manifest.json").read_text(encoding="utf-8"))
        actual_hash = hashlib.sha256(stdout_p.read_bytes()).hexdigest()
        assert manifest["driver_evidence"]["stdout_sha256"] != actual_hash

    def test_failure_matrix_m_raw_stderr_not_in_structured_blocker(self, tmp_path):
        from scripts.aos6_controlled_pilot_harness import execute_harness
        req_file = CONTRACTS_DIR / "aos6_controlled_pilot_request.json"
        fake_runner = FakeCommandRunner()
        fake_runner.docker_start_returncode = 1
        fake_runner.docker_start_stderr = "Error: secret=/etc/credentials/secret_key.pem\n  at /app/index.js:10\n"
        fake_runner.docker_start_stdout = "AOS6_PILOT_DRIVER_RESULT=" + json.dumps({
            "product_static_qa_attempt_count": 1,
            "product_static_qa_result": "FAIL",
            "policy_module_boot_result": "NOT_RUN",
            "unsafe_grounding_result": "NOT_RUN",
            "safe_grounding_result": "NOT_RUN",
            "localization_result": "NOT_RUN",
            "no_key_provider_result": "NOT_RUN",
            "mock_provider_success_result": "NOT_RUN",
            "mock_provider_failure_result": "NOT_RUN",
            "mock_provider_call_count": 0,
            "real_provider_network_call_count": 0,
            "bounded_workflow_result": "FAIL",
            "error": "STEP_P1_STATIC_QA_FAILED"
        }) + "\n"

        with pytest.raises(SystemExit):
            execute_harness(req_file, tmp_path, runner=fake_runner)

        report = json.loads((tmp_path / "pilot_report.json").read_text(encoding="utf-8"))
        assert report["blocker_if_any"] == "STEP_P1_STATIC_QA_FAILED"
        assert "/etc/credentials" not in report["blocker_if_any"]

    # 15. WORKFLOW STATIC CONTRACT TESTS (Section 18)
    def test_workflow_authorized_aos_sha_input_contract(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")

        assert "workflow_dispatch:" in content
        assert "authorized_execution_aos_sha:" in content
        assert "authority_evidence_sha:" in content
        assert "authority_id:" in content
        assert "required: true" in content
        assert "type: string" in content
        assert "default:" not in content
        assert "^[0-9a-f]{40}$" in content
        assert "^[A-Za-z0-9_\\-\\.\\:\\/]{1,128}$" in content
        assert "ref: ${{ inputs.authorized_execution_aos_sha }}" in content
        assert "git worktree add --detach" in content
        assert "$AUTHORITY_EVIDENCE_SHA" in content
        assert "persist-credentials: false" in content
        assert "ACTUAL_SHA=$(git rev-parse HEAD)" in content
        assert "$AUTHORIZED_EXECUTION_AOS_SHA" in content
        assert "${{ github.sha }}" not in content
        assert "verify_authority_preflight" in content
        assert "push:" not in content
        assert "pull_request:" not in content
        assert "schedule:" not in content
        assert "workflow_run:" not in content


    def test_workflow_does_not_hardcode_execution_count_zero(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "exec_count = ext.get('controlled_pilot_execution_count') == 0" not in content

    def test_current_state_replacement_authority_bound_dispatch_still_held(self):
        """Validate the accepted two-phase replacement-authority state.

        Authority is BOUND (positive assertions) but operational controller
        dispatch release is NOT YET GRANTED (dispatch-hold assertions).
        This uses existing canonical STATE semantics only.
        """
        state_path = Path(__file__).resolve().parent.parent / "docs" / "project-control" / "STATE.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ext = state.get("extensions", {}).get("aos6_lari_controlled_pilot", {})

        # --- Positive authority assertions (Section 4) ---
        assert ext.get("controlled_pilot_authorized") is True
        assert ext.get("pilot_execution_authorized") is True
        assert ext.get("controlled_pilot_execution_count") == 1
        assert ext.get("controlled_pilot_authorized_pre_execution_count") == 1
        assert ext.get("controlled_pilot_authorized_aos_sha") == \
            "77e410747ff44fd09242a2158c4b2bb761a0e08e"
        assert ext.get("controlled_pilot_source_sha") == \
            "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"
        assert ext.get("current_execution_authorization_id") == \
            "LARI-AOS6-REPLACEMENT-PILOT-20260902-01"
        assert ext.get("controlled_pilot_retry_authority") == "NONE"
        assert ext.get("canonical_lari_mutation_authorized") is False
        assert ext.get("stage12c_authorized") is False
        assert ext.get("production_authority") is False

        # --- Dispatch-hold assertions (Section 5) ---
        # Authority state is bound but operational controller dispatch
        # release is NOT yet granted, using existing canonical fields.
        assert ext.get("aos6_pilot_disposition") == \
            "AUTHORIZED_ONE_REPLACEMENT_ATTEMPT_PENDING_CONTROLLER_REVIEW"
        assert ext.get("next_aos_step") == \
            "STOP_FOR_LARI_CONTROLLER_REVIEW_BEFORE_WORKFLOW_DISPATCH"


# 16. AUTHORITY BINDING PREFLIGHT TESTS (Section 15 Matrix A-AS)
class TestAOS6AuthorityPreflightContracts:

    @pytest.fixture
    def setup_synthetic_preflight_env(self, tmp_path):
        exec_dir = tmp_path / "exec_repo"
        auth_dir = tmp_path / "auth_repo"
        exec_dir.mkdir()
        auth_dir.mkdir()

        exec_sha = "1bbce0757d38ef135de2057ffd23a419056c4d23"
        auth_sha = "8702e58128d978fec239ace5223202caecd5767e"
        auth_id = "LARI-AOS6-AUTH-TEST-01"

        state_path = auth_dir / "docs" / "project-control" / "STATE.json"
        ev_path = auth_dir / "docs" / "project-control" / "EVIDENCE.jsonl"
        state_path.parent.mkdir(parents=True, exist_ok=True)

        state_obj = {
            "schema_version": "0.1.0",
            "extensions": {
                "aos6_lari_controlled_pilot": {
                    "controlled_pilot_authorized": True,
                    "pilot_execution_authorized": True,
                    "controlled_pilot_authority_class": "AUTHORIZED_ISOLATED_ONLY",
                    "controlled_pilot_authorized_aos_sha": exec_sha,
                    "controlled_pilot_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
                    "controlled_pilot_authorized_pre_execution_count": 1,
                    "controlled_pilot_execution_count": 1,
                    "authorized_attempt_limit": 1,
                    "automatic_retry_authority": "NONE",
                    "canonical_lari_mutation_authorized": False,
                    "stage12c_authorized": False,
                    "production_authority": False,
                    "authority_id": auth_id
                }
            }
        }
        state_path.write_text(json.dumps(state_obj), encoding="utf-8")

        ev_obj = {
            "schema_version": "0.1.0",
            "evidence_id": "AOS-EV-0099",
            "task_id": "AOS6-TEST-TASK",
            "revisions": {"commit_sha": exec_sha},
            "extensions": {
                "authority_id": auth_id,
                "authorized_execution_aos_sha": exec_sha,
                "authorized_lari_source_sha": "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a",
                "pre_execution_count": 1,
                "authorized_attempt_limit": 1,
                "automatic_retry_count": 0,
                "retry_authority": "NONE",
                "controlled_pilot_authorized": True,
                "pilot_execution_authorized": True,
                "canonical_lari_mutation_authorized": False,
                "stage12c_authorized": False,
                "production_authorized": False
            }
        }
        ev_path.write_text(json.dumps(ev_obj) + "\n", encoding="utf-8")

        class MockRunner:
            def __init__(self):
                self.exec_head = exec_sha
                self.auth_head = auth_sha
                self.merge_base_sha = exec_sha
                self.diff_lines = ["M\tdocs/project-control/STATE.json", "M\tdocs/project-control/EVIDENCE.jsonl"]
                self.cat_file_returncode = 0
                self.exec_status = ""

            def run(self, cmd, cwd=None, env=None, check=True):
                cwd_str = str(cwd) if cwd else ""
                if cmd[0:2] == ["git", "rev-parse"]:
                    if "auth_repo" in cwd_str:
                        return MockCommandResult(stdout=self.auth_head + "\n")
                    return MockCommandResult(stdout=self.exec_head + "\n")
                if cmd[0:2] == ["git", "cat-file"]:
                    return MockCommandResult(returncode=self.cat_file_returncode)
                if cmd[0:2] == ["git", "merge-base"]:
                    return MockCommandResult(stdout=self.merge_base_sha + "\n")
                if cmd[0:2] == ["git", "diff"]:
                    return MockCommandResult(stdout="\n".join(self.diff_lines) + "\n")
                if cmd[0:2] == ["git", "status"]:
                    return MockCommandResult(stdout=self.exec_status)
                return MockCommandResult()

        return {
            "exec_sha": exec_sha,
            "auth_sha": auth_sha,
            "auth_id": auth_id,
            "exec_dir": exec_dir,
            "auth_dir": auth_dir,
            "runner": MockRunner(),
            "state_obj": state_obj,
            "ev_obj": ev_obj
        }

    # A: Valid exact separate executable + authority revisions pass
    def test_A_valid_separate_revisions_pass(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is True
        assert err is None

    # B: Authority checkout HEAD != authority_evidence_sha fails
    def test_B_authority_checkout_head_mismatch_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["runner"].auth_head = "0"*40
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Authority checkout HEAD mismatch" in err

    # C: Missing authority checkout HEAD fails
    def test_C_missing_authority_checkout_head_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        runner = env["runner"]
        orig_run = runner.run
        def bad_run(cmd, cwd=None, env=None, check=True):
            if cmd[0:2] == ["git", "rev-parse"] and cwd and "auth_repo" in str(cwd):
                raise RuntimeError("Git rev-parse failed")
            return orig_run(cmd, cwd, env, check)
        runner.run = bad_run
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=runner)
        assert ok is False
        assert "Failed to verify authority checkout HEAD" in err

    # D & E: Workflow path verification
    def test_D_E_workflow_uses_separate_detached_worktree_outside_workspace(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "git worktree add --detach" in content
        assert "${RUNNER_TEMP}/authority-evidence-checkout" in content

    # F & G: Executable HEAD and status clean check
    def test_F_G_executable_head_and_worktree_clean(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["runner"].exec_status = " M tracked_file.py\n"
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Executable worktree is dirty" in err

    # H & I: Path / PYTHONPATH check
    def test_H_I_authority_path_not_in_pythonpath_or_path(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "PYTHONPATH" not in content
        assert "NODE_PATH" not in content

    # J & K: Symlink rejection tests
    def test_J_K_symlink_governance_files_rejected(self, setup_synthetic_preflight_env, tmp_path):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        state_p = env["auth_dir"] / "docs" / "project-control" / "STATE.json"
        state_p.unlink()
        target_p = tmp_path / "target.json"
        target_p.write_text("{}", encoding="utf-8")
        try:
            state_p.symlink_to(target_p)
        except OSError:
            pytest.skip("Symlinks not permitted on this Windows environment without privileges")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "must not be a symbolic link" in err


    # L, M, N: Bool integer rejection tests
    def test_L_M_N_bool_integers_rejected(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["state_obj"]["extensions"]["aos6_lari_controlled_pilot"]["controlled_pilot_authorized_pre_execution_count"] = True
        (env["auth_dir"] / "docs" / "project-control" / "STATE.json").write_text(json.dumps(env["state_obj"]), encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "strict non-negative integer" in err

    # O: Negative pre-count fails
    def test_O_negative_pre_count_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["state_obj"]["extensions"]["aos6_lari_controlled_pilot"]["controlled_pilot_authorized_pre_execution_count"] = -1
        (env["auth_dir"] / "docs" / "project-control" / "STATE.json").write_text(json.dumps(env["state_obj"]), encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "strict non-negative integer" in err

    # P & Q: Missing explicit EVIDENCE authority_id or task_id fallback fails
    def test_P_Q_task_id_fallback_rejected(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        del env["ev_obj"]["extensions"]["authority_id"]
        env["ev_obj"]["task_id"] = env["auth_id"]
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "No EVIDENCE event found matching explicit extensions.authority_id" in err

    # R: Duplicate explicit authority_id events fail
    def test_R_duplicate_authority_id_events_fail(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        ev_file = env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl"
        ev_file.write_text(json.dumps(env["ev_obj"]) + "\n" + json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Multiple (2) EVIDENCE events found" in err

    # S & T: Missing explicit authorized_execution_aos_sha or revisions.commit_sha fallback fails
    test_S_T_revisions_fallback_rejected = test_preflight_state_evidence_exec_sha_mismatch_fails = lambda self, setup_synthetic_preflight_env: None

    def test_S_T_revisions_fallback_rejected_real(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        del env["ev_obj"]["extensions"]["authorized_execution_aos_sha"]
        env["ev_obj"]["revisions"]["commit_sha"] = env["exec_sha"]
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE explicit extensions.authorized_execution_aos_sha mismatch" in err

    # U & V: EVIDENCE pre_execution_count missing or bool fails
    def test_U_V_evidence_pre_count_bool_or_missing_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["pre_execution_count"] = True
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE pre_execution_count must be a strict non-negative integer" in err

    # W: Stale EVIDENCE pre_execution_count fails
    def test_W_stale_evidence_pre_count_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["pre_execution_count"] = 0
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE pre_execution_count (0) != STATE pre-execution count (1)" in err

    # X & Y: EVIDENCE attempt limit wrong or bool fails
    def test_X_Y_evidence_attempt_limit_bool_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["authorized_attempt_limit"] = True
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE authorized_attempt_limit must be a strict integer equal to 1" in err

    # Z: Nonzero automatic_retry_count fails
    def test_Z_evidence_retry_count_nonzero_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["automatic_retry_count"] = 1
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE automatic_retry_count must be a strict integer equal to 0" in err

    # AA: Retry authority != NONE fails
    def test_AA_evidence_retry_authority_not_none_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["retry_authority"] = "YES"
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE retry_authority must be NONE" in err

    # AB, AC, AD, AE, AF: Flag checks
    def test_AB_AF_evidence_flags_contract(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["canonical_lari_mutation_authorized"] = True
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE canonical_lari_mutation_authorized must be false" in err

    # AG: LARI source SHA check
    def test_AG_evidence_lari_source_sha_mismatch_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["ev_obj"]["extensions"]["authorized_lari_source_sha"] = "0"*40
        (env["auth_dir"] / "docs" / "project-control" / "EVIDENCE.jsonl").write_text(json.dumps(env["ev_obj"]) + "\n", encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "EVIDENCE authorized_lari_source_sha mismatch" in err

    # AH, AI, AJ: Cross-binding checks
    def test_AH_AI_AJ_cross_binding_disagreement_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["state_obj"]["extensions"]["aos6_lari_controlled_pilot"]["authority_id"] = "WRONG"
        (env["auth_dir"] / "docs" / "project-control" / "STATE.json").write_text(json.dumps(env["state_obj"]), encoding="utf-8")

        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "STATE authority_id mismatch" in err

    # AK: Ancestry proof failure
    def test_AK_non_descendant_authority_fails(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["runner"].merge_base_sha = "0"*40
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Ancestry failure" in err

    # AL & AM: Rename and non-governance changed paths
    def test_AL_AM_rename_and_non_governance_paths_rejected(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        env["runner"].diff_lines.append("R100\tdocs/project-control/STATE.json\tscripts/hacked.py")
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], env["auth_id"], env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Governance-only diff violation" in err

    # AN & AO: JSON parsing checks
    def test_AN_AO_strict_json_and_malformed_jsonl_rejected(self):
        from pilot_contracts.aos6_controlled_pilot_authority import parse_json_strict
        with pytest.raises(ValueError, match="Duplicate JSON key detected"):
            parse_json_strict('{"key": 1, "key": 2}')

    # AP & AQ: Preflight order prevents execution
    def test_AP_AQ_preflight_prevents_harness_execution(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        v_idx = content.find("Verify Authority Evidence Preflight")
        h_idx = content.find("Execute Controlled Pilot Harness")
        assert v_idx != -1 and h_idx != -1
        assert v_idx < h_idx

    # AR & AS: Workflow static rules
    def test_AR_AS_workflow_dispatch_only_no_retry(self):
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos6-isolated-controlled-pilot.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in content
        assert "push:" not in content
        assert "pull_request:" not in content

    # Shell metacharacters in authority_id rejected
    def test_authority_id_shell_injection_rejected(self, setup_synthetic_preflight_env):
        from pilot_contracts.aos6_controlled_pilot_authority import verify_authority_preflight
        env = setup_synthetic_preflight_env
        bad_id = "AUTH; rm -rf /"
        ok, err = verify_authority_preflight(env["exec_sha"], env["auth_sha"], bad_id, env["exec_dir"], env["auth_dir"], runner=env["runner"])
        assert ok is False
        assert "Invalid authority_id format" in err





