HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
FOUNDATION_R1_AUTHORITY_ID=LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-R1-20260903-01
SUBJECT_REPOSITORY=MertSGI/AOS
SUBJECT_BRANCH=feature/controller-relay-service-v1
SUBJECT_PARENT_SHA=56d1e155908f4bb864a39ad22fc818756e9d5d3a
SUBJECT_SHA=d62e61a63817772ace4820faea14bff115fb97ef

# AOS Controller Relay Service V1 Foundation R1 — Executor Claim

## Topology & Implementation Scope
* **Implementation Branch:** `feature/controller-relay-service-v1`
* **Subject SHA:** `d62e61a63817772ace4820faea14bff115fb97ef`
* **Parent SHA:** `56d1e155908f4bb864a39ad22fc818756e9d5d3a`
* **Authorized Scope:** Exactly 5 existing files modified (zero diff in `src/aos/controller_relay.py`):
  1. `docs/controller-relay/SERVICE_DESIGN.md`
  2. `src/aos/controller_relay_service.py`
  3. `src/aos/controller_relay_git_transport.py`
  4. `tests/test_controller_relay_service.py`
  5. `tests/test_controller_relay_git_transport.py`

## Foundation R1 Hardening Proof Matrix
1. **Production Recursive Tree Allowlist:** Stdlib endpoint allowlist permits strictly `/trees/<sha>` and `/trees/<sha>?recursive=1`. Arbitrary queries rejected (`PRODUCTION_RECURSIVE_TREE_ALLOWLIST=PASS`).
2. **Truncated Tree Fail-Closed:** Recursive tree response with `truncated: true` aborts fail closed without mutations (`TRUNCATED_TREE_FAIL_CLOSED=PASS`).
3. **Invalid History Fail-Closed:** Every historical record under messages and receipts must pass raw validation. Any malformed JSON, BOM, duplicate keys, or schema errors fail closed (`INVALID_HISTORY_FAIL_CLOSED=PASS`).
4. **History Provenance & Publication Commit:** Publication commit SHA derived by walking first-parent Git ancestry (`RelayRecordProvenance`). Never inferred from HEAD or receipt claims (`MESSAGE_PUBLICATION_PROVENANCE=PASS`).
5. **Receipt Commit Binding:** `receipt.message_commit_sha` bound strictly to derived message publication commit SHA (`RECEIPT_MESSAGE_COMMIT_BINDING=PASS`).
6. **True Reply Guard:** `requires_reply=true` CONSUMED receipt requires a real valid inverted outbound reply record (`REQUIRES_REPLY_TRUE_REPLY_GUARD=PASS`).
7. **Transport Exception Propagation:** Read errors propagate fail-closed with zero mutations.

## Local Test & Validation Summary
* **Relay Core Tests (`test_controller_relay.py`):** 47 PASSED
* **Relay Service Tests (`test_controller_relay_service.py`):** 15 PASSED
* **Relay Transport Tests (`test_controller_relay_git_transport.py`):** 11 PASSED
* **Offline Non-Postgres Pytest Suite:** 704 PASSED, 8 SKIPPED
* **Full Canonical Pytest Suite:** 704 PASSED, 8 SKIPPED
* **STATE Validation:** PASS (`docs/project-control/STATE.json`)
* **EVIDENCE Validation:** PASS (`docs/project-control/EVIDENCE.jsonl`)

## Live Relay Freeze & Non-Execution Invariants
* **Frozen Live Relay Branch (`control/controller-relay`):** `039232ecf10948bf55a9d9dab665828b6c06f7c6`
* **`LIVE_RELAY_WRITE_COUNT`:** 0
* **`CREDENTIAL_CREATE_COUNT`:** 0
* **`GITHUB_APP_INSTALL_COUNT`:** 0
* **`DEPLOYMENT_COUNT`:** 0
