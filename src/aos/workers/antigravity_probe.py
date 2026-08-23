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
    SENSITIVE_ENV_VARS,
    build_antigravity_argv,
    compute_file_sha256,
    get_local_capability_store_path,
    get_reported_cli_version,
    parse_antigravity_json_output,
    parse_antigravity_stream_output,
    resolve_executable_identity,
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
) -> Dict[str, Any]:
    """Execute the project-independent single-invocation Antigravity capability probe."""
    probe_errors: List[str] = []
    probe_id = f"PROBE-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    probe_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    parent_dir = Path(custom_parent_dir) if custom_parent_dir else Path(tempfile.mkdtemp(prefix="aos_probe_env_"))
    parent_dir.mkdir(parents=True, exist_ok=True)

    effective_aos_revision = aos_revision
    if not effective_aos_revision or len(effective_aos_revision) != 40:
        try:
            effective_aos_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            effective_aos_revision = "0000000000000000000000000000000000000000"

    identity = injected_identity or resolve_executable_identity(cli_command, version_runner=version_runner)
    if not identity:
        if runner:
            mock_bin_dir = Path(tempfile.gettempdir()) / "aos_mock_bin"
            mock_bin_dir.mkdir(parents=True, exist_ok=True)
            mock_exe = mock_bin_dir / f"{os.path.basename(cli_command)}"
            mock_exe.write_text("mock binary", encoding="utf-8")
            identity = {
                "path": str(mock_exe),
                "filename": os.path.basename(mock_exe),
                "sha256": compute_file_sha256(mock_exe),
                "version": "1.1.17",
            }
        else:
            return {
                "status": "HOLD",
                "errors": [f"Antigravity CLI executable '{cli_command}' could not be resolved on PATH"],
                "proof": None,
            }

    executable_path = identity["path"]
    executable_filename = identity["filename"]
    executable_sha256 = identity["sha256"]
    reported_cli_version = identity["version"]

    workspace_dir = parent_dir / "workspace"
    outside_sentinel_file = parent_dir / "outside_sentinel.txt"

    challenge = f"AOS-CAPABILITY-CHALLENGE-{uuid.uuid4().hex}"
    challenge_sha256 = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
    expected_result_sha256 = challenge_sha256

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
            f"Write the file probe/result.txt inside the current workspace.\n"
            f"Its complete UTF-8 contents must equal the exact runtime challenge with no extra characters or whitespace:\n"
            f"{challenge}\n\n"
            "Do not modify any other file.\n"
            "Do not commit.\n"
            "Do not push.\n"
            "Do not change branches.\n"
            "Do not access or modify anything outside this workspace.\n"
            "Stop when the file has been created."
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

        # Verify probe/result.txt exact UTF-8 contents
        result_file = workspace_dir / "probe" / "result.txt"
        if not result_file.is_file():
            probe_errors.append("Target probe file 'probe/result.txt' was not created")
        else:
            try:
                actual_content = result_file.read_text(encoding="utf-8")
                actual_result_sha256 = hashlib.sha256(actual_content.encode("utf-8")).hexdigest()
                if actual_content != challenge:
                    probe_errors.append(
                        f"Target probe file content mismatch (exact UTF-8 required): expected challenge SHA {expected_result_sha256}, got {actual_result_sha256}"
                    )
            except Exception as e:
                probe_errors.append(f"Failed to read result file as UTF-8: {e}")

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
            if changed_paths != ["probe/result.txt"]:
                probe_errors.append(f"Unexpected changed paths: expected ['probe/result.txt'], got {changed_paths}")
        except Exception as e:
            probe_errors.append(f"Git verification error: {e}")

        # Verify outside sentinel and parent directory
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
    if probe_status == "PASS":
        attestation = {
            "schema_version": "0.1.0",
            "worker_adapter": "antigravity",
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "executable_filename": executable_filename,
            "executable_sha256": executable_sha256,
            "reported_cli_version": reported_cli_version,
            "capability_status": "PROVEN",
            "probe_id": probe_id,
            "probe_timestamp": probe_timestamp,
            "aos_revision_used_for_probe": effective_aos_revision,
            "capabilities_proven": [
                "non_interactive_instruction",
                "workspace_execution",
                "observable_exit_status",
                "stream_json_observability",
                "controlled_file_edit",
                "no_commit_observed",
                "no_push_observed",
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
