HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
CORRECTION_AUTHORITY_ID=LARI-AOS6-TMPFS-RUNTIME-CORR-20260902-01
SUBJECT_REPOSITORY=MertSGI/AOS
SUBJECT_BRANCH=feature/aos-6-lari-controlled-pilot
SUBJECT_PARENT_SHA=0868dc6326ddc5b6cf1251cf0effd91e66ceffa9
SUBJECT_CANDIDATE_SHA=0eda7e5acc02b1fe95b542c3670490584912e165

# AOS6 Controlled Pilot Tmpfs Runtime Correction Executor Report

## Executive Summary
This document records the executor claim for the bounded tmpfs runtime correction executed under authority `LARI-AOS6-TMPFS-RUNTIME-CORR-20260902-01`.
The correction provisions a sealed 64 MiB `tmpfs` at `/tmp` (`rw,noexec,nosuid,size=67108864,mode=1777`) inside the target container host configuration to resolve the `ENOENT mkdir /tmp/tsx-0` runtime failure observed during workflow run 33635438024.

This candidate `0eda7e5acc02b1fe95b542c3670490584912e165` is a **NEW EXECUTABLE CANDIDATE** and does **NOT** possess controlled-pilot execution authority. No controlled pilot rerun or workflow dispatch was performed.

---

## Authority & Subject Identification
- **CORRECTION_AUTHORITY_ID**: `LARI-AOS6-TMPFS-RUNTIME-CORR-20260902-01`
- **SUBJECT_REPOSITORY**: `MertSGI/AOS`
- **SUBJECT_BRANCH**: `feature/aos-6-lari-controlled-pilot`
- **SUBJECT_PARENT_SHA**: `0868dc6326ddc5b6cf1251cf0effd91e66ceffa9`
- **TMPFS_CORRECTION_SHA**: `0eda7e5acc02b1fe95b542c3670490584912e165`
- **Subject Commit Message**: `fix(aos-6): provision sealed tmp runtime`
- **Subject Changed Files**:
  1. `scripts/aos6_controlled_pilot_harness.py`
  2. `pilot_contracts/test_aos6_controlled_pilot_contracts.py`
  3. `pilot_contracts/aos6_controlled_pilot_runtime_manifest.schema.json`

---

## Local Verification Evidence
- **Test Commands**:
  - `py -m pytest -v --tb=short pilot_contracts/test_aos6_controlled_pilot_contracts.py`
  - `py -m pytest -v --tb=short -m "not postgres_integration and not live_read_only"`
  - `py -m aos.validate state docs/project-control/STATE.json`
  - `py -m aos.validate evidence docs/project-control/EVIDENCE.jsonl`
  - `git diff --check`
- **Observed Test Counts / Results**:
  - Focused contracts test suite: **97 passed, 2 skipped** (120 total test functions including positive/negative matrices)
  - Full canonical pytest suite: **661 passed, 8 skipped, 26 deselected**
  - STATE validation: `PASS`
  - EVIDENCE validation: `PASS`
  - git diff check: `CLEAN`
- **Schema Validation Coverage**:
  - Validated runtime manifest with all 9 required tmpfs fields passing schema.
  - Early failure manifest with all 9 tmpfs fields set to `null` validated against schema.
  - Extra/unexpected properties rejected via `additionalProperties=false`.
- **tmpfs Positive / Negative Test Matrix Results**:
  - Positive exact `/tmp` tmpfs test (`rw,noexec,nosuid,mode=1777,size=67108864`): `PASS`
  - Fail-closed negative tests A-T (missing Tmpfs, empty Tmpfs, missing /tmp, wrong destination, duplicate tmpfs, missing/invalid rw, ro, missing/invalid noexec, exec, missing/invalid nosuid, suid, missing/wrong mode, missing/wrong/zero size, host /tmp bind mount present, malformed inspect shape): `ALL PASS`

---

## Container Creation & Docker Inspect Security Contract
- **Docker Create tmpfs Argument**:
  `--tmpfs /tmp:rw,noexec,nosuid,size=67108864,mode=1777`
- **Actual Docker Inspect Validator Contract**:
  - Evaluates `HostConfig.Tmpfs` mapping in Docker inspect output.
  - Enforces exact destination `/tmp` and exact 64 MiB byte count `67108864`.
  - Rejects `ro`, `exec`, `suid`, missing/duplicate/conflicting options.
  - Enforces `HOST_TMP_BIND_MOUNT_COUNT=0` across all `Mounts`.
- **Preserved Isolation Controls**:
  - `NetworkMode == "none"`
  - `ReadonlyRootfs == True`
  - `PidsLimit == 100`
  - `CapDrop` contains `"ALL"`
  - `SecurityOpt` contains `"no-new-privileges"`
  - Memory limit == `512m`, CPU limit == `1.0`
  - Authorized host bind mounts remains exactly 2 (`/workspace:ro`, `/aos-driver/aos6_controlled_pilot_driver.mjs:ro`).

---

## Governance & Integrity Scope Checks
- `STATE_CHANGED`: `NO`
- `EVIDENCE_CHANGED`: `NO`
- `WORKFLOW_CHANGED`: `NO`
- `DRIVER_CHANGED`: `NO`
- `AUTHORITY_PREFLIGHT_CODE_CHANGED`: `NO`
- `REQUEST_CONTRACT_CHANGED`: `NO`

---

## Ordinary CI Verification Results
- **Ordinary CI Run ID**: `33641200777`
- **Ordinary CI Job ID**: `100284417825` (`validate-and-test`)
- **Ordinary CI Head SHA**: `0eda7e5acc02b1fe95b542c3670490584912e165`
- **Ordinary CI Event**: `push`
- **Ordinary CI Terminal Conclusion**: `success`
- **Executed Stages**:
  - `offline/core`: `PASS` (24s)
  - `PostgreSQL integration`: `PASS` (3s)
  - `focused controlled-pilot contracts`: `PASS` (3s)
  - `full canonical pytest`: `PASS` (23s)
  - `STATE validation`: `PASS`
  - `EVIDENCE validation`: `PASS`

---

## Controlled Pilot Dispatch Accounting
- **Historical Controlled Pilot Dispatch Count**: `3` (Run IDs: `33595580362`, `33595714244`, `33635438024`)
- **CONTROLLED_PILOT_DISPATCH_COUNT_THIS_CORRECTION**: `0`
- **WORKFLOW_RERUN_COUNT**: `0`
- **LARI_ACCESS_COUNT**: `0`
- **LARI_MUTATION_COUNT**: `0`
- **SUPABASE_ACCESS_COUNT**: `0`
- **VERCEL_ACCESS_COUNT**: `0`
- **PROVIDER_ACCESS_COUNT**: `0`
- **CUSTOMER_DATA_ACCESS_COUNT**: `0`

---

## Authority & Next Steps Requirements
- **NEW_EXECUTABLE_AOS_SHA_REQUIRED**: `YES`
- **TMPFS_CORRECTION_SHA_IS_EXECUTION_AUTHORIZED**: `NO`
- **NEW_AUTHORITY_REQUIRED**: `YES`
- **CONTROLLER_REVIEW_REQUIRED**: `YES`
- **WORKFLOW_DISPATCH_AUTHORITY**: `NONE`
- **STAGE12C_AUTHORITY**: `NONE`
- **PRODUCTION_AUTHORITY**: `NONE`
- **BLOCKER_IF_ANY**: `NONE`
