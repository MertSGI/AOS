HANDOFF_PROTOCOL_VERSION=1
HANDOFF_KIND=EXECUTOR_CLAIM_ONLY
CONTROLLER_ACCEPTANCE_IMPLIED=NO
AUTHORITY_ID=LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-R1-20260903-01
SUBJECT_BRANCH=feature/controller-relay-service-v1
SUBJECT_PARENT_SHA=bded3b6e716499f34cc6972ca8acf77f98a21a12
SUBJECT_SHA=fa3a0d39333fddeb72a7a23d0aec8f63eeda54ab
LIVE_RELAY_BRANCH_HEAD=039232ecf10948bf55a9d9dab665828b6c06f7c6

# EXECUTOR CLAIM PROOF

## 1. Scope & Core Identity Adapter Correction
- Scope strictly bound to exact 2 files:
  - `scripts/controller_relay_cr1_once.py`
  - `tests/test_controller_relay_cr1_once.py`
- Schema version fixed to exact `"0.1"`.
- Subject repository fixed to exact `"MertSGI/AOS"`.
- Subject branch fixed to exact `"feature/controller-relay-v1"`.
- Subject SHA fixed to exact `"039232ecf10948bf55a9d9dab665828b6c06f7c6"`.
- Expected parent SHA fixed to exact `"039232ecf10948bf55a9d9dab665828b6c06f7c6"`.
- Authority refs exact ordered list:
  1. `LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01`
  2. `039232ecf10948bf55a9d9dab665828b6c06f7c6`
  3. `c3ee2f2c1510abdddd3de14bc879e5ba27dac835`
- Root requested next action fixed to exact `"LARI_CONTROLLER_VERIFY_AND_REPLY"`.
- Reply requested next action fixed to exact `"AOS_CONTROLLER_VERIFY_REPLY_AND_CLOSE_HANDSHAKE"`.

## 2. Hardened Observed Root Validation & Guards
- `build_cr1_reply_message_plan` enforces strict observed-root contract validation prior to plan generation.
- Rejection verified for all negative cases with `HOLD_INVALID_OBSERVED_ROOT`:
  - `schema_version = "0.1.0"` (raw valid schema alternative rejected)
  - `subject_branch = "feature/controller-relay-service-v1"`
  - Wrong or reordered `authority_refs`
  - Differing `requested_next_action`
  - Differing `subject_repository` or `subject_sha`

## 3. Verification & CI Proof
- Local tests (`test_controller_relay.py`, `test_controller_relay_service.py`, `test_controller_relay_git_transport.py`, `test_controller_relay_identity.py`, `test_controller_relay_cr1_once.py`, full suite) passed 100%.
- State and Evidence validation passed.
- Naturally triggered GitHub Actions CI:
  - Workflow Run ID: `33761423143`
  - Job ID: `100668433141`
  - Result: `success`
  - Status: `completed` (Attempt 1)
- Live Relay Branch Head preserved at `039232ecf10948bf55a9d9dab665828b6c06f7c6`.
- Zero credentials, zero network mutation, zero live relay write counts.
