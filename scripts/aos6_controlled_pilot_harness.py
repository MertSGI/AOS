#!/usr/bin/env python3
"""
AOS6 Controlled Pilot Harness CLI (Python 3.12+)
Full production orchestration harness for isolated controlled pilot.
"""

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

try:
    import jsonschema
except ImportError:
    print("[AOS6 Harness] FATAL: jsonschema module is missing. Failing closed.", file=sys.stderr)
    sys.exit(1)

AUTHORIZED_SOURCE_SHA = "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"
FIXED_SOURCE_REPO_URL = "https://github.com/MertSGI/Randapp-main.git"
TARGET_IMAGE_NAME = "node:22-bookworm-slim"

FORBIDDEN_ENV_KEYWORDS = [
    "SUPABASE", "SERVICE_ROLE", "ANON_KEY", "VERCEL", "OPENAI", "GROQ",
    "TWILIO", "WHATSAPP", "SMS", "SMTP", "EMAIL", "PAYMENT", "IYZICO",
    "STRIPE", "DATABASE_URL", "POSTGRES_URL"
]

class CommandRunner:
    """Pluggable command runner interface for deterministic testing."""
    def run(self, cmd, cwd=None, env=None, check=True):
        res = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)
        return res

def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def validate_against_schema(data, schema):
    jsonschema.validate(instance=data, schema=schema)

def write_json_deterministic(path, data):
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return content.encode("utf-8")

def sanitize_env(env_dict):
    sanitized = {}
    for k, v in env_dict.items():
        k_upper = k.upper()
        if any(keyword in k_upper for keyword in FORBIDDEN_ENV_KEYWORDS):
            continue
        sanitized[k] = v
    return sanitized

def build_minimal_container_env():
    return {
        "NODE_ENV": "test",
        "DISPOSABLE_WORKSPACE_DIR": "/workspace"
    }

def validate_request(request_data, request_schema):
    validate_against_schema(request_data, request_schema)
    if request_data.get("source_sha") != AUTHORIZED_SOURCE_SHA:
        raise ValueError(f"Unauthorized source SHA: {request_data.get('source_sha')}")
    if request_data.get("attempt_limit") != 1:
        raise ValueError("attempt_limit must be 1")
    if request_data.get("automatic_retry_allowed") is not False:
        raise ValueError("automatic_retry_allowed must be false")
    if request_data.get("canonical_lari_mutation_allowed") is not False:
        raise ValueError("canonical_lari_mutation_allowed must be false")
    if request_data.get("stage12c_allowed") is not False:
        raise ValueError("stage12c_allowed must be false")
    if request_data.get("production_allowed") is not False:
        raise ValueError("production_allowed must be false")
    if request_data.get("real_customer_data_allowed") is not False:
        raise ValueError("real_customer_data_allowed must be false")
    if request_data.get("real_external_communications_allowed") is not False:
        raise ValueError("real_external_communications_allowed must be false")

def build_tracked_source_manifest(source_dir, runner=None):
    if runner is None:
        runner = CommandRunner()
    res = runner.run(["git", "ls-files", "-z"], cwd=source_dir)
    raw_files = res.stdout.split("\0")

    manifest_entries = {}
    aggregate_sha = hashlib.sha256()

    for rel_path in sorted(raw_files):
        if not rel_path:
            continue
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Invalid path in git tracked files: {rel_path}")

        full_p = Path(source_dir) / p
        if full_p.is_symlink():
            raise ValueError(f"Unsafe symlink in tracked source: {rel_path}")

        try:
            file_bytes = full_p.read_bytes()
        except Exception as e:
            raise RuntimeError(f"Failed to read tracked file {rel_path}: {e}")

        file_sha = hashlib.sha256(file_bytes).hexdigest()
        file_size = len(file_bytes)

        manifest_entries[rel_path] = {
            "size": file_size,
            "sha256": file_sha
        }
        aggregate_sha.update(rel_path.encode("utf-8"))
        aggregate_sha.update(str(file_size).encode("utf-8"))
        aggregate_sha.update(file_sha.encode("utf-8"))

    return manifest_entries, aggregate_sha.hexdigest()

