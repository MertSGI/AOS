HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
AUTHORITY_ID=LARI-AOS6-POST-R1-CANONICAL-RECONCILIATION-20260902-01
SUBJECT_REPOSITORY=MertSGI/AOS
SUBJECT_BRANCH=feature/aos-6-lari-controlled-pilot
SUBJECT_PARENT_SHA=91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9
SUBJECT_RECONCILIATION_SHA=931f1bc877497987c1ef3571624b4fed05ea332c

# AOS6 Post-Tmpfs-R1 Canonical Governance Reconciliation Executor Report

## Executive Summary
This document records the executor claim for the post-R1 canonical governance reconciliation executed under authority `LARI-AOS6-POST-R1-CANONICAL-RECONCILIATION-20260902-01`.
The reconciliation updates `docs/project-control/STATE.json` and appends `AOS-EV-0085` and `AOS-EV-0086` to `docs/project-control/EVIDENCE.jsonl` to faithfully encode historical replacement pilot failure (run 33635438024), tmpfs R1 correction acceptance (candidate 91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9), and the resulting no-execution-authority state (`controlled_pilot_authorized = false`, `pilot_execution_authorized = false`, `next_action = WAIT_FOR_FRESH_LARI_CONTROLLER_AUTHORITY_BINDING`).

Reconciliation commit `931f1bc877497987c1ef3571624b4fed05ea332c` is a governance reconciliation commit only and does **NOT** grant controlled-pilot execution authority. Zero controlled-pilot dispatches or workflow reruns were performed.

---

## Authority & Subject Identification
- **AUTHORITY_ID**: `LARI-AOS6-POST-R1-CANONICAL-RECONCILIATION-20260902-01`
- **SUBJECT_REPOSITORY**: `MertSGI/AOS`
- **SUBJECT_BRANCH**: `feature/aos-6-lari-controlled-pilot`
- **RECONCILIATION_PARENT_SHA**: `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9`
- **AOS6_POST_R1_RECONCILIATION_SHA**: `931f1bc877497987c1ef3571624b4fed05ea332c`
- **Subject Commit Message**: `docs(aos-6): reconcile pilot failure and tmpfs R1`
- **Subject Changed Scope**: Exactly two files (`docs/project-control/STATE.json`, `docs/project-control/EVIDENCE.jsonl`).

---

## Appended Evidence Records
1. **AOS-EV-0085**:
   - `gate`: `AOS-6`
   - `task_id`: `AOS6-REPLACEMENT-CONTROLLED-PILOT-TERMINAL`
   - `type`: `AOS6_REPLACEMENT_CONTROLLED_PILOT_FAILURE_AND_CONTROLLER_DISPOSITION`
   - `result`: `FAIL`
   - `evidence_level`: `E3_ISOLATED_RUNTIME_PROVEN`
   - Bindings:
     - `authority_id`: `LARI-AOS6-REPLACEMENT-PILOT-20260902-01`
     - `authorized_execution_aos_sha`: `77e410747ff44fd09242a2158c4b2bb761a0e08e`
     - `authority_evidence_sha`: `18058ef91a12345bbe98ceb925fd8f3d990ee3ae`
     - `authorized_lari_source_sha`: `cc9c55e7fc841f4f16137b0a5e7c6f04b44b631a`
     - `workflow_run_id`: `33635438024`
     - `canonical_workflow_job_id`: `100265031211`
     - `p1_failure_class`: `AOS_CONTROLLED_PILOT_CONTAINER_RUNTIME_TMP_UNAVAILABLE`
     - `p1_failure_detail`: `TSX_CACHE_MKDIR_ENOENT_/tmp/tsx-0`
     - `lari_product_defect`: `NOT_PROVEN`
     - `aos_pilot_runtime_defect`: `PROVEN`
     - `known_dispatch_count`: `3`
     - `precheck_only_dispatch_deviation_count`: `1`
     - `substantive_controlled_pilot_attempt_count`: `2`

2. **AOS-EV-0086**:
   - `gate`: `AOS-6`
   - `task_id`: `AOS6-TMPFS-R1-FINAL-ACCEPTANCE`
   - `type`: `AOS6_TMPFS_R1_CORRECTION_ACCEPTANCE`
   - `result`: `PASS`
   - `evidence_level`: `E2_EXECUTABLE_EXACT_REVISION_PROVEN`
   - Bindings:
     - `accepted_execution_candidate_sha`: `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9`
     - `parent_sha`: `0eda7e5acc02b1fe95b542c3670490584912e165`
     - `commit_message`: `fix(aos-6): make tmpfs mount accounting type-aware`
     - `canonical_ci_run_id`: `33665957478`
     - `canonical_ci_job_id`: `100367637362`
     - `canonical_ci_result`: `SUCCESS`
     - `tmpfs_r1_final_accepted`: `YES`
     - `execution_authority_granted_by_this_evidence`: `NO`

3. **AOS-EV-0087 Absence**:
   - `AOS_EV_0087_PRESENT`: `NO` (Count: 0)

---

## Reconciled STATE.json Governance
- `status`: `TMPFS_R1_ACCEPTED_NEW_AUTHORITY_REQUIRED`
- `controlled_pilot_authorized`: `false`
- `pilot_execution_authorized`: `false`
- `current_execution_authority`: `NONE`
- `previous_replacement_authority`: `LARI-AOS6-REPLACEMENT-PILOT-20260902-01`
- `previous_replacement_authority_consumed`: `true`
- `accepted_new_executable_candidate_sha`: `91cfa2c58e7545bbe8bb8d5d535ef0fc2efe37c9`
- `known_dispatch_count`: `3`
- `precheck_only_dispatch_deviation_count`: `1`
- `substantive_controlled_pilot_attempt_count`: `2`
- `automatic_retry_count`: `0`
- `retry_authority`: `NONE`
- `new_controlled_pilot_execution_authority`: `NOT_GRANTED`
- `new_authority_required`: `true`
- `stage12c_authorized`: `false`
- `production_status`: `NO_GO`
- `next_action`: `* next action is WAIT_FOR_FRESH_LARI_CONTROLLER_AUTHORITY_BINDING`

---

## Local Verification Evidence
- `py -m aos.validate state docs/project-control/STATE.json`: `PASS`
- `py -m aos.validate evidence docs/project-control/EVIDENCE.jsonl`: `PASS`
- `py -3 -m pytest -v --tb=short pilot_contracts/test_aos6_controlled_pilot_contracts.py`: `122 passed, 2 skipped`
- `py -3 -m pytest -v`: `661 passed, 8 skipped, 26 deselected`
- `git diff --check`: `CLEAN`
- Historical evidence records preserved byte-for-byte.

---

## Ordinary CI Verification Results
- **Ordinary CI Run ID**: `33667500122`
- **Ordinary CI Job ID**: `100371420918` (`validate-and-test`)
- **Ordinary CI Head SHA**: `931f1bc877497987c1ef3571624b4fed05ea332c`
- **Ordinary CI Event**: `push`
- **Ordinary CI Result**: `success`

---

## Dispatch & Execution Accounting
- **CONTROLLED_PILOT_DISPATCH_COUNT_THIS_RECONCILIATION**: `0`
- **WORKFLOW_RERUN_COUNT**: `0`
- **LARI_ACCESS_COUNT**: `0`
- **LARI_MUTATION_COUNT**: `0`
- **STAGE12C_COUNT**: `0`
- **PRODUCTION_COUNT**: `0`

---

## Next Steps Requirements
- **NEW_AUTHORITY_REQUIRED**: `YES`
- **CONTROLLER_REVIEW_REQUIRED**: `YES`
