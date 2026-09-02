"""
AOS6 Controlled Pilot Authority Binding Preflight Verification Module.

Provides deterministic, fail-closed validation of execution authority inputs,
executable vs authority revision separation, Git ancestry proof, governance-only
diff boundaries, and STATE + EVIDENCE cross-corroboration.
"""

import json
import os
import re
import sys
from pathlib import Path

EXPECTED_LARI_SOURCE_SHA = "cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a"
ALLOWED_GOVERNANCE_PATHS = {
    "docs/project-control/STATE.json",
    "docs/project-control/EVIDENCE.jsonl"
}

def parse_json_strict(raw_text: str):
    """
    Parses JSON text while strictly rejecting duplicate object keys.
    """
    def dict_raise_on_duplicates(ordered_pairs):
        d = {}
        for k, v in ordered_pairs:
            if k in d:
                raise ValueError(f"Duplicate JSON key detected: {k}")
            d[k] = v
        return d
    return json.loads(raw_text, object_pairs_hook=dict_raise_on_duplicates)

def validate_authority_id_format(authority_id: str) -> bool:
    """
    Validates authority_id against a conservative non-empty safe format regex.
    """
    if not isinstance(authority_id, str):
        return False
    return bool(re.match(r"^[A-Za-z0-9_\-\.\:\/]{1,128}$", authority_id))

def validate_sha_format(sha: str) -> bool:
    """
    Validates 40-char lower-hex SHA format.
    """
    if not isinstance(sha, str):
        return False
    return bool(re.match(r"^[0-9a-f]{40}$", sha))

def is_strict_int(val) -> bool:
    """
    Returns True if val is an int and NOT a bool.
    """
    return isinstance(val, int) and not isinstance(val, bool)

