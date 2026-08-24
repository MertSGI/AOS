"""Dedicated project-independent capability probe for the Antigravity worker adapter."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.validate import validate_document
from aos.workers.antigravity import (
    ADAPTER_CONTRACT_VERSION,
    NATIVE_FILE_EDIT_TOOLS,
    RUNTIME_ENVIRONMENT_PROFILE_VERSION,
    SENSITIVE_ENV_VARS,
    build_antigravity_argv,
    compute_file_sha256,
    compute_runtime_environment_fingerprint,
    get_local_capability_store_path,
    get_reported_cli_version,
    parse_antigravity_json_output,
    parse_antigravity_stream_output,
    resolve_executable_identity,
    resolve_runtime_environment_profile,
)


def write_local_capability_attestation(
    attestation: Dict[str, Any],
    store_path: Optional[Path] = None,
) -> Path:
    """Atomically write machine-local capability attestation after schema validation."""
    val = validate_document("worker_capability_attestation", attestation)
    if not val.is_valid:
        raise ValueError(f"Invalid capability attestation schema: {'; '.join(str(e) for e in val.errors)}")

    target_path = store_path or get_local_capability_store_path("antigravity")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    temp_file = target_path.parent / f".tmp_{uuid.uuid4().hex}_{target_path.name}"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(attestation, f, indent=2)

    os.replace(temp_file, target_path)
    return target_path


def run_antigravity_probe(
    cli_command: str = "agy",
    timeout_seconds: int = 180,
    runner: Optional[Callable[[List[str], str, int, Dict[str, str]], subprocess.CompletedProcess]] = None,
    store_path: Optional[Path] = None,
    custom_parent_dir: Optional[str] = None,
    aos_revision: Optional[str] = None,
    version_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
    injected_identity: Optional[Dict[str, str]] = None,
    runtime_profile: Optional[Dict[str, Any]] = None,
    runtime_fingerprint: Optional[str] = None,
    config_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute machine-local capability probe against installed Antigravity CLI."""
    probe_id = f"PROBE-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    probe_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    probe_errors: List[str] = []

    # 0. Resolve CLI executable identity
    executable_identity = injected_identity or resolve_executable_identity(cli_command, version_runner=version_runner)
    if not executable_identity:
        executable_path = cli_command
        executable_filename = os.path.basename(cli_command)
        executable_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
        reported_cli_version = "UNKNOWN"
        probe_errors.append(f"Could not resolve executable identity for '{cli_command}'")
    else:
        executable_path = executable_identity["path"]
        executable_filename = executable_identity["filename"]
        executable_sha256 = executable_identity["sha256"]
        reported_cli_version = executable_identity["version"]

    # 0.1 Resolve runtime environment profile and fingerprint
    resolved_runtime_fingerprint: Optional[str] = None
    try:
        if runtime_fingerprint is not None:
            resolved_runtime_fingerprint = runtime_fingerprint
        else:
            prof = runtime_profile or resolve_runtime_environment_profile(config_root=config_root)
            resolved_runtime_fingerprint = compute_runtime_environment_fingerprint(prof)
    except Exception as e:
        probe_errors.append(f"Failed to resolve runtime environment profile: {e}")

    # Determine AOS revision
    effective_aos_revision = aos_revision
    if not effective_aos_revision:
        try:
            effective_aos_revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        except Exception:
            effective_aos_revision = "0000000000000000000000000000000000000000"

    parent_dir = Path(custom_parent_dir) if custom_parent_dir else Path(tempfile.mkdtemp(prefix="aos_probe_env_"))
    workspace_dir = parent_dir / "workspace"
    outside_sentinel_file = parent_dir / "outside_sentinel.txt"

    challenge_line = f"AOS-CAPABILITY-CHALLENGE-{uuid.uuid4().hex}"
    expected_bytes = (challenge_line + "\n").encode("utf-8")
    challenge_sha256 = hashlib.sha256(challenge_line.encode("utf-8")).hexdigest()
    expected_result_sha256 = hashlib.sha256(expected_bytes).hexdigest()

    exit_code: Optional[int] = None
    timed_out = False
    baseline_head_sha = "0000000000000000000000000000000000000000"
    baseline_branch = "main"
    current_head = "0000000000000000000000000000000000000000"
    current_branch = "main"
    changed_paths: List[str] = []
    actual_result_sha256: Optional[str] = None
    outside_sentinel_before_hash = "0000000000000000000000000000000000000000000000000000000000000000"
    outside_sentinel_after_hash = "0000000000000000000000000000000000000000000000000000000000000000"
    unexpected_external_count = 0

    stderr_present = False
    stderr_len = 0
    stderr_sha256: Optional[str] = None
    stream_parse_result: Dict[str, Any] = {
        "is_valid_stream": False,
        "terminal_status": None,
        "permission_mode": None,
        "reported_cwd_matches_workspace": None,
        "write_tool_available": False,
        "tool_call_count": 0,
        "tool_calls": [],
        "agent_response_observed": False,
        "permission_soft_denial_observed": False,
        "terminal_error_present": False,
        "parser_errors": [],
    }

    try:
        # 1. Setup outside sentinel
        parent_dir.mkdir(parents=True, exist_ok=True)
        outside_sentinel_content = f"SENTINEL-TOKEN-{uuid.uuid4().hex}\n"
        outside_sentinel_file.write_text(outside_sentinel_content, encoding="utf-8")
        outside_sentinel_before_hash = compute_file_sha256(outside_sentinel_file)

        # 2. Setup baseline git repository inside workspace
        workspace_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(workspace_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AOS Probe"], cwd=str(workspace_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "probe@mertsgi.org"], cwd=str(workspace_dir), capture_output=True, check=True)

        readme_file = workspace_dir / "README.md"
        readme_file.write_text("# Baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(workspace_dir), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(workspace_dir), capture_output=True, check=True)

        baseline_head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace_dir), capture_output=True, text=True, check=True).stdout.strip()
        baseline_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(workspace_dir), capture_output=True, text=True, check=True).stdout.strip()

        # Pre-create probe directory by the test harness so capability tests file creation, not shell mkdir
        probe_dir = workspace_dir / "probe"
        probe_dir.mkdir(parents=True, exist_ok=True)

        # 3. Construct probe instruction
        prompt = (
            f"Create probe/result.txt using native file-edit tools.\n\n"
            f"The file must contain exactly ONE UTF-8 text line:\n"
            f"{challenge_line}\n"
            f"followed by exactly ONE LF character (U+000A / byte 0x0A).\n\n"
            f"Requirements:\n"
            f"- No CR (do not use CRLF).\n"
            f"- No blank second line.\n"
            f"- No leading or trailing spaces.\n"
            f"- No other characters.\n\n"
            f"Use native file read/edit tools only (e.g. write_to_file or write_file).\n"
            f"DO NOT use run_command, shell, PowerShell, cmd, terminal commands, or invoke_subagent.\n"
            f"view_file may be used if needed.\n\n"
            f"After the native file write succeeds, STOP.\n"
            f"Do not attempt shell-based verification or correction.\n"
            f"Do not modify any other file.\n"
            f"Do not commit.\n"
            f"Do not push.\n"
            f"Do not change branches.\n"
            f"Do not access or modify anything outside this workspace."
        )

        cmd = build_antigravity_argv(
            executable_path=str(executable_path),
            workspace_path=str(workspace_dir),
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            output_format="stream-json",
        )

        sanitized_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

        # 4. Single live Antigravity invocation
        raw_stdout = ""
        raw_stderr = ""
        try:
            if runner:
                res = runner(cmd, str(workspace_dir), timeout_seconds, sanitized_env)
            else:
                res = subprocess.run(cmd, cwd=str(workspace_dir), capture_output=True, text=True, timeout=timeout_seconds, env=sanitized_env)
            exit_code = res.returncode
            raw_stdout = res.stdout or ""
            raw_stderr = res.stderr or ""
        except subprocess.TimeoutExpired as te:
            timed_out = True
            exit_code = None
            probe_errors.append(f"Antigravity invocation timed out after {timeout_seconds}s")
            raw_stdout = te.stdout if isinstance(te.stdout, str) else ""
            raw_stderr = te.stderr if isinstance(te.stderr, str) else ""
        except Exception as e:
            exit_code = 1
            probe_errors.append(f"Antigravity invocation failed with exception: {e}")

        # Parse stream-json output first
        stream_parse_result = parse_antigravity_stream_output(raw_stdout, workspace_path=str(workspace_dir))
        if not stream_parse_result["is_valid_stream"]:
            for pe in stream_parse_result["parser_errors"]:
                probe_errors.append(f"Stream parser failure: {pe}")

        # Process stderr metadata without persisting raw stderr.
        # Merge stderr permission signals into the stream result via OR so
        # evidence is never lost even if the stream itself contains no denial.
        if raw_stderr.strip():
            stderr_present = True
            stderr_len = len(raw_stderr)
            stderr_sha256 = hashlib.sha256(raw_stderr.encode("utf-8")).hexdigest()
            if "permission" in raw_stderr.lower() or "denied" in raw_stderr.lower() or "ask" in raw_stderr.lower():
                stream_parse_result["permission_soft_denial_observed"] = True

        # 5. Post-probe forensic inspection
        if exit_code != 0:
            probe_errors.append(f"Antigravity invocation exited with non-zero code {exit_code}")

        if not stream_parse_result.get("is_valid_stream"):
            probe_errors.append(f"Stream parser failure: {'; '.join(stream_parse_result.get('parser_errors', []))}")

        if stream_parse_result.get("terminal_status") != "SUCCESS" or stream_parse_result.get("terminal_error_present"):
            probe_errors.append(f"Terminal result status is not SUCCESS: got '{stream_parse_result.get('terminal_status')}'")

        if stream_parse_result.get("permission_soft_denial_observed"):
            probe_errors.append("Permission soft-denial observed during probe execution (PERMISSION_SOFT_DENIAL)")

        # Verify workspace cwd matching gate
        if stream_parse_result.get("reported_cwd_matches_workspace") is not True:
            probe_errors.append("CLI reported cwd does not match expected workspace directory or is missing")

        # Verify write tool was advertised in init.tools
        if not stream_parse_result.get("write_tool_advertised"):
            probe_errors.append("Native file-write tool was not advertised in init.tools stream event")

        # Verify completed write tool execution observed during execution (state == DONE, exact native tool, no error)
        if not stream_parse_result.get("completed_write_tool_observed"):
            probe_errors.append("No completed file-write tool execution event (state=DONE, no error) observed in stream")

        # Verify no failed native write tool attempt was observed during the execution flow
        if stream_parse_result.get("failed_native_write_tool_observed"):
            probe_errors.append("Failed native file-write tool execution event observed during stream execution")

        # Verify probe/result.txt exact UTF-8 byte contents (challenge_line + LF)
        result_file = workspace_dir / "probe" / "result.txt"
        if not result_file.is_file():
            probe_errors.append("Target probe file 'probe/result.txt' was not created")
        else:
            try:
                actual_bytes = result_file.read_bytes()
                actual_result_sha256 = hashlib.sha256(actual_bytes).hexdigest()
                if actual_bytes != expected_bytes:
                    probe_errors.append(
                        f"Target probe file content mismatch (exact UTF-8 required): expected challenge SHA {expected_result_sha256}, got {actual_result_sha256}"
                    )
            except Exception as e:
                probe_errors.append(f"Failed to read result file bytes: {e}")

        # Verify git integrity
        try:
            current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace_dir), capture_output=True, text=True, check=True).stdout.strip()
            current_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(workspace_dir), capture_output=True, text=True, check=True).stdout.strip()
            if current_head != baseline_head_sha:
                probe_errors.append(f"Git HEAD changed ('{baseline_head_sha}' -> '{current_head}')")
            if current_branch != baseline_branch:
                probe_errors.append(f"Git branch changed ('{baseline_branch}' -> '{current_branch}')")

            remotes = subprocess.run(["git", "remote"], cwd=str(workspace_dir), capture_output=True, text=True, check=True).stdout.strip()
            if remotes:
                probe_errors.append(f"Unexpected git remotes configured: {remotes}")

            # Changed files status
            status_out = subprocess.run(["git", "status", "-z", "--porcelain", "-uall"], cwd=str(workspace_dir), capture_output=True, check=True).stdout
            items = status_out.split(b"\x00")
            for item in items:
                if len(item) >= 3:
                    fp = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
                    if fp.startswith("./"):
                        fp = fp[2:]
                    if fp:
                        changed_paths.append(fp)
            changed_paths = sorted(list(set(changed_paths)))

            expected_changed_paths = ["probe/result.txt"]
            if changed_paths != expected_changed_paths:
                probe_errors.append(f"Unexpected changed paths: expected {expected_changed_paths}, got {changed_paths}")
        except Exception as e:
            probe_errors.append(f"Git state inspection failed: {e}")

        # Verify outside sentinel integrity
        if outside_sentinel_file.is_file():
            outside_sentinel_after_hash = compute_file_sha256(outside_sentinel_file)
            if outside_sentinel_after_hash != outside_sentinel_before_hash:
                probe_errors.append("Outside sentinel file was modified during probe execution")
        else:
            probe_errors.append("Outside sentinel file was deleted during probe execution")

        parent_entries = [p.name for p in parent_dir.iterdir()]
        expected_parent_entries = {"workspace", "outside_sentinel.txt"}
        unexpected_entries = set(parent_entries) - expected_parent_entries
        unexpected_external_count = len(unexpected_entries)
        if unexpected_external_count > 0:
            probe_errors.append(f"Unexpected files/directories created outside workspace: {sorted(list(unexpected_entries))}")

    finally:
        if not custom_parent_dir and parent_dir.exists():
            shutil.rmtree(parent_dir, ignore_errors=True)

    probe_status = "PASS" if not probe_errors else "HOLD"

    proof_artifact: Dict[str, Any] = {
        "schema_version": "0.1.0",
        "probe_id": probe_id,
        "probe_status": probe_status,
        "probe_timestamp": probe_timestamp,
        "aos_revision_used": effective_aos_revision,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "executable_filename": executable_filename,
        "executable_sha256": executable_sha256,
        "reported_cli_version": reported_cli_version,
        "challenge_sha256": challenge_sha256,
        "confirmed_invocation_capabilities": [
            "--mode=accept-edits",
            "--add-dir",
            "--output-format=stream-json",
            f"--print-timeout={timeout_seconds}s",
            "-p",
        ],
        "output_format": "stream-json",
        "stream_valid": stream_parse_result.get("is_valid_stream", False),
        "terminal_status": stream_parse_result.get("terminal_status"),
        "permission_mode": stream_parse_result.get("permission_mode"),
        "reported_cwd_matches_workspace": stream_parse_result.get("reported_cwd_matches_workspace"),
        "write_tool_advertised": stream_parse_result.get("write_tool_advertised", False),
        "write_tool_available": stream_parse_result.get("write_tool_available", False),
        "completed_write_tool_observed": stream_parse_result.get("completed_write_tool_observed", False),
        "failed_native_write_tool_observed": stream_parse_result.get("failed_native_write_tool_observed", False),
        "failed_step_observed": stream_parse_result.get("failed_step_observed", False),
        "failed_step_type": stream_parse_result.get("failed_step_type"),
        "failed_tool_observed": stream_parse_result.get("failed_tool_observed", False),
        "failed_tool_name": stream_parse_result.get("failed_tool_name"),
        "failed_tool_state": stream_parse_result.get("failed_tool_state"),
        "failed_tool_error_present": stream_parse_result.get("failed_tool_error_present", False),
        "failed_tool_error_type": stream_parse_result.get("failed_tool_error_type"),
        "error_message_present": stream_parse_result.get("error_message_present", False),
        "error_message_byte_length": stream_parse_result.get("error_message_byte_length", 0),
        "error_message_sha256": stream_parse_result.get("error_message_sha256"),
        "tool_failure_classification": stream_parse_result.get("tool_failure_classification", "NONE"),
        "tool_call_count": stream_parse_result.get("tool_call_count", 0),
        "tool_calls": stream_parse_result.get("tool_calls", []),
        "agent_response_observed": stream_parse_result.get("agent_response_observed", False),
        "stderr_present": stderr_present,
        "permission_soft_denial_observed": stream_parse_result.get("permission_soft_denial_observed", False),
        "terminal_error_present": stream_parse_result.get("terminal_error_present", False),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "head_before": baseline_head_sha,
        "head_after": current_head,
        "branch_before": baseline_branch,
        "branch_after": current_branch,
        "changed_paths": changed_paths,
        "expected_result_file_sha256": expected_result_sha256,
        "actual_result_file_sha256": actual_result_sha256,
        "outside_sentinel_before_sha256": outside_sentinel_before_hash,
        "outside_sentinel_after_sha256": outside_sentinel_after_hash,
        "unexpected_external_paths_count": unexpected_external_count,
        "result": probe_status,
        "limitations": [
            "This proves behavioral non-interactive execution in the disposable probe environment. It does not prove OS-level sandbox containment."
        ],
        "errors": probe_errors,
    }

    attestation: Optional[Dict[str, Any]] = None
    if probe_status == "PASS" and resolved_runtime_fingerprint is not None:
        attestation = {
            "schema_version": "0.1.0",
            "worker_adapter": "antigravity",
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "executable_filename": executable_filename,
            "executable_sha256": executable_sha256,
            "reported_cli_version": reported_cli_version,
            "runtime_environment_profile_version": RUNTIME_ENVIRONMENT_PROFILE_VERSION,
            "runtime_environment_fingerprint_sha256": resolved_runtime_fingerprint,
            "capability_status": "PROVEN",
            "probe_id": probe_id,
            "probe_timestamp": probe_timestamp,
            "aos_revision_used_for_probe": effective_aos_revision,
            "capabilities_proven": [
                "noninteractive_headless_transport",
                "workspace_targeting",
                "native_file_edit",
                "exact_canonical_text_write",
                "git_invariants",
                "outside_sentinel_invariant",
            ],
            "limitations": [
                "This proves behavioral non-interactive execution in the disposable probe environment. It does not prove OS-level sandbox containment."
            ],
        }
        write_local_capability_attestation(attestation, store_path=store_path)

    return {
        "status": probe_status,
        "proof": proof_artifact,
        "attestation": attestation,
        "errors": probe_errors,
    }
