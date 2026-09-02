#!/usr/bin/env python3
"""
AOS6 Controlled Pilot Harness CLI (Python 3.12+)
Full production orchestration harness for isolated controlled pilot.
PREP-1.3 Final Readiness Guard Closure.
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

def capture_aos_start_sha(runner):
    res = runner.run(["git", "rev-parse", "HEAD"])
    sha = res.stdout.strip()
    if not re.match(r"^[0-9a-f]{40}$", sha):
        raise RuntimeError(f"Invalid aos_start_sha format: '{sha}'")
    return sha

def verify_aos_immutability(aos_start_sha, runner):
    try:
        res_head = runner.run(["git", "rev-parse", "HEAD"])
        final_sha = res_head.stdout.strip()
        res_status = runner.run(["git", "status", "--porcelain=v1", "--untracked-files=all"])
        status_out = res_status.stdout.strip()
        res_diff = runner.run(["git", "diff", "--exit-code"], check=False)
        diff_rc = res_diff.returncode
        if final_sha == aos_start_sha and not status_out and diff_rc == 0:
            return True, final_sha
        return False, final_sha
    except Exception:
        return False, None

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

    expected_files = set()
    expected_dirs = {Path(".")}

    for rel_path in source_manifest_entries.keys():
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Invalid path in manifest entries: {rel_path}")
        expected_files.add(p)
        curr = p.parent
        while curr != Path(".") and curr != Path(""):
            expected_dirs.add(curr)
            curr = curr.parent

    # Enumerate workspace recursively without following symlinks
    for root, dirs, files in os.walk(ws_path, followlinks=False):
        rel_root = Path(root).relative_to(ws_path)
        if rel_root not in expected_dirs:
            raise RuntimeError(f"Unexpected directory in disposable workspace: {rel_root}")

        for d in dirs:
            dir_p = rel_root / d
            full_d = ws_path / dir_p
            if full_d.is_symlink():
                raise RuntimeError(f"Symlink directory forbidden in workspace: {dir_p}")
            if dir_p not in expected_dirs:
                raise RuntimeError(f"Unexpected directory in workspace: {dir_p}")

        for f in files:
            file_p = rel_root / f
            full_f = ws_path / file_p
            if full_f.is_symlink():
                raise RuntimeError(f"Symlink file forbidden in workspace: {file_p}")
            if file_p not in expected_files:
                raise RuntimeError(f"Unexpected file in workspace: {file_p}")

    ws_entries = {}
    aggregate_sha = hashlib.sha256()

    for rel_path, expected_info in sorted(source_manifest_entries.items()):
        p = Path(rel_path)
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

def validate_docker_inspect_data(inspect_data, temp_workspace_dir, driver_src_path):
    if not isinstance(inspect_data, (dict, list)):
        raise RuntimeError("Docker inspect data must be a dict or list")
    if isinstance(inspect_data, list):
        if len(inspect_data) == 0:
            raise RuntimeError("Docker inspect array is empty")
        inspect_obj = inspect_data[0]
    else:
        inspect_obj = inspect_data

    if not isinstance(inspect_obj, dict):
        raise RuntimeError("Docker inspect element is not an object")

    if "HostConfig" not in inspect_obj or not isinstance(inspect_obj["HostConfig"], dict):
        raise RuntimeError("HostConfig missing or invalid in docker inspect")
    if "Mounts" not in inspect_obj or not isinstance(inspect_obj["Mounts"], list):
        raise RuntimeError("Mounts missing or invalid in docker inspect")

    host_config = inspect_obj["HostConfig"]
    net_mode = host_config.get("NetworkMode")
    readonly_root = host_config.get("ReadonlyRootfs")
    pids_lim = host_config.get("PidsLimit")
    cap_drop = host_config.get("CapDrop") or []
    sec_opt = host_config.get("SecurityOpt") or []
    mounts = inspect_obj["Mounts"]

    if net_mode != "none":
        raise RuntimeError(f"NetworkMode must be 'none', got '{net_mode}'")
    if readonly_root is not True:
        raise RuntimeError(f"ReadonlyRootfs must be true, got '{readonly_root}'")
    if pids_lim != 100:
        raise RuntimeError(f"PidsLimit must be 100, got '{pids_lim}'")

    cap_drop_has_all = any(str(c).upper() == "ALL" for c in cap_drop)
    if not cap_drop_has_all:
        raise RuntimeError("CapDrop MUST contain 'ALL'")

    no_new_priv = "no-new-privileges" in sec_opt
    if not no_new_priv:
        raise RuntimeError("SecurityOpt MUST contain 'no-new-privileges'")

    ws_dest = "/workspace"
    drv_dest = "/aos-driver/aos6_controlled_pilot_driver.mjs"
    expected_destinations = {ws_dest, drv_dest}

    ws_mounts = [m for m in mounts if m.get("Destination") == ws_dest]
    drv_mounts = [m for m in mounts if m.get("Destination") == drv_dest]

    if len(ws_mounts) != 1:
        raise RuntimeError(f"Workspace mount count at '{ws_dest}' must be exactly 1, got {len(ws_mounts)}")
    if len(drv_mounts) != 1:
        raise RuntimeError(f"Driver mount count at '{drv_dest}' must be exactly 1, got {len(drv_mounts)}")
    if len(mounts) != 2:
        raise RuntimeError(f"Total mount count must be exactly 2, got {len(mounts)}")

    ws_m = ws_mounts[0]
    drv_m = drv_mounts[0]

    if ws_m.get("Type") != "bind":
        raise RuntimeError(f"Workspace mount Type must be 'bind', got '{ws_m.get('Type')}'")
    if ws_m.get("RW") is not False:
        raise RuntimeError("Workspace mount MUST be read-only (RW=false)")
    ws_src_res = Path(ws_m.get("Source", "")).resolve()
    ws_expected_res = Path(temp_workspace_dir).resolve()
    if ws_src_res != ws_expected_res:
        raise RuntimeError(f"Workspace mount Source '{ws_src_res}' != expected '{ws_expected_res}'")

    if drv_m.get("Type") != "bind":
        raise RuntimeError(f"Driver mount Type must be 'bind', got '{drv_m.get('Type')}'")
    if drv_m.get("RW") is not False:
        raise RuntimeError("Driver mount MUST be read-only (RW=false)")
    drv_src_res = Path(drv_m.get("Source", "")).resolve()
    drv_expected_res = Path(driver_src_path).resolve()
    if drv_src_res != drv_expected_res:
        raise RuntimeError(f"Driver mount Source '{drv_src_res}' != expected '{drv_expected_res}'")

    docker_socket_count = sum(1 for m in mounts if "docker.sock" in str(m.get("Source", "")) or "docker.sock" in str(m.get("Destination", "")))
    if docker_socket_count > 0:
        raise RuntimeError("Docker socket mount detected!")

    cred_mount_count = sum(1 for m in mounts if any(k in str(m.get("Source", "")) or k in str(m.get("Destination", "")) for k in [".ssh", ".aws", ".gcp", "supabase", "vercel"]))
    if cred_mount_count > 0:
        raise RuntimeError("Credential directory mount detected!")

    unexpected_bind_count = sum(1 for m in mounts if m.get("Destination") not in expected_destinations)
    if unexpected_bind_count > 0:
        raise RuntimeError(f"Unexpected bind mount count: {unexpected_bind_count}")

    return {
        "network_mode": net_mode,
        "readonly_rootfs": readonly_root,
        "pids_limit": pids_lim,
        "cap_drop_has_all": cap_drop_has_all,
        "no_new_privileges": no_new_priv,
        "workspace_mount_readonly": True,
        "driver_mount_readonly": True,
        "docker_socket_mount_count": docker_socket_count,
        "credential_directory_mount_count": cred_mount_count,
        "unexpected_host_bind_mount_count": unexpected_bind_count
    }

def parse_and_validate_driver_terminal_result(output_text):
    result_lines = [line for line in output_text.splitlines() if line.startswith("AOS6_PILOT_DRIVER_RESULT=")]
    if len(result_lines) == 0:
        raise RuntimeError("Zero AOS6_PILOT_DRIVER_RESULT terminal lines found")
    if len(result_lines) > 1:
        raise RuntimeError(f"Multiple AOS6_PILOT_DRIVER_RESULT lines found: {len(result_lines)}")

    raw_json = result_lines[0].split("=", 1)[1]
    try:
        res_obj = json.loads(raw_json)
    except Exception as e:
        raise RuntimeError(f"Malformed JSON in driver terminal result: {e}")

    if not isinstance(res_obj, dict):
        raise RuntimeError("Driver terminal result JSON is not an object")

    base_keys = {
        "product_static_qa_attempt_count",
        "product_static_qa_result",
        "policy_module_boot_result",
        "unsafe_grounding_result",
        "safe_grounding_result",
        "localization_result",
        "no_key_provider_result",
        "mock_provider_success_result",
        "mock_provider_failure_result",
        "mock_provider_call_count",
        "real_provider_network_call_count",
        "bounded_workflow_result"
    }

    actual_keys = set(res_obj.keys())
    bounded_res = res_obj.get("bounded_workflow_result")

    if bounded_res == "PASS":
        if actual_keys != base_keys:
            missing = base_keys - actual_keys
            extra = actual_keys - base_keys
            raise RuntimeError(f"Exact key mismatch in successful driver result. Missing: {missing}, Extra: {extra}")
    else:
        allowed = base_keys | {"error"}
        if not base_keys.issubset(actual_keys) or not actual_keys.issubset(allowed):
            missing = base_keys - actual_keys
            extra = actual_keys - allowed
            raise RuntimeError(f"Exact key mismatch in failure driver result. Missing: {missing}, Extra: {extra}")

    int_keys = {"product_static_qa_attempt_count", "mock_provider_call_count", "real_provider_network_call_count"}
    enum_keys = {
        "product_static_qa_result",
        "policy_module_boot_result",
        "unsafe_grounding_result",
        "safe_grounding_result",
        "localization_result",
        "no_key_provider_result",
        "mock_provider_success_result",
        "mock_provider_failure_result",
        "bounded_workflow_result"
    }

    for k in int_keys:
        v = res_obj[k]
        if type(v) is not int:
            raise RuntimeError(f"Driver result key '{k}' must be int, got {type(v).__name__}")

    for k in enum_keys:
        v = res_obj[k]
        if v not in ("PASS", "FAIL", "NOT_RUN"):
            raise RuntimeError(f"Driver result key '{k}' invalid enum value: '{v}'")

    if "error" in res_obj and not isinstance(res_obj["error"], str):
        raise RuntimeError("Driver result key 'error' must be str")

    return res_obj

def derive_pilot_result(obs):
    checks = [
        obs.get("primary_failure") is None,
        obs.get("secondary_cleanup_failure") is None,
        obs.get("authorized_source_acquisition_count") == 1,
        obs.get("exact_source_head") == AUTHORIZED_SOURCE_SHA,
        obs.get("source_baseline_manifest_exists") is True,
        obs.get("workspace_verification") == "PASS",
        obs.get("dependency_preparation") == "PASS",
        obs.get("target_image_id_valid") is True,
        obs.get("target_repo_digest_valid") is True,
        obs.get("container_inspection_exists") is True,
        obs.get("network_mode") == "none",
        obs.get("readonly_rootfs") is True,
        obs.get("pids_limit") == 100,
        obs.get("cap_drop_has_all") is True,
        obs.get("no_new_privileges") is True,
        obs.get("workspace_mount_exact_ro") is True,
        obs.get("driver_mount_exact_ro") is True,
        obs.get("docker_socket_count") == 0,
        obs.get("credential_mount_count") == 0,
        obs.get("unexpected_bind_count") == 0,
        obs.get("driver_result_exact_key_validation") == "PASS",
        obs.get("product_static_qa_attempt_count") == 1,
        obs.get("product_static_qa_result") == "PASS",
        obs.get("policy_module_boot_result") == "PASS",
        obs.get("unsafe_grounding_result") == "PASS",
        obs.get("safe_grounding_result") == "PASS",
        obs.get("localization_result") == "PASS",
        obs.get("no_key_provider_result") == "PASS",
        obs.get("mock_provider_success_result") == "PASS",
        obs.get("mock_provider_failure_result") == "PASS",
        obs.get("bounded_workflow_result") == "PASS",
        obs.get("mock_provider_call_count") == 2,
        obs.get("real_provider_network_call_count") == 0,
        obs.get("synthetic_data_only_result") == "PASS",
        obs.get("source_mutation_count") == 0,
        obs.get("canonical_lari_mutation_count") == 0,
        obs.get("canonical_remote_access_count") == 0,
        obs.get("lari_e3_project_access_count") == 0,
        obs.get("shared_staging_access_count") == 0,
        obs.get("production_access_count") == 0,
        obs.get("vercel_access_count") == 0,
        obs.get("real_customer_data_access_count") == 0,
        obs.get("real_whatsapp_send_count") == 0,
        obs.get("real_sms_send_count") == 0,
        obs.get("real_email_send_count") == 0,
        obs.get("real_payment_count") == 0,
        obs.get("attempt_count") == 1,
        obs.get("retry_count") == 0,
        obs.get("resource_created_count") == 1,
        obs.get("cleanup_attempt_count") == 1,
        obs.get("cleanup_success_count") == 1,
        obs.get("cleanup_failure_count") == 0,
        obs.get("surviving_disposable_resource_count") == 0,
        obs.get("original_lari_source_final_immutability") == "PASS",
        obs.get("aos_exact_start_sha_final_immutability") == "PASS",
        obs.get("final_report_manifest_verification") == "PASS",
        obs.get("evidence_capture_result") == "PASS",
    ]
    return "PASS" if all(checks) else "FAIL"

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
    if set(binding.keys()) != {"manifest_filename", "manifest_sha256", "manifest_schema_version"}:
        raise RuntimeError("runtime_evidence_binding has unknown or missing fields")

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

    aos_start_sha = capture_aos_start_sha(runner)

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

    target_image_id = None
    target_repo_digest = None
    container_inspection_obs = None
    immut_ok = None
    ws_verified = "FAIL"
    dep_prep_result = "NOT_CHECKED"
    driver_result = None
    driver_val_res = "FAIL"
    exact_source_head = None
    source_manifest_before_sha256 = None
    source_tree_sha = None
    final_sha256 = None

    try:
        # Phase 1: Source Acquisition
        print("[AOS6 Harness] Phase 1: Source Acquisition...")
        temp_source_dir.mkdir(parents=True, exist_ok=True)
        runner.run(["git", "init"], cwd=temp_source_dir)
        runner.run(["git", "remote", "add", "source", FIXED_SOURCE_REPO_URL], cwd=temp_source_dir)

        runner.run(["git", "fetch", "--no-tags", "--depth=1", "source", AUTHORIZED_SOURCE_SHA], cwd=temp_source_dir)
        authorized_source_acquisition_count = 1

        fetch_head_sha = runner.run(["git", "rev-parse", "FETCH_HEAD"], cwd=temp_source_dir).stdout.strip()
        if fetch_head_sha != AUTHORIZED_SOURCE_SHA:
            raise RuntimeError(f"FETCH_HEAD SHA mismatch: {fetch_head_sha} != {AUTHORIZED_SOURCE_SHA}")

        runner.run(["git", "checkout", "--detach", AUTHORIZED_SOURCE_SHA], cwd=temp_source_dir)
        exact_source_head = runner.run(["git", "rev-parse", "HEAD"], cwd=temp_source_dir).stdout.strip()
        if exact_source_head != AUTHORIZED_SOURCE_SHA:
            raise RuntimeError(f"HEAD SHA mismatch after checkout: {exact_source_head} != {AUTHORIZED_SOURCE_SHA}")

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

        ws_manifest_entries, ws_sha256 = verify_workspace_against_source_manifest(temp_workspace_dir, source_manifest_entries)
        if ws_sha256 != source_manifest_before_sha256:
            raise RuntimeError("Disposable workspace copy sha256 does not match original source manifest!")
        ws_verified = "PASS"

        # Phase 4: Dependency Preparation
        print("[AOS6 Harness] Phase 4: Dependency Preparation...")
        lockfile = temp_workspace_dir / "package-lock.json"
        if not lockfile.exists():
            raise RuntimeError("package-lock.json missing in workspace!")

        runner.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=temp_workspace_dir)
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
            raw_inspect = json.loads(inspect_res.stdout)
        except Exception as e:
            raise RuntimeError(f"Failed to parse docker inspect JSON: {e}")

        container_inspection_obs = validate_docker_inspect_data(raw_inspect, temp_workspace_dir, driver_src_path)

        # Phase 8: Bounded Execution & Exactly-Once Terminal Result Verification
        print("[AOS6 Harness] Phase 8: Executing Target Container...")
        attempt_count = 1
        start_res = runner.run(["docker", "start", "-a", container_name], check=False)
        driver_exit_code = start_res.returncode

        # Immediately persist logs BEFORE parsing or checking return code
        stdout_bytes = start_res.stdout.encode("utf-8")
        stderr_bytes = start_res.stderr.encode("utf-8")

        stdout_file = out_dir / "pilot_driver_stdout.log"
        stderr_file = out_dir / "pilot_driver_stderr.log"

        stdout_file.write_bytes(stdout_bytes)
        stderr_file.write_bytes(stderr_bytes)

        stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_sha256 = hashlib.sha256(stderr_bytes).hexdigest()

        driver_stdout_filename = "pilot_driver_stdout.log"
        driver_stderr_filename = "pilot_driver_stderr.log"

        combined_out = start_res.stdout + "\n" + start_res.stderr

        try:
            driver_result = parse_and_validate_driver_terminal_result(combined_out)
            driver_val_res = "PASS"
            terminal_result_parse_status = "PASS"

            # Persist parsed terminal result
            terminal_result_bytes = write_json_deterministic(out_dir / "pilot_driver_terminal_result.json", driver_result)
            terminal_result_filename = "pilot_driver_terminal_result.json"
            terminal_result_sha256 = hashlib.sha256(terminal_result_bytes).hexdigest()

            mock_provider_call_count = driver_result.get("mock_provider_call_count", 0)
            real_provider_network_call_count = driver_result.get("real_provider_network_call_count", 0)

            p1_res = driver_result.get("product_static_qa_result", "NOT_RUN")
            p2_res = driver_result.get("policy_module_boot_result", "NOT_RUN")
            p3_res = driver_result.get("bounded_workflow_result", "NOT_RUN")

            if p1_res == "FAIL":
                first_failed_step = "P1_STATIC_QA"
                sanitized_primary_failure_reason = "STEP_P1_STATIC_QA_FAILED"
            elif p2_res == "FAIL":
                first_failed_step = "P2_POLICY_BOOT"
                sanitized_primary_failure_reason = "STEP_P2_POLICY_BOOT_FAILED"
            elif p3_res == "FAIL":
                first_failed_step = "P3_GROUNDED_POLICY_MATRIX"
                sanitized_primary_failure_reason = "STEP_P3_GROUNDED_POLICY_MATRIX_FAILED"

            if start_res.returncode != 0 or driver_result.get("bounded_workflow_result") != "PASS":
                primary_failure = sanitized_primary_failure_reason or f"TARGET_CONTAINER_DRIVER_EXIT_{start_res.returncode}"

        except Exception as parse_err:
            print(f"[AOS6 Harness] Driver terminal result parse error: {parse_err}", file=sys.stderr)
            terminal_result_parse_status = "FAIL"
            primary_failure = f"DRIVER_TERMINAL_RESULT_PARSE_FAILED: {parse_err}"
            first_failed_step = "TERMINAL_RESULT_PARSING"

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
    aos_immut_bool, final_aos_sha = verify_aos_immutability(aos_start_sha, runner)
    aos_immut = "PASS" if aos_immut_bool else "FAIL"

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

    final_pair_verified = False

    observations = {
        "primary_failure": primary_failure,
        "secondary_cleanup_failure": secondary_cleanup_failure,
        "authorized_source_acquisition_count": authorized_source_acquisition_count,
        "exact_source_head": exact_source_head,
        "source_baseline_manifest_exists": source_manifest_before_sha256 is not None,
        "workspace_verification": ws_verified,
        "dependency_preparation": dep_prep_result,
        "target_image_id_valid": target_image_id is not None,
        "target_repo_digest_valid": target_repo_digest is not None,
        "container_inspection_exists": container_inspection_obs is not None,
        "network_mode": container_inspection_obs.get("network_mode") if container_inspection_obs else None,
        "readonly_rootfs": container_inspection_obs.get("readonly_rootfs") if container_inspection_obs else None,
        "pids_limit": container_inspection_obs.get("pids_limit") if container_inspection_obs else None,
        "cap_drop_has_all": container_inspection_obs.get("cap_drop_has_all") if container_inspection_obs else None,
        "no_new_privileges": container_inspection_obs.get("no_new_privileges") if container_inspection_obs else None,
        "workspace_mount_exact_ro": container_inspection_obs.get("workspace_mount_readonly") if container_inspection_obs else None,
        "driver_mount_exact_ro": container_inspection_obs.get("driver_mount_readonly") if container_inspection_obs else None,
        "docker_socket_count": container_inspection_obs.get("docker_socket_mount_count") if container_inspection_obs else None,
        "credential_mount_count": container_inspection_obs.get("credential_directory_mount_count") if container_inspection_obs else None,
        "unexpected_bind_count": container_inspection_obs.get("unexpected_host_bind_mount_count") if container_inspection_obs else None,
        "driver_result_exact_key_validation": driver_val_res,
        "product_static_qa_attempt_count": driver_result.get("product_static_qa_attempt_count") if driver_result else 0,
        "product_static_qa_result": driver_result.get("product_static_qa_result") if driver_result else "FAIL",
        "policy_module_boot_result": driver_result.get("policy_module_boot_result") if driver_result else "FAIL",
        "unsafe_grounding_result": driver_result.get("unsafe_grounding_result") if driver_result else "FAIL",
        "safe_grounding_result": driver_result.get("safe_grounding_result") if driver_result else "FAIL",
        "localization_result": driver_result.get("localization_result") if driver_result else "FAIL",
        "no_key_provider_result": driver_result.get("no_key_provider_result") if driver_result else "FAIL",
        "mock_provider_success_result": driver_result.get("mock_provider_success_result") if driver_result else "FAIL",
        "mock_provider_failure_result": driver_result.get("mock_provider_failure_result") if driver_result else "FAIL",
        "bounded_workflow_result": driver_result.get("bounded_workflow_result") if driver_result else "FAIL",
        "mock_provider_call_count": mock_provider_call_count,
        "real_provider_network_call_count": real_provider_network_call_count,
        "synthetic_data_only_result": synthetic_data_only_result,
        "source_mutation_count": source_mutation_count,
        "canonical_lari_mutation_count": canonical_lari_mutation_count,
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
        "attempt_count": attempt_count,
        "retry_count": retry_count,
        "resource_created_count": resource_created_count,
        "cleanup_attempt_count": cleanup_attempt_count,
        "cleanup_success_count": cleanup_success_count,
        "cleanup_failure_count": cleanup_failure_count,
        "surviving_disposable_resource_count": surviving_disposable_resource_count,
        "original_lari_source_final_immutability": "PASS" if immut_ok else "FAIL",
        "aos_exact_start_sha_final_immutability": aos_immut,
        "final_report_manifest_verification": "NOT_CHECKED",
        "evidence_capture_result": "NOT_CHECKED"
    }

    # 1. Build & write Runtime Manifest
    manifest_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "target_image_name": TARGET_IMAGE_NAME if target_image_id else None,
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
            "original_source_tree_sha256_pre": source_manifest_before_sha256,
            "original_source_tree_sha256_post": final_sha256 if 'final_sha256' in locals() else None,
            "immutable": immut_ok
        },
        "dependency_preparation": {
            "location": "DISPOSABLE_WORKSPACE_COPY" if dep_prep_result == "PASS" else None,
            "command": "npm ci --ignore-scripts --no-audit --no-fund" if dep_prep_result == "PASS" else None,
            "result": dep_prep_result,
            "lifecycle_scripts_disabled": True if dep_prep_result == "PASS" else None
        },
        "workflow_execution": {
            "step_p1_static_qa": driver_result.get("product_static_qa_result", "NOT_RUN") if driver_result else "NOT_RUN",
            "step_p2_policy_boot": driver_result.get("policy_module_boot_result", "NOT_RUN") if driver_result else "NOT_RUN",
            "step_p3_grounded_policy_matrix": {
                "unsafe_promise_rejected": driver_result.get("unsafe_grounding_result") == "PASS" if driver_result else None,
                "safe_request_accepted": driver_result.get("safe_grounding_result") == "PASS" if driver_result else None,
                "localized_responses_produced": driver_result.get("localization_result") == "PASS" if driver_result else None,
                "missing_key_503_produced": driver_result.get("no_key_provider_result") == "PASS" if driver_result else None,
                "mock_fetch_success_produced": driver_result.get("mock_provider_success_result") == "PASS" if driver_result else None,
                "mock_fetch_exception_503_produced": driver_result.get("mock_provider_failure_result") == "PASS" if driver_result else None
            }
        },
        "driver_evidence": {
            "stdout_filename": driver_stdout_filename if 'driver_stdout_filename' in locals() else None,
            "stdout_sha256": stdout_sha256 if 'stdout_sha256' in locals() else None,
            "stderr_filename": driver_stderr_filename if 'driver_stderr_filename' in locals() else None,
            "stderr_sha256": stderr_sha256 if 'stderr_sha256' in locals() else None,
            "terminal_result_filename": terminal_result_filename if 'terminal_result_filename' in locals() else None,
            "terminal_result_sha256": terminal_result_sha256 if 'terminal_result_sha256' in locals() else None,
            "terminal_result_parse_status": terminal_result_parse_status if 'terminal_result_parse_status' in locals() else "NOT_RUN",
            "driver_exit_code": driver_exit_code if 'driver_exit_code' in locals() else None,
            "first_failed_step": first_failed_step,
            "sanitized_primary_failure_reason": sanitized_primary_failure_reason if 'sanitized_primary_failure_reason' in locals() else primary_failure
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

    # 2. Build initial Report (evidence_capture_result = "NOT_CHECKED")
    report_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "aos_canonical_binding_sha": aos_start_sha,
        "lari_source_sha": AUTHORIZED_SOURCE_SHA,
        "pilot_execution_environment": "AOS_OWNED_ISOLATED_DISPOSABLE_SYNTHETIC_NONCANONICAL",
        "aos6_controlled_pilot_result": "FAIL",
        "exact_sha_result": "PASS" if exact_source_head == AUTHORIZED_SOURCE_SHA else "FAIL",
        "environment_isolation_result": "PASS" if container_inspection_obs else "FAIL",
        "synthetic_data_only_result": synthetic_data_only_result,
        "runtime_boot_result": driver_result.get("policy_module_boot_result", "NOT_RUN") if driver_result else "NOT_RUN",
        "runtime_boot_class": "NODE_TSX_PRODUCT_POLICY_MODULE",
        "bounded_workflow_result": driver_result.get("bounded_workflow_result", "NOT_RUN") if driver_result else "NOT_RUN",
        "evidence_capture_result": "NOT_CHECKED",
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
        "blocker_if_any": sanitized_primary_failure_reason if 'sanitized_primary_failure_reason' in locals() and sanitized_primary_failure_reason else primary_failure,
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
    write_json_deterministic(out_dir / "pilot_report.json", report_obj)

    # 3. Two-stage verification: verify initial report & manifest on disk
    try:
        if verify_report_manifest_pair(out_dir / "pilot_report.json", out_dir / "pilot_runtime_manifest.json", report_schema, manifest_schema):
            observations["final_report_manifest_verification"] = "PASS"
            observations["evidence_capture_result"] = "PASS"

            aos6_result = derive_pilot_result(observations)
            report_obj["aos6_controlled_pilot_result"] = aos6_result
            report_obj["evidence_capture_result"] = "PASS"

            validate_against_schema(report_obj, report_schema)
            write_json_deterministic(out_dir / "pilot_report.json", report_obj)

            if verify_report_manifest_pair(out_dir / "pilot_report.json", out_dir / "pilot_runtime_manifest.json", report_schema, manifest_schema):
                final_pair_verified = True
    except Exception as pair_err:
        print(f"[AOS6 Harness] Final pair re-validation failed: {pair_err}", file=sys.stderr)

    if not final_pair_verified:
        observations["final_report_manifest_verification"] = "FAIL"
        observations["evidence_capture_result"] = "FAIL"
        aos6_result = "FAIL"
        report_obj["aos6_controlled_pilot_result"] = "FAIL"
        report_obj["evidence_capture_result"] = "FAIL"
        validate_against_schema(report_obj, report_schema)
        write_json_deterministic(out_dir / "pilot_report.json", report_obj)

    final_report_bytes = (out_dir / "pilot_report.json").read_bytes()
    final_report_sha256 = hashlib.sha256(final_report_bytes).hexdigest()

    attestation_obj = {
        "schema_version": "0.1.0",
        "pilot_run_id": pilot_run_id,
        "aos_sha": aos_start_sha,
        "authorized_lari_source_sha": AUTHORIZED_SOURCE_SHA,
        "lari_source_tree_sha": source_tree_sha if 'source_tree_sha' in locals() and source_tree_sha else "0"*40,
        "tracked_source_manifest_sha256": source_manifest_before_sha256 if source_manifest_before_sha256 else "0"*64,
        "report_sha256": final_report_sha256,
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