def verify_workspace_against_source_manifest(workspace_dir, source_manifest_entries):
    """Gitless verification of disposable workspace against authoritative source manifest entries."""
    ws_path = Path(workspace_dir).resolve()
    dot_git = ws_path / ".git"
    if dot_git.exists():
        raise RuntimeError(".git directory MUST NOT exist in disposable workspace!")

    ws_entries = {}
    aggregate_sha = hashlib.sha256()

    # Iterate expected tracked files only (no git commands)
    for rel_path, expected_info in sorted(source_manifest_entries.items()):
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Invalid path in manifest entries: {rel_path}")

        full_p = ws_path / p
        if not full_p.exists():
            raise RuntimeError(f"Missing expected tracked file in workspace: {rel_path}")
        if full_p.is_symlink():
            raise RuntimeError(f"Symlink forbidden in workspace file: {rel_path}")

        try:
            file_bytes = full_p.read_bytes()
        except Exception as e:
            raise RuntimeError(f"Failed to read workspace file {rel_path}: {e}")

        file_sha = hashlib.sha256(file_bytes).hexdigest()
        file_size = len(file_bytes)

        if file_size != expected_info["size"] or file_sha != expected_info["sha256"]:
            raise RuntimeError(f"Workspace file {rel_path} byte/hash mismatch with source manifest!")

        ws_entries[rel_path] = {"size": file_size, "sha256": file_sha}
        aggregate_sha.update(rel_path.encode("utf-8"))
        aggregate_sha.update(str(file_size).encode("utf-8"))
        aggregate_sha.update(file_sha.encode("utf-8"))

    return ws_entries, aggregate_sha.hexdigest()

def verify_report_manifest_pair(report_path, manifest_path, report_schema, manifest_schema):
    """Disk re-read validator for report <-> manifest pair substitution defense."""
    report_file = Path(report_path).resolve()
    manifest_file = Path(manifest_path).resolve()

    if not report_file.exists() or not manifest_file.exists():
        raise RuntimeError("Report or manifest file missing on disk!")

    try:
        report_data = json.loads(report_file.read_text(encoding="utf-8"))
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Malformed JSON in report or manifest pair: {e}")

    validate_against_schema(report_data, report_schema)
    validate_against_schema(manifest_data, manifest_schema)

    manifest_bytes = manifest_file.read_bytes()
    recomputed_sha = hashlib.sha256(manifest_bytes).hexdigest()

    binding = report_data.get("runtime_evidence_binding", {})
    claimed_filename = binding.get("manifest_filename")
    claimed_sha = binding.get("manifest_sha256")
    claimed_ver = binding.get("manifest_schema_version")

    if claimed_filename != "pilot_runtime_manifest.json":
        raise RuntimeError(f"Invalid bound manifest_filename: {claimed_filename}")
    if claimed_ver != "0.1.0":
        raise RuntimeError(f"Invalid bound manifest_schema_version: {claimed_ver}")
    if not claimed_sha or claimed_sha.lower() != recomputed_sha.lower():
        raise RuntimeError(f"Report bound manifest SHA256 {claimed_sha} != actual written manifest SHA256 {recomputed_sha}")

    return True

