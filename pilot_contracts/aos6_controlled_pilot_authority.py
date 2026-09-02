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

    # Step 2: Executable HEAD check
    if runner is None:
        import subprocess
        class DefaultRunner:
            def run(self, cmd, cwd=None, env=None, check=True):
                res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=check)
                return res
        runner = DefaultRunner()

    try:
        exec_head = runner.run(["git", "rev-parse", "HEAD"], cwd=executable_repo_dir).stdout.strip()
        if exec_head != authorized_execution_aos_sha:
            return False, f"Executable HEAD mismatch: observed {exec_head}, expected {authorized_execution_aos_sha}"
    except Exception as e:
        return False, f"Failed to verify executable HEAD: {e}"

    # Step 3: Authority Evidence Checkout Revision Check
    authority_state_file = authority_evidence_dir / "docs" / "project-control" / "STATE.json"
    authority_evidence_file = authority_evidence_dir / "docs" / "project-control" / "EVIDENCE.jsonl"

    if not authority_state_file.exists() or not authority_evidence_file.exists():
        return False, "Authority evidence checkout missing STATE.json or EVIDENCE.jsonl"

    # Git Ancestry and Diff Verification (run inside executable repo directory)
    try:
        # Check if authority_evidence_sha is a valid commit object
        cat_check = runner.run(["git", "cat-file", "-t", authority_evidence_sha], cwd=executable_repo_dir, check=False)
        if cat_check.returncode != 0:
            return False, f"Authority evidence SHA {authority_evidence_sha} not found in Git database"

        # Check ancestry: merge-base between exec SHA and authority SHA must be exec SHA
        merge_base = runner.run(["git", "merge-base", authorized_execution_aos_sha, authority_evidence_sha], cwd=executable_repo_dir).stdout.strip()
        if merge_base != authorized_execution_aos_sha:
            return False, f"Ancestry failure: {authority_evidence_sha} is not a descendant of executable SHA {authorized_execution_aos_sha}"

        # Diff check: git diff --name-only exec_sha..authority_sha
        diff_res = runner.run(["git", "diff", "--name-only", f"{authorized_execution_aos_sha}..{authority_evidence_sha}"], cwd=executable_repo_dir).stdout.strip()
        changed_paths = [p.strip() for p in diff_res.splitlines() if p.strip()]

        for path in changed_paths:
            if path not in ALLOWED_GOVERNANCE_PATHS:
                return False, f"Governance-only diff violation: changed path '{path}' outside allowed governance set"
    except Exception as e:
        return False, f"Failed Git ancestry/diff verification: {e}"

    # Step 4: Governance Content Verification from Authority Checkout (DATA ONLY)
    try:
        state_text = authority_state_file.read_text(encoding="utf-8")
        state_data = parse_json_strict(state_text)
    except Exception as e:
        return False, f"Failed to parse STATE.json from authority checkout: {e}"

    ext = state_data.get("extensions", {}).get("aos6_lari_controlled_pilot", {})
    if not isinstance(ext, dict):
        return False, "STATE.json missing aos6_lari_controlled_pilot extension dictionary"

    # Validate STATE authority fields
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
    if not isinstance(bound_pre_count, int) or bound_pre_count < 0:
        return False, "STATE controlled_pilot_authorized_pre_execution_count must be a non-negative integer"

    curr_exec_count = ext.get("controlled_pilot_execution_count")
    if curr_exec_count != bound_pre_count:
        return False, f"STATE controlled_pilot_execution_count ({curr_exec_count}) != authorized pre-execution count ({bound_pre_count})"

    limit = ext.get("authorized_attempt_limit", ext.get("controlled_pilot_attempt_limit"))
    if limit != 1:
        return False, f"STATE attempt limit must be exactly 1 (observed {limit})"

    retry_auth = ext.get("automatic_retry_authority", ext.get("controlled_pilot_retry_authority"))
    if retry_auth not in ("NO", "NONE", False, 0):
        return False, f"STATE automatic retry authority must be NO/NONE (observed {retry_auth})"

    if ext.get("canonical_lari_mutation_authorized", ext.get("canonical_lari_mutation")) is not False:
        return False, "STATE canonical LARI mutation authority must be false"

    if ext.get("stage12c_authorized", ext.get("stage12c")) is not False:
        return False, "STATE stage12c authority must be false"

    if ext.get("production_authority", ext.get("production")) is not False:
        return False, "STATE production authority must be false"

    state_auth_id = ext.get("current_execution_authorization_id", ext.get("authority_id"))
    if state_auth_id != authority_id:
        return False, f"STATE authority_id mismatch: observed '{state_auth_id}', expected '{authority_id}'"

    # Step 5: EVIDENCE.jsonl Corroboration
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
        ev_auth_id = ev_ext.get("authority_id") or ev_obj.get("task_id")
        if ev_auth_id == authority_id:
            matching_events.append(ev_obj)

    if len(matching_events) == 0:
        return False, f"No EVIDENCE event found matching authority_id '{authority_id}'"
    if len(matching_events) > 1:
        return False, f"Multiple ({len(matching_events)}) EVIDENCE events found matching authority_id '{authority_id}'"

    ev = matching_events[0]
    ev_ext = ev.get("extensions", {})

    ev_exec_sha = ev_ext.get("authorized_execution_aos_sha") or ev_ext.get("controller_reported_aos_sha") or ev.get("revisions", {}).get("commit_sha")
    if ev_exec_sha != authorized_execution_aos_sha:
        return False, f"EVIDENCE authorized_execution_aos_sha mismatch: observed '{ev_exec_sha}', expected '{authorized_execution_aos_sha}'"

    ev_pre_count = ev_ext.get("pre_execution_count", ev_ext.get("controlled_pilot_authorized_pre_execution_count"))
    if ev_pre_count is not None and ev_pre_count != bound_pre_count:
        return False, f"EVIDENCE pre_execution_count ({ev_pre_count}) != STATE pre-execution count ({bound_pre_count})"

    ev_lari_sha = ev_ext.get("controller_reported_lari_candidate_sha") or ev_ext.get("authorized_lari_source_sha") or ev_ext.get("lari_candidate_sha")
    if ev_lari_sha != EXPECTED_LARI_SOURCE_SHA:
        return False, f"EVIDENCE LARI source SHA mismatch: observed '{ev_lari_sha}', expected '{EXPECTED_LARI_SOURCE_SHA}'"

    return True, None