def verify_authority_preflight(
    authorized_execution_aos_sha: str,
    authority_evidence_sha: str,
    authority_id: str,
    executable_repo_dir: Path,
    authority_evidence_dir: Path,
    runner=None
):
    """
    Executes all preflight authority checks.
    Returns (True, None) on success or (False, error_message) on failure.
    """
    # Step 1: Input Format Validation
    if not validate_sha_format(authorized_execution_aos_sha):
        return False, "Invalid authorized_execution_aos_sha format (must be 40-hex lowercase)"

    if not validate_sha_format(authority_evidence_sha):
        return False, "Invalid authority_evidence_sha format (must be 40-hex lowercase)"

    if not validate_authority_id_format(authority_id):
        return False, "Invalid authority_id format (must be non-empty safe string <= 128 chars)"

    # Step 2: Executable HEAD check & Default Runner
    if runner is None:
        import subprocess
        class DefaultRunner:
            def run(self, cmd, cwd=None, env=None, check=True):
                res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=check)
                return res
        runner = DefaultRunner()

    executable_repo_dir = executable_repo_dir.resolve()
    authority_evidence_dir = authority_evidence_dir.resolve()

    try:
        exec_head = runner.run(["git", "rev-parse", "HEAD"], cwd=executable_repo_dir).stdout.strip()
        if exec_head != authorized_execution_aos_sha:
            return False, f"Executable HEAD mismatch: observed {exec_head}, expected {authorized_execution_aos_sha}"
    except Exception as e:
        return False, f"Failed to verify executable HEAD: {e}"

    # Step 3: Verify Authority Evidence Checkout Exact Revision
    try:
        auth_head = runner.run(["git", "rev-parse", "HEAD"], cwd=authority_evidence_dir).stdout.strip()
        if auth_head != authority_evidence_sha:
            return False, f"Authority checkout HEAD mismatch: observed {auth_head}, expected {authority_evidence_sha}"
    except Exception as e:
        return False, f"Failed to verify authority checkout HEAD: {e}"

    # Step 4: Verify Data Files in Authority Checkout (Regular files, no symlinks, resolved inside authority_evidence_dir)
    authority_state_file = authority_evidence_dir / "docs" / "project-control" / "STATE.json"
    authority_evidence_file = authority_evidence_dir / "docs" / "project-control" / "EVIDENCE.jsonl"

    for file_path, name in [(authority_state_file, "STATE.json"), (authority_evidence_file, "EVIDENCE.jsonl")]:
        if not file_path.exists():
            return False, f"Authority evidence checkout missing {name}"
        if file_path.is_symlink():
            return False, f"Authority evidence file {name} must not be a symbolic link"
        if not file_path.is_file():
            return False, f"Authority evidence file {name} is not a regular file"
        try:
            resolved_path = file_path.resolve()
            resolved_auth_dir = authority_evidence_dir.resolve()
            resolved_path.relative_to(resolved_auth_dir)
        except ValueError:
            return False, f"Authority evidence file {name} resolves outside authority checkout directory"

    # Step 5: Git Ancestry and Diff Verification (run inside executable repo directory)
    try:
        # Check if authority_evidence_sha is a valid commit object
        cat_check = runner.run(["git", "cat-file", "-t", authority_evidence_sha], cwd=executable_repo_dir, check=False)
        if cat_check.returncode != 0:
            return False, f"Authority evidence SHA {authority_evidence_sha} not found in Git database"

        # Check ancestry: merge-base between exec SHA and authority SHA must be exec SHA
        merge_base = runner.run(["git", "merge-base", authorized_execution_aos_sha, authority_evidence_sha], cwd=executable_repo_dir).stdout.strip()
        if merge_base != authorized_execution_aos_sha:
            return False, f"Ancestry failure: {authority_evidence_sha} is not a descendant of executable SHA {authorized_execution_aos_sha}"

        # Diff check with rename detection: git diff --name-status -M exec_sha..authority_sha
        diff_res = runner.run(["git", "diff", "--name-status", "-M", f"{authorized_execution_aos_sha}..{authority_evidence_sha}"], cwd=executable_repo_dir).stdout.strip()
        lines = [l.strip() for l in diff_res.splitlines() if l.strip()]

        for line in lines:
            parts = line.split()
            if not parts:
                continue
            status = parts[0]
            # Checked paths are all path arguments in the line
            paths = parts[1:]
            for path in paths:
                if path not in ALLOWED_GOVERNANCE_PATHS:
                    return False, f"Governance-only diff violation: changed/renamed path '{path}' outside allowed governance set"
    except Exception as e:
        return False, f"Failed Git ancestry/diff verification: {e}"

    # Step 6: STATE Governance Content Verification
    try:
        state_text = authority_state_file.read_text(encoding="utf-8")
        state_data = parse_json_strict(state_text)
    except Exception as e:
        return False, f"Failed to parse STATE.json from authority checkout: {e}"

    ext = state_data.get("extensions", {}).get("aos6_lari_controlled_pilot", {})
    if not isinstance(ext, dict):
        return False, "STATE.json missing aos6_lari_controlled_pilot extension dictionary"

    if ext.get("controlled_pilot_authorized") is not True:
        return False, "STATE controlled_pilot_authorized must be true"

    if ext.get("pilot_execution_authorized") is not True:
        return False, "STATE pilot_execution_authorized must be true"

    if ext.get("controlled_pilot_authority_class") not in ("AUTHORIZED_ISOLATED_ONLY", "AUTHORIZED_ISOLATED_SYNTHETIC_NONCANONICAL"):
        return False, "STATE controlled_pilot_authority_class invalid"

    if ext.get("controlled_pilot_authorized_aos_sha") != authorized_execution_aos_sha:
        return False, f"STATE controlled_pilot_authorized_aos_sha mismatch: observed {ext.get('controlled_pilot_authorized_aos_sha')}, expected {authorized_execution_aos_sha}"

    if ext.get("controlled_pilot_source_sha") != EXPECTED_LARI_SOURCE_SHA:
        return False, f"STATE controlled_pilot_source_sha mismatch: expected {EXPECTED_LARI_SOURCE_SHA}"

    bound_pre_count = ext.get("controlled_pilot_authorized_pre_execution_count")
    if not is_strict_int(bound_pre_count) or bound_pre_count < 0:
        return False, "STATE controlled_pilot_authorized_pre_execution_count must be a strict non-negative integer"

    curr_exec_count = ext.get("controlled_pilot_execution_count")
    if not is_strict_int(curr_exec_count) or curr_exec_count < 0:
        return False, "STATE controlled_pilot_execution_count must be a strict non-negative integer"

    if curr_exec_count != bound_pre_count:
        return False, f"STATE controlled_pilot_execution_count ({curr_exec_count}) != authorized pre-execution count ({bound_pre_count})"

    limit = ext.get("authorized_attempt_limit", ext.get("controlled_pilot_attempt_limit"))
    if not is_strict_int(limit) or limit != 1:
        return False, f"STATE attempt limit must be a strict integer equal to 1 (observed {limit})"

    retry_auth = ext.get("automatic_retry_authority", ext.get("controlled_pilot_retry_authority"))
    if retry_auth not in ("NONE", "NO", False, 0):
        return False, f"STATE automatic retry authority must be NONE (observed {retry_auth})"

    if ext.get("canonical_lari_mutation_authorized", ext.get("canonical_lari_mutation")) is not False:
        return False, "STATE canonical LARI mutation authority must be false"

    if ext.get("stage12c_authorized", ext.get("stage12c")) is not False:
        return False, "STATE stage12c authority must be false"

    if ext.get("production_authority", ext.get("production")) is not False:
        return False, "STATE production authority must be false"

    state_auth_id = ext.get("current_execution_authorization_id", ext.get("authority_id"))
    if state_auth_id != authority_id:
        return False, f"STATE authority_id mismatch: observed '{state_auth_id}', expected '{authority_id}'"

    # Step 7: EVIDENCE.jsonl Corroboration (Strict Fresh Authority Event Contract)
    try:
        evidence_lines = authority_evidence_file.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return False, f"Failed to read EVIDENCE.jsonl from authority checkout: {e}"

    matching_events = []
    for line_idx, line in enumerate(evidence_lines, 1):
        line_str = line.strip()
        if not line_str:
            continue
        try:
            ev_obj = parse_json_strict(line_str)
        except Exception as e:
            return False, f"Malformed JSONL on line {line_idx} of EVIDENCE.jsonl: {e}"

        ev_ext = ev_obj.get("extensions", {})
        # REQUIRE explicit extensions.authority_id (NO task_id fallback)
        if isinstance(ev_ext, dict) and ev_ext.get("authority_id") == authority_id:
            matching_events.append(ev_obj)

    if len(matching_events) == 0:
        return False, f"No EVIDENCE event found matching explicit extensions.authority_id '{authority_id}'"
    if len(matching_events) > 1:
        return False, f"Multiple ({len(matching_events)}) EVIDENCE events found matching explicit extensions.authority_id '{authority_id}'"

    ev = matching_events[0]
    ev_ext = ev.get("extensions", {})

    # REQUIRE explicit extensions.authorized_execution_aos_sha
    ev_exec_sha = ev_ext.get("authorized_execution_aos_sha")
    if ev_exec_sha != authorized_execution_aos_sha:
        return False, f"EVIDENCE explicit extensions.authorized_execution_aos_sha mismatch: observed '{ev_exec_sha}', expected '{authorized_execution_aos_sha}'"

    # Strict integer contract on EVIDENCE fields
    ev_pre_count = ev_ext.get("pre_execution_count")
    if not is_strict_int(ev_pre_count) or ev_pre_count < 0:
        return False, "EVIDENCE pre_execution_count must be a strict non-negative integer"
    if ev_pre_count != bound_pre_count:
        return False, f"EVIDENCE pre_execution_count ({ev_pre_count}) != STATE pre-execution count ({bound_pre_count})"

    ev_limit = ev_ext.get("authorized_attempt_limit")
    if not is_strict_int(ev_limit) or ev_limit != 1:
        return False, "EVIDENCE authorized_attempt_limit must be a strict integer equal to 1"

    ev_retry_count = ev_ext.get("automatic_retry_count")
    if not is_strict_int(ev_retry_count) or ev_retry_count != 0:
        return False, "EVIDENCE automatic_retry_count must be a strict integer equal to 0"

    if ev_ext.get("retry_authority") != "NONE":
        return False, f"EVIDENCE retry_authority must be NONE (observed '{ev_ext.get('retry_authority')}')"

    if ev_ext.get("controlled_pilot_authorized") is not True:
        return False, "EVIDENCE controlled_pilot_authorized must be true"

    if ev_ext.get("pilot_execution_authorized") is not True:
        return False, "EVIDENCE pilot_execution_authorized must be true"

    if ev_ext.get("canonical_lari_mutation_authorized") is not False:
        return False, "EVIDENCE canonical_lari_mutation_authorized must be false"

    if ev_ext.get("stage12c_authorized") is not False:
        return False, "EVIDENCE stage12c_authorized must be false"

    if ev_ext.get("production_authorized") is not False:
        return False, "EVIDENCE production_authorized must be false"

    ev_lari_sha = ev_ext.get("authorized_lari_source_sha")
    if ev_lari_sha != EXPECTED_LARI_SOURCE_SHA:
        return False, f"EVIDENCE authorized_lari_source_sha mismatch: observed '{ev_lari_sha}', expected '{EXPECTED_LARI_SOURCE_SHA}'"

    # Step 8: Executable Worktree Cleanliness & Integrity Proof
    try:
        exec_head_post = runner.run(["git", "rev-parse", "HEAD"], cwd=executable_repo_dir).stdout.strip()
        if exec_head_post != authorized_execution_aos_sha:
            return False, f"Executable HEAD changed during preflight: observed {exec_head_post}, expected {authorized_execution_aos_sha}"

        exec_status = runner.run(["git", "status", "--porcelain"], cwd=executable_repo_dir).stdout.strip()
        if exec_status:
            return False, f"Executable worktree is dirty after preflight verification: {exec_status}"
    except Exception as e:
        return False, f"Failed executable worktree final verification: {e}"

    return True, None