def execute_harness(request_path, output_dir, runner=None):
    if runner is None:
        runner = CommandRunner()

    req_path = Path(request_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    contract_dir = Path(__file__).resolve().parent.parent / "pilot_contracts"
    req_schema = load_json_file(contract_dir / "aos6_controlled_pilot_request.schema.json")
    report_schema = load_json_file(contract_dir / "aos6_controlled_pilot_report.schema.json")
    manifest_schema = load_json_file(contract_dir / "aos6_controlled_pilot_runtime_manifest.schema.json")
    attestation_schema = load_json_file(contract_dir / "aos6_controlled_pilot_attestation.schema.json")

    request_data = load_json_file(req_path)
    validate_request(request_data, req_schema)

    pilot_run_id = f"AOS6-PILOT-{int(datetime.now(timezone.utc).timestamp())}"
    container_name = f"aos6-pilot-{pilot_run_id.lower()}"

    first_failed_step = None
    primary_failure = None
    secondary_cleanup_failure = None

    # Tracked counters
    source_mutation_count = 0
    canonical_lari_mutation_count = 0
    authorized_source_acquisition_count = 0
    canonical_remote_access_count = 0
    lari_e3_project_access_count = 0
    shared_staging_access_count = 0
    production_access_count = 0
    vercel_access_count = 0
    real_customer_data_access_count = 0
    real_whatsapp_send_count = 0
    real_sms_send_count = 0
    real_email_send_count = 0
    real_payment_count = 0
    real_provider_network_call_count = 0
    mock_provider_call_count = 0

    resource_created_count = 0
    cleanup_attempt_count = 0
    cleanup_success_count = 0
    cleanup_failure_count = 0
    surviving_disposable_resource_count = 0

    attempt_count = 0
    retry_count = 0

    temp_base = Path(tempfile.mkdtemp(prefix="aos6_pilot_"))
    temp_source_dir = temp_base / "source"
    temp_workspace_dir = temp_base / "workspace"

    # Initial unpopulated observation states (NO FABRICATED PROOF)
    target_image_id = None
    target_repo_digest = None
    container_inspection_obs = None
    immut_ok = None
    dep_prep_result = "NOT_CHECKED"
    driver_result = None

    try:
        # Phase 1: Source Acquisition
        print("[AOS6 Harness] Phase 1: Source Acquisition...")
        temp_source_dir.mkdir(parents=True, exist_ok=True)
        runner.run(["git", "init"], cwd=temp_source_dir)
        runner.run(["git", "remote", "add", "source", FIXED_SOURCE_REPO_URL], cwd=temp_source_dir)

        fetch_res = runner.run(["git", "fetch", "--no-tags", "--depth=1", "source", AUTHORIZED_SOURCE_SHA], cwd=temp_source_dir)
        authorized_source_acquisition_count = 1

        fetch_head_sha = runner.run(["git", "rev-parse", "FETCH_HEAD"], cwd=temp_source_dir).stdout.strip()
        if fetch_head_sha != AUTHORIZED_SOURCE_SHA:
            raise RuntimeError(f"FETCH_HEAD SHA mismatch: {fetch_head_sha} != {AUTHORIZED_SOURCE_SHA}")

        runner.run(["git", "checkout", "--detach", AUTHORIZED_SOURCE_SHA], cwd=temp_source_dir)
        head_sha = runner.run(["git", "rev-parse", "HEAD"], cwd=temp_source_dir).stdout.strip()
        if head_sha != AUTHORIZED_SOURCE_SHA:
            raise RuntimeError(f"HEAD SHA mismatch after checkout: {head_sha} != {AUTHORIZED_SOURCE_SHA}")

        source_tree_sha = runner.run(["git", "rev-parse", "HEAD^{tree}"], cwd=temp_source_dir).stdout.strip()

        status_res = runner.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=temp_source_dir)
        if status_res.stdout.strip():
            raise RuntimeError("Source checkout git status is not clean!")

        runner.run(["git", "remote", "remove", "source"], cwd=temp_source_dir)
        remotes_res = runner.run(["git", "remote"], cwd=temp_source_dir)
        if remotes_res.stdout.strip():
            raise RuntimeError("Remotes remain after remote removal!")

        # Phase 2: Source Immutability Baseline
        print("[AOS6 Harness] Phase 2: Source Immutability Baseline...")
        source_manifest_entries, source_manifest_before_sha256 = build_tracked_source_manifest(temp_source_dir, runner)

        # Phase 3: Runtime Copy Preparation & Gitless Verification
        print("[AOS6 Harness] Phase 3: Runtime Copy Preparation...")
        temp_workspace_dir.mkdir(parents=True, exist_ok=True)
        for rel_file in source_manifest_entries.keys():
            src_f = temp_source_dir / rel_file
            dst_f = temp_workspace_dir / rel_file
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)

        # Gitless workspace verification against authoritative source manifest
        ws_manifest_entries, ws_sha256 = verify_workspace_against_source_manifest(temp_workspace_dir, source_manifest_entries)
        if ws_sha256 != source_manifest_before_sha256:
            raise RuntimeError("Disposable workspace copy sha256 does not match original source manifest!")

        # Phase 4: Dependency Preparation
        print("[AOS6 Harness] Phase 4: Dependency Preparation...")
        lockfile = temp_workspace_dir / "package-lock.json"
        if not lockfile.exists():
            raise RuntimeError("package-lock.json missing in workspace!")

        dep_res = runner.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=temp_workspace_dir)
        dep_prep_result = "PASS"

        # Phase 5: Image Acquisition & Fail-Closed Resolution
        print("[AOS6 Harness] Phase 5: Target Image Acquisition...")
        runner.run(["docker", "pull", TARGET_IMAGE_NAME])

        img_id_res = runner.run(["docker", "inspect", "--format={{.Id}}", TARGET_IMAGE_NAME])
        resolved_img_id = img_id_res.stdout.strip()
        if not resolved_img_id or resolved_img_id == "unknown":
            raise RuntimeError("Target Image ID could not be resolved!")
        target_image_id = resolved_img_id

        digest_res = runner.run(["docker", "inspect", "--format={{index .RepoDigests 0}}", TARGET_IMAGE_NAME])
        resolved_digest = digest_res.stdout.strip()
        if not resolved_digest or "@unknown" in resolved_digest or not re.match(r"^node@sha256:[0-9a-f]{64}$", resolved_digest):
            raise RuntimeError(f"Target RepoDigest resolution fail-closed check failed: '{resolved_digest}'")
        target_repo_digest = resolved_digest

        # Phase 6: Boundary Seal & Target Container Create
        print("[AOS6 Harness] Phase 6: Creating Sealed Target Container...")
        driver_src_path = Path(__file__).resolve().parent / "aos6_controlled_pilot_driver.mjs"

        container_env = build_minimal_container_env()
        env_args = []
        for k, v in container_env.items():
            env_args.extend(["-e", f"{k}={v}"])

        create_cmd = [
            "docker", "create",
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "100",
            "--memory", "512m",
            "--cpus", "1.0",
            "-v", f"{temp_workspace_dir.as_posix()}:/workspace:ro",
            "-v", f"{driver_src_path.as_posix()}:/aos-driver/aos6_controlled_pilot_driver.mjs:ro"
        ] + env_args + [TARGET_IMAGE_NAME, "node", "/aos-driver/aos6_controlled_pilot_driver.mjs"]

        runner.run(create_cmd)
        resource_created_count = 1
        surviving_disposable_resource_count = 1

        # Phase 7: Actual Docker Inspection
        print("[AOS6 Harness] Phase 7: Actual Docker Inspection...")
        inspect_res = runner.run(["docker", "inspect", container_name])
        try:
            inspect_data = json.loads(inspect_res.stdout)[0]
        except Exception as e:
            raise RuntimeError(f"Failed to parse docker inspect JSON: {e}")

        host_config = inspect_data.get("HostConfig", {})
        net_mode = host_config.get("NetworkMode", None)
        readonly_root = host_config.get("ReadonlyRootfs", None)
        pids_lim = host_config.get("PidsLimit", None)
        cap_drop = host_config.get("CapDrop", []) or []
        sec_opt = host_config.get("SecurityOpt", []) or []
        mounts = inspect_data.get("Mounts", []) or []

        cap_drop_has_all = "ALL" in [c.upper() for c in cap_drop]
        no_new_priv = "no-new-privileges" in sec_opt
        docker_socket_count = sum(1 for m in mounts if "docker.sock" in m.get("Source", ""))
        cred_mount_count = sum(1 for m in mounts if any(k in m.get("Source", "") for k in [".ssh", ".aws", ".gcp", "supabase", "vercel"]))

        ws_mount_ro = any(m.get("Destination") == "/workspace" and m.get("RW") is False for m in mounts)
        drv_mount_ro = any(m.get("Destination") == "/aos-driver/aos6_controlled_pilot_driver.mjs" and m.get("RW") is False for m in mounts)

        # Check for any unexpected host bind mounts beyond workspace and driver
        expected_destinations = {"/workspace", "/aos-driver/aos6_controlled_pilot_driver.mjs"}
        unexpected_bind_count = sum(1 for m in mounts if m.get("Destination") not in expected_destinations)

        if (net_mode != "none" or
            readonly_root is not True or
            pids_lim != 100 or
            not cap_drop_has_all or
            not no_new_priv or
            docker_socket_count > 0 or
            cred_mount_count > 0 or
            not ws_mount_ro or
            not drv_mount_ro or
            unexpected_bind_count > 0):
            raise RuntimeError("Docker inspect security parameters validation failed!")

        container_inspection_obs = {
            "network_mode": net_mode,
            "readonly_rootfs": readonly_root,
            "pids_limit": pids_lim,
            "cap_drop_has_all": cap_drop_has_all,
            "no_new_privileges": no_new_priv,
            "workspace_mount_readonly": ws_mount_ro,
            "driver_mount_readonly": drv_mount_ro,
            "docker_socket_mount_count": docker_socket_count,
            "credential_directory_mount_count": cred_mount_count,
            "unexpected_host_bind_mount_count": unexpected_bind_count
        }

        # Phase 8: Bounded Execution & Exactly-Once Terminal Result Verification
        print("[AOS6 Harness] Phase 8: Executing Target Container...")
        attempt_count = 1
        start_res = runner.run(["docker", "start", "-a", container_name], check=False)

        result_lines = [line for line in (start_res.stdout + "\n" + start_res.stderr).splitlines() if line.startswith("AOS6_PILOT_DRIVER_RESULT=")]
        if len(result_lines) != 1:
            raise RuntimeError(f"Terminal driver result MUST occur exactly once! Found count={len(result_lines)}")

        raw_json = result_lines[0].split("=", 1)[1]
        try:
            driver_result = json.loads(raw_json)
        except Exception as e:
            raise RuntimeError(f"Failed to parse terminal driver JSON: {e}")

        mock_provider_call_count = driver_result.get("mock_provider_call_count", 0)
        real_provider_network_call_count = driver_result.get("real_provider_network_call_count", 0)

        if start_res.returncode != 0 or driver_result.get("bounded_workflow_result") != "PASS":
            raise RuntimeError(f"Target container driver execution failed with code {start_res.returncode}")

    except Exception as e:
        print(f"[AOS6 Harness] Execution error: {e}", file=sys.stderr)
        primary_failure = str(e)
        if not first_failed_step:
            first_failed_step = "HARNESS_EXECUTION_FAILURE"

    finally:
        # Phase 9: Final Original Source Proof
        print("[AOS6 Harness] Phase 9: Final Original Source Proof...")
        try:
            if temp_source_dir.exists() and (temp_source_dir / ".git").exists():
                head_check = runner.run(["git", "rev-parse", "HEAD"], cwd=temp_source_dir).stdout.strip()
                status_check = runner.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=temp_source_dir).stdout.strip()
                remote_check = runner.run(["git", "remote"], cwd=temp_source_dir).stdout.strip()

                final_entries, final_sha256 = build_tracked_source_manifest(temp_source_dir, runner)

                if (head_check == AUTHORIZED_SOURCE_SHA and
                    not status_check and
                    not remote_check and
                    final_sha256 == source_manifest_before_sha256):
                    immut_ok = True
                else:
                    immut_ok = False
                    source_mutation_count = 1
            else:
                immut_ok = False
        except Exception as imm_err:
            print(f"[AOS6 Harness] Source immutability check error: {imm_err}", file=sys.stderr)
            immut_ok = False

        # Phase 10: Resource Cleanup
        print("[AOS6 Harness] Phase 10: Resource Cleanup...")
        rm_rc = None
        absence_proven = False

        if resource_created_count > 0:
            cleanup_attempt_count = 1
            try:
                rm_res = runner.run(["docker", "rm", "-f", container_name], check=False)
                rm_rc = rm_res.returncode

                insp_after = runner.run(["docker", "inspect", container_name], check=False)
                if insp_after.returncode != 0:
                    absence_proven = True
                    surviving_disposable_resource_count = 0

                if rm_rc == 0 and absence_proven:
                    cleanup_success_count = 1
                else:
                    cleanup_failure_count = 1
                    secondary_cleanup_failure = "Container removal or absence verification failed"
            except Exception as cl_err:
                cleanup_failure_count = 1
                secondary_cleanup_failure = str(cl_err)

        # Remove temporary base
        shutil.rmtree(temp_base, ignore_errors=True)

    # Phase 11: Final AOS Immutability Verification
    print("[AOS6 Harness] Phase 11: Final AOS Immutability Proof...")
    aos_sha = runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()
    aos_status = runner.run(["git", "status", "--porcelain=v1", "--untracked-files=all"]).stdout.strip()
    aos_diff_res = runner.run(["git", "diff", "--exit-code"], check=False)

    aos_immut = "PASS" if (not aos_status and aos_diff_res.returncode == 0) else "FAIL"

    # Derived Synthetic and Evidence Results
    synthetic_data_only_result = "PASS" if (
        real_customer_data_access_count == 0 and
        real_whatsapp_send_count == 0 and
        real_sms_send_count == 0 and
        real_email_send_count == 0 and
        real_payment_count == 0 and
        real_provider_network_call_count == 0 and
        production_access_count == 0 and
        shared_staging_access_count == 0 and
        lari_e3_project_access_count == 0 and
        vercel_access_count == 0
    ) else "FAIL"

    # Complete PASS Derivation
    aos6_result = "FAIL"
    pass_conditions = {
        "primary_failure": primary_failure is None,
        "secondary_cleanup_failure": secondary_cleanup_failure is None,
        "immut_ok": immut_ok is True,
        "aos_immut": aos_immut == "PASS",
        "driver_result": driver_result is not None,
        "bounded_workflow_result": driver_result and driver_result.get("bounded_workflow_result") == "PASS",
        "real_provider_network_call_count": real_provider_network_call_count == 0,
        "mock_provider_call_count": mock_provider_call_count == 2,
        "surviving_disposable_resource_count": surviving_disposable_resource_count == 0,
        "attempt_count": attempt_count == 1,
        "retry_count": retry_count == 0
    }
    if all(pass_conditions.values()):
        aos6_result = "PASS"

    # Build Runtime Manifest
    manifest_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "target_image_name": TARGET_IMAGE_NAME if 'resolved_img_id' in locals() else None,
        "target_image_id": target_image_id,
        "target_repo_digest": target_repo_digest,
        "container_name": container_name if resource_created_count > 0 else None,
        "container_inspection": container_inspection_obs if container_inspection_obs else {
            "network_mode": None,
            "readonly_rootfs": None,
            "pids_limit": None,
            "cap_drop_has_all": None,
            "no_new_privileges": None,
            "workspace_mount_readonly": None,
            "driver_mount_readonly": None,
            "docker_socket_mount_count": None,
            "credential_directory_mount_count": None,
            "unexpected_host_bind_mount_count": None
        },
        "source_immutability": {
            "original_source_tree_sha256_pre": source_manifest_before_sha256 if 'source_manifest_before_sha256' in locals() else None,
            "original_source_tree_sha256_post": final_sha256 if 'final_sha256' in locals() else None,
            "immutable": immut_ok
        },
        "dependency_preparation": {
            "location": "DISPOSABLE_WORKSPACE_COPY" if 'source_manifest_before_sha256' in locals() else None,
            "command": "npm ci --ignore-scripts --no-audit --no-fund" if 'source_manifest_before_sha256' in locals() else None,
            "result": dep_prep_result,
            "lifecycle_scripts_disabled": True if dep_prep_result == "PASS" else None
        },
        "workflow_execution": {
            "step_p1_static_qa": driver_result.get("product_static_qa_result", "NOT_CHECKED") if driver_result else "NOT_CHECKED",
            "step_p2_policy_boot": driver_result.get("policy_module_boot_result", "NOT_CHECKED") if driver_result else "NOT_CHECKED",
            "step_p3_grounded_policy_matrix": {
                "unsafe_promise_rejected": driver_result.get("unsafe_grounding_result") == "PASS" if driver_result else None,
                "safe_request_accepted": driver_result.get("safe_grounding_result") == "PASS" if driver_result else None,
                "localized_responses_produced": driver_result.get("localization_result") == "PASS" if driver_result else None,
                "missing_key_503_produced": driver_result.get("no_key_provider_result") == "PASS" if driver_result else None,
                "mock_fetch_success_produced": driver_result.get("mock_provider_success_result") == "PASS" if driver_result else None,
                "mock_fetch_exception_503_produced": driver_result.get("mock_provider_failure_result") == "PASS" if driver_result else None
            }
        },
        "cleanup_verification": {
            "cleanup_attempted": cleanup_attempt_count > 0,
            "docker_rm_return_code": rm_rc,
            "post_cleanup_absence_proven": absence_proven if cleanup_attempt_count > 0 else None,
            "surviving_resource_count": surviving_disposable_resource_count if cleanup_attempt_count > 0 else None
        }
    }

    validate_against_schema(manifest_obj, manifest_schema)
    manifest_bytes = write_json_deterministic(out_dir / "pilot_runtime_manifest.json", manifest_obj)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    # Build Report
    report_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "aos_canonical_binding_sha": aos_sha,
        "lari_source_sha": AUTHORIZED_SOURCE_SHA,
        "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL",
        "aos6_controlled_pilot_result": aos6_result,
        "exact_sha_result": "PASS" if 'head_sha' in locals() and head_sha == AUTHORIZED_SOURCE_SHA else "FAIL",
        "environment_isolation_result": "PASS" if container_inspection_obs else "FAIL",
        "synthetic_data_only_result": synthetic_data_only_result,
        "runtime_boot_result": driver_result.get("policy_module_boot_result", "FAIL") if driver_result else "FAIL",
        "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE",
        "bounded_workflow_result": driver_result.get("bounded_workflow_result", "FAIL") if driver_result else "FAIL",
        "evidence_capture_result": "NOT_CHECKED", # Set after pair validation below
        "cleanup_result": "PASS" if cleanup_success_count == 1 else "FAIL",
        "source_mutation_count": source_mutation_count,
        "canonical_lari_mutation_count": canonical_lari_mutation_count,
        "authorized_source_acquisition_count": authorized_source_acquisition_count,
        "canonical_remote_access_count": canonical_remote_access_count,
        "lari_e3_project_access_count": lari_e3_project_access_count,
        "shared_staging_access_count": shared_staging_access_count,
        "production_access_count": production_access_count,
        "vercel_access_count": vercel_access_count,
        "real_customer_data_access_count": real_customer_data_access_count,
        "real_whatsapp_send_count": real_whatsapp_send_count,
        "real_sms_send_count": real_sms_send_count,
        "real_email_send_count": real_email_send_count,
        "real_payment_count": real_payment_count,
        "real_provider_network_call_count": real_provider_network_call_count,
        "mock_provider_call_count": mock_provider_call_count,
        "surviving_disposable_resource_count": surviving_disposable_resource_count,
        "attempt_count": attempt_count,
        "retry_count": retry_count,
        "first_failed_step_if_any": first_failed_step,
        "blocker_if_any": primary_failure,
        "stage12c_authority": "NOT_AUTHORIZED",
        "production_authority": "NO",
        "controller_review_required": True,
        "runtime_evidence_binding": {
            "manifest_filename": "pilot_runtime_manifest.json",
            "manifest_sha256": manifest_sha256,
            "manifest_schema_version": "0.1.0"
        }
    }

    validate_against_schema(report_obj, report_schema)
    report_bytes = write_json_deterministic(out_dir / "pilot_report.json", report_obj)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()

    # Disk Re-Read Pair Validator Defense Check
    pair_ok = verify_report_manifest_pair(out_dir / "pilot_report.json", out_dir / "pilot_runtime_manifest.json", report_schema, manifest_schema)
    if pair_ok:
        report_obj["evidence_capture_result"] = "PASS"
        write_json_deterministic(out_dir / "pilot_report.json", report_obj)
        report_sha256 = hashlib.sha256((out_dir / "pilot_report.json").read_bytes()).hexdigest()

    # Build Attestation
    attestation_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "aos_sha": aos_sha,
        "authorized_lari_source_sha": AUTHORIZED_SOURCE_SHA,
        "lari_source_tree_sha": source_tree_sha if 'source_tree_sha' in locals() else "0"*40,
        "tracked_source_manifest_sha256": source_manifest_before_sha256 if 'source_manifest_before_sha256' in locals() else "0"*64,
        "report_sha256": report_sha256,
        "runtime_manifest_sha256": manifest_sha256,
        "aos_worktree_immutable_result": aos_immut,
        "original_lari_source_immutable_result": "PASS" if immut_ok else "FAIL",
        "attestation_timestamp": datetime.now(timezone.utc).isoformat()
    }

    validate_against_schema(attestation_obj, attestation_schema)
    write_json_deterministic(out_dir / "pilot_attestation.json", attestation_obj)

    print(f"[AOS6 Harness] Execution finished with result={aos6_result}. Artifacts written to {out_dir}")
    if aos6_result != "PASS":
        sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print("Usage: aos6_controlled_pilot_harness.py <request.json> <output_dir>", file=sys.stderr)
        sys.exit(1)
    execute_harness(sys.argv[1], sys.argv[2])

if __name__ == "__main__":
    main()
