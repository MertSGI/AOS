HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
CORRECTION_AUTHORITY_ID=LARI-AOS6-TMPFS-R1-TYPE-AWARE-MOUNTS-CORR-20260902-01
SUBJECT_REPOSITORY=MertSGI/AOS
SUBJECT_BRANCH=feature/aos-6-lari-controlled-pilot
SUBJECT_PARENT_SHA=0eda7e5acc02b1fe95b542c3670490584912e165
SUBJECT_CANDIDATE_SHA=91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9

# AOS6 Controlled Pilot Tmpfs R1 Type-Aware Mount Accounting Correction Executor Report

## Executive Summary
This document records the executor claim for the authorized type-aware mount accounting correction executed under authority `LARI-AOS6-TMPFS-R1-TYPE-AWARE-MOUNTS-CORR-20260902-01`.
The correction updates `validate_docker_inspect_data()` in `scripts/aos6_controlled_pilot_harness.py` to derive an explicit `bind_mounts = [m for m in mounts if m.get("Type") == "bind"]` array and perform all host bind accounting, workspace bind checks, driver bind checks, `/tmp` host bind checks, and unexpected host bind checks strictly on `bind_mounts`.

Candidate `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9` is a **NEW EXECUTABLE CANDIDATE** and does **NOT** possess controlled-pilot execution authority. No controlled pilot rerun or workflow dispatch was performed.

---

## Authority & Subject Identification
- **CORRECTION_AUTHORITY_ID**: `LARI-AOS6-TMPFS-R1-TYPE-AWARE-MOUNTS-CORR-20260902-01`
- **SUBJECT_REPOSITORY**: `MertSGI/AOS`
- **SUBJECT_BRANCH**: `feature/aos-6-lari-controlled-pilot`
- **SUBJECT_PARENT_SHA**: `0eda7e5acc02b1fe95b542c3670490584912e165`
- **TMPFS_R1_CORRECTION_SHA**: `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9`
- **Subject Commit Message**: `fix(aos-6): make tmpfs mount accounting type-aware`
- **Subject Changed Files**:
  1. `scripts/aos6_controlled_pilot_harness.py`
  2. `pilot_contracts/test_aos6_controlled_pilot_contracts.py`

---

## Technical Implementation Details
- Derived `bind_mounts = [m for m in mounts if m.get("Type") == "bind"]`.
- Checked `workspace` bind candidates: `Type == "bind"` AND `Destination == "/workspace"`.
- Checked `driver` bind candidates: `Type == "bind"` AND `Destination == "/aos-driver/aos6_controlled_pilot_driver.mjs"`.
- Enforced `workspace bind count == 1`, `driver bind count == 1`, `total bind count == 2`.
- `host_tmp_bind_mount_count` counts ONLY `Type == "bind"` AND `normalized Destination == "/tmp"` (legitimate `Type == "tmpfs"` at `/tmp` does NOT increment count).
- `unexpected_host_bind_mount_count` counts ONLY `Type == "bind"` mounts whose destination is not `/workspace` or `/aos-driver/aos6_controlled_pilot_driver.mjs`.
- Preserved all security controls, HostConfig.Tmpfs validation semantics, read-only rootfs, cap-drop ALL, no-new-privileges, network=none, memory/cpu limits, and runtime manifest schema.

---

## Local Verification Evidence
- **Test Commands**:
  - `py -3 -m pytest -v --tb=short pilot_contracts/test_aos6_controlled_pilot_contracts.py`
  - `py -3 -m pytest -v`
  - `py -m aos.validate state docs/project-control/STATE.json`
  - `py -m aos.validate evidence docs/project-control/EVIDENCE.jsonl`
  - `git diff --check`
- **Observed Test Results**:
  - Focused contracts suite: **122 passed, 2 skipped**
  - Full canonical pytest suite: **661 passed, 8 skipped, 26 deselected**
  - STATE validation: `PASS`
  - EVIDENCE validation: `PASS`
  - git diff check: `CLEAN`
- **Positive Test Coverage**:
  - `test_docker_inspect_valid_with_tmpfs_in_mounts` verifies a realistic Docker inspect shape containing 2 authorized `Type=bind` mounts and 1 `Type=tmpfs` `/tmp` mount passes with:
    - `host_tmp_bind_mount_count == 0`
    - `unexpected_host_bind_mount_count == 0`
    - `tmpfs_mount_count == 1`
    - `tmpfs_tmp_present is True`
    - `tmpfs_tmp_read_write is True`
    - `tmpfs_tmp_noexec is True`
    - `tmpfs_tmp_nosuid is True`
    - `tmpfs_tmp_mode_1777 is True`
    - `tmpfs_tmp_size_bytes == 67108864`
- **Negative Test Coverage**:
  - `test_tmpfs_negative_R_host_tmp_bind_present` and `test_tmpfs_negative_S_host_tmp_bind_present_alongside_valid_tmpfs` verify fail-closed behavior for `Type="bind"` at `/tmp`.
  - `test_docker_inspect_negative_unexpected_third_bind_fails_closed` verifies an unexpected third bind `Type="bind", Destination="/extra"` fails closed based on bind semantics (`Total bind mount count must be exactly 2`).
- **Full Harness Mock Compatibility**:
  - Updated `FakeCommandRunner` inspect mock to return realistic Mounts array (`2 Type=bind + 1 Type=tmpfs /tmp`).
  - Full harness execution test `test_harness_full_execution_mocked_success` passed without weakening any existing assertions.

---

## Governance & Integrity Scope Checks
- `EXACT_AUTHORIZED_TWO_FILES_MODIFIED`: `YES`
- `STATE_CHANGED`: `NO`
- `EVIDENCE_CHANGED`: `NO`
- `WORKFLOW_CHANGED`: `NO`
- `DRIVER_CHANGED`: `NO`
- `AUTHORITY_PREFLIGHT_CODE_CHANGED`: `NO`
- `REQUEST_CONTRACT_CHANGED`: `NO`
- `MANIFEST_SCHEMA_CHANGED`: `NO`

---

## Ordinary CI Verification Results
- **Ordinary CI Run ID**: `33646549210`
- **Ordinary CI Job ID**: `100301188390` (`validate-and-test`)
- **Ordinary CI Head SHA**: `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9`
- **Ordinary CI Event**: `push`
- **Ordinary CI Terminal Conclusion**: `success`
- **Observed Test Counts**:
  - `offline/core count`: 24s
  - `PostgreSQL count`: 3s
  - `focused controlled-pilot contract count`: 122 passed, 2 skipped
  - `full canonical pytest count`: 661 passed, 8 skipped, 26 deselected
  - `STATE validation`: PASS
  - `EVIDENCE validation`: PASS

---

## Controlled Pilot Dispatch Accounting
- **CONTROLLED_PILOT_DISPATCH_COUNT_THIS_CORRECTION**: `0`
- **WORKFLOW_RERUN_COUNT**: `0`
- **RETRY_AUTHORITY**: `NONE`
- **LARI_MUTATION_COUNT**: `0`
- **STAGE12C_COUNT**: `0`
- **PRODUCTION_COUNT**: `0`

---

## Residual Risks & Next Steps
- **NEW_EXECUTABLE_AOS_SHA_REQUIRED**: `YES`
- **TMPFS_R1_CORRECTION_SHA_IS_EXECUTION_AUTHORIZED**: `NO`
- **NEW_AUTHORITY_REQUIRED**: `YES`
- **CONTROLLER_REVIEW_REQUIRED**: `YES`
