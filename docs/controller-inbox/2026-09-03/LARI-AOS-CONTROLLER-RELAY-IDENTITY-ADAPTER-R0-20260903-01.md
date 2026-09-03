HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
AUTHORITY_ID=LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-20260903-01
SUBJECT_BRANCH=feature/controller-relay-service-v1
SUBJECT_PARENT_SHA=6776abb8f9cff7a1ac65a8f4723c5277ccf46190
SUBJECT_SHA=bded3b6e716499f34cc6972ca8acf77f98a21a12
LIVE_RELAY_BRANCH_HEAD=039232ecf10948bf55a9d9dab665828b6c06f7c6

# EXECUTOR CLAIM EVIDENCE - RELAY IDENTITY ADAPTER R0

## Topology & Scope Verification
* Authority ID: `LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-20260903-01`
* Repository: `MertSGI/AOS`
* Branch: `feature/controller-relay-service-v1`
* Parent SHA: `6776abb8f9cff7a1ac65a8f4723c5277ccf46190`
* Implementation SHA: `bded3b6e716499f34cc6972ca8acf77f98a21a12`
* Authorized Files Changed (EXACTLY 5):
  1. `docs/controller-relay/SERVICE_DESIGN.md`
  2. `src/aos/controller_relay_identity.py`
  3. `scripts/controller_relay_cr1_once.py`
  4. `tests/test_controller_relay_identity.py`
  5. `tests/test_controller_relay_cr1_once.py`
* Protocol & Transport Core Changes: `0` (ZERO changes to frozen files)

## Core Identity Implementations & Boundaries
1. **Injected Installation Token Provider:** Implemented `InjectedInstallationTokenCredentialProvider` in `src/aos/controller_relay_identity.py` implementing `CredentialProvider`.
2. **Memory-Only & Redaction:** Credentials strictly process-memory-only with `repr` / `str` redaction (`token='***REDACTED***'`). Zero token values in exceptions.
3. **120-Second Expiry Safety Boundary:** `get_token()` strictly requires `>= 120.0` seconds remaining lifetime (`HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY`).
4. **Zero Fallback/Discovery:** Zero `os.environ`, file, PAT, OAuth, keychain, or sub-process credential discovery.
5. **Fixed Role-to-Principal Mapping:** `scripts/controller_relay_cr1_once.py` maps `AOS_ROOT` -> `AOS_CONTROLLER` and `LARI_REPLY` -> `LARI_CONTROLLER`. Rejects all arbitrary input.
6. **Canonical Message Hashing:** Fresh canonical SHA-256 computation via `compute_message_content_sha256()` from `aos.controller_relay`. Zero hardcoded legacy hashes.
7. **R0 Dry-Run Execution:** `execute_cr1_dry_run()` operates strictly in `DRY_RUN` mode (`CREDENTIAL_ACCESS_COUNT=0`, `TRANSPORT_MUTATION_COUNT=0`, `LIVE_RELAY_WRITE_COUNT=0`).

## Verification & CI Evidence
* Identity Unit Tests (`tests/test_controller_relay_identity.py`): PASS (14 passed)
* CR-1 One-Shot Tests (`tests/test_controller_relay_cr1_once.py`): PASS (5 passed)
* Core Protocol Tests (`tests/test_controller_relay.py`): PASS (47 passed)
* Service Core Tests (`tests/test_controller_relay_service.py`): PASS (25 passed)
* Transport Tests (`tests/test_controller_relay_git_transport.py`): PASS (11 passed)
* Offline Pytest Suite (`-m "not postgres_integration and not live_read_only"`): PASS (733 passed)
* Full Canonical Pytest: PASS (733 passed)
* STATE Validation (`docs/project-control/STATE.json`): PASS
* EVIDENCE Validation (`docs/project-control/EVIDENCE.jsonl`): PASS
* Ordinary CI Workflow: `AOS CI`
  * Event: `push`
  * Run Attempt: `1`
  * Run ID: `33758611322`
  * Job ID: `100659048493`
  * Status: `completed`
  * Conclusion: `success`

## Absolute Zero Mutation & Freeze Verification
* `LIVE_RELAY_WRITE_COUNT=0`
* `LIVE_RELAY_MESSAGE_COUNT=0`
* `LIVE_RELAY_RECEIPT_COUNT=0`
* `CONTROL_RELAY_BRANCH_MUTATION_COUNT=0`
* `GITHUB_APP_CREATE_COUNT=0`
* `GITHUB_APP_INSTALL_COUNT=0`
* `PRIVATE_KEY_CREATE_COUNT=0`
* `JWT_CREATE_COUNT=0`
* `INSTALLATION_TOKEN_CREATE_COUNT=0`
* `CREDENTIAL_STORAGE_COUNT=0`
* `CREDENTIAL_ACCESS_COUNT_DURING_DRY_RUN=0`
* `LIVE_SERVICE_DEPLOYMENT_COUNT=0`
