HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
CORRECTION_AUTHORITY_ID=LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-R2-20260903-01
SUBJECT_BRANCH=feature/controller-relay-service-v1
SUBJECT_PARENT_SHA=d62e61a63817772ace4820faea14bff115fb97ef
SUBJECT_SHA=6776abb8f9cff7a1ac65a8f4723c5277ccf46190
TRUSTED_RELAY_BOOTSTRAP_SHA=039232ecf10948bf55a9d9dab665828b6c06f7c6

# EXECUTOR CLAIM EVIDENCE - RELAY SERVICE V1 FOUNDATION R2

## Topology & Execution Scope
* Authority ID: `LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-R2-20260903-01`
* Repository: `MertSGI/AOS`
* Branch: `feature/controller-relay-service-v1`
* Parent SHA: `d62e61a63817772ace4820faea14bff115fb97ef`
* Implementation SHA: `6776abb8f9cff7a1ac65a8f4723c5277ccf46190`
* Trusted Relay Bootstrap SHA: `039232ecf10948bf55a9d9dab665828b6c06f7c6`
* Authorized Files Changed (EXACTLY 5):
  1. `docs/controller-relay/SERVICE_DESIGN.md`
  2. `src/aos/controller_relay_service.py`
  3. `src/aos/controller_relay_git_transport.py`
  4. `tests/test_controller_relay_service.py`
  5. `tests/test_controller_relay_git_transport.py`
* Protocol Engine (`src/aos/controller_relay.py`) Changes: `0` (ZERO changes)

## Core Trust Hardening Implementations
1. **Global History Validator:** `_validate_existing_relay_history(expected_head)` implemented in `ControllerRelayService` as a single reusable fail-closed operation invoked prior to any object creation or ref update by both `publish_message()` and `publish_receipt()`.
2. **Provenance Map & Binding:** Map `message_id -> { message, publication_commit_sha, path, publication_ordinal }` constructed over single first-parent lineage. Receipts bound strictly to Git-derived message publication commit SHAs (`receipt.message_commit_sha == target.publication_commit_sha`).
3. **Actual Git Publication Order:** History lifecycle validation uses actual Git publication commit ordinals (`publication_ordinal`). Out-of-order published receipt events fail closed (`HOLD_INVALID_RELAY_HISTORY`).
4. **Temporal True Reply Integrity:** For `requires_reply=true` messages and `CONSUMED` receipts, a qualifying reply MUST have `reply.publication_ordinal < consumed_receipt.publication_ordinal`.
5. **Trusted Bootstrap & Reappearance Detection:** Ancestry walk strictly requires reaching `039232ecf10948bf55a9d9dab665828b6c06f7c6`. If unreachable (`HOLD_RELAY_BOOTSTRAP_UNREACHABLE`), non-linear (`HOLD_INVALID_RELAY_HISTORY`), reappeared (`HOLD_RELAY_RECORD_REAPPEARED`), mutated (`HOLD_RELAY_RECORD_MUTATED`), or present at bootstrap (`HOLD_RECORD_PROVENANCE_UNVERIFIABLE`), history fails closed with 0 mutations.
6. **Canonical GitHub Host Pinning:** Production API host strictly pinned to `https://api.github.com`. `StdlibGitHubRequester.__init__` validates `base_url` before `CredentialProvider.get_token()` (`CREDENTIAL_PROVIDER_GET_TOKEN_CALL_COUNT=0` on invalid host).

## Verification Results
* `tests/test_controller_relay.py`: PASS (47 passed)
* `tests/test_controller_relay_service.py`: PASS (25 passed)
* `tests/test_controller_relay_git_transport.py`: PASS (11 passed)
* Offline Pytest Suite (`-m "not postgres_integration and not live_read_only"`): PASS (714 passed)
* Full Canonical Pytest: PASS (714 passed)
* STATE Validation (`docs/project-control/STATE.json`): PASS
* EVIDENCE Validation (`docs/project-control/EVIDENCE.jsonl`): PASS

## Ordinary CI Evidence
* Workflow: `AOS CI`
* Event: `push`
* Run Attempt: `1`
* Run ID: `33753266313`
* Job ID: `100641523647`
* Status: `completed`
* Conclusion: `success`

## Absolute Zero Mutation & Freeze Verification
* `LIVE_RELAY_WRITE_COUNT=0`
* `LIVE_RELAY_MESSAGE_COUNT=0`
* `LIVE_RELAY_RECEIPT_COUNT=0`
* `CONTROL_RELAY_BRANCH_MUTATION_COUNT=0`
* `CREDENTIAL_CREATE_COUNT=0`
* `GITHUB_APP_INSTALL_COUNT=0`
* `LIVE_SERVICE_DEPLOYMENT_COUNT=0`
* `STATE_MUTATION_COUNT=0`
* `EVIDENCE_MUTATION_COUNT=0`
