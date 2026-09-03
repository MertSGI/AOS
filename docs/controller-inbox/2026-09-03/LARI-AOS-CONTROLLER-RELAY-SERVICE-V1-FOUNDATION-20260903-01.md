HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
IMPLEMENTATION_AUTHORITY_ID=LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01
SUBJECT_REPOSITORY=MertSGI/AOS
SUBJECT_BRANCH=feature/controller-relay-service-v1
SUBJECT_PARENT_SHA=039232ecf10948bf55a9d9dab665828b6c06f7c6
SUBJECT_SHA=56d1e155908f4bb864a39ad22fc818756e9d5d3a

# AOS Controller Relay Service V1 Foundation — Executor Claim

## Topology & Implementation Scope
* **Implementation Branch:** `feature/controller-relay-service-v1`
* **Commit SHA:** `56d1e155908f4bb864a39ad22fc818756e9d5d3a`
* **Parent SHA:** `039232ecf10948bf55a9d9dab665828b6c06f7c6`
* **Authorized File Count:** 5 files modified/created (no sixth file):
  1. `docs/controller-relay/SERVICE_DESIGN.md`
  2. `src/aos/controller_relay_service.py`
  3. `src/aos/controller_relay_git_transport.py`
  4. `tests/test_controller_relay_service.py`
  5. `tests/test_controller_relay_git_transport.py`

## Architecture & Verification Proof
1. **Network-Neutral Core:** Implemented `ControllerRelayService` reusing production protocol validators from `aos.controller_relay`.
2. **Principal Authentication Boundary:** Implemented `ControllerPrincipal` abstraction enforcing identity matching prior to any transport invocation. Mismatches reject fail-closed (`TRANSPORT_CALL_COUNT=0`).
3. **True Git Data CAS Transport:** Implemented `GitDataCASRelayTransport` with 11-step REST Git Data API pipeline (`blobs`, `trees`, `commits`, `refs`). Ref patch uses `force=False` and handles races fail-closed with `HOLD_CAS_RACE`.
4. **One-Record-One-Commit:** Publication creates exactly one blob, one tree, and one commit with parent `expected_head`. No batching, no overwrites, no deletes.
5. **Credential & Requester Abstraction:** Injected `CredentialProvider` and stdlib `GitHubRequester` with endpoint allowlist restricted strictly to `MertSGI/AOS` and `refs/heads/control/controller-relay`.
6. **Token Safety:** Credentials strictly suppressed from reprs, logs, error strings, and payloads. Remote GitHub errors sanitized.
7. **Offline Determinism:** Full unit test suite runs with zero network calls (`NETWORK_CALL_COUNT=0`).

## Local & Canonical CI Results
* **Relay Core Tests (`test_controller_relay.py`):** 47 PASSED
* **Relay Service Core Tests (`test_controller_relay_service.py`):** 12 PASSED
* **Relay Transport Tests (`test_controller_relay_git_transport.py`):** 8 PASSED
* **Full Offline Pytest Suite:** 698 PASSED, 8 SKIPPED
* **Full Pytest Suite:** 716 PASSED, 8 SKIPPED
* **State Validation (`STATE.json`):** PASS
* **Evidence Validation (`EVIDENCE.jsonl`):** PASS
* **Canonical CI (Push Event, Attempt 1):**
  * **Run ID:** `33745490592`
  * **Job ID:** `100616869065` (`validate-and-test`)
  * **Result:** `SUCCESS`

## Live Relay Freeze & Invariants
* **Frozen Live Relay Branch Head (`control/controller-relay`):** `039232ecf10948bf55a9d9dab665828b6c06f7c6`
* **`LIVE_RELAY_WRITE_COUNT`:** 0
* **`CREDENTIAL_CREATE_COUNT`:** 0
* **`GITHUB_APP_INSTALL_COUNT`:** 0
* **`LIVE_SERVICE_DEPLOYMENT_COUNT`:** 0
