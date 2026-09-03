# AOS Controller Relay Service V1 Architecture & Design Specification

**Authority ID:** `LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01`
**Repository:** `MertSGI/AOS`
**Live Relay Branch:** `control/controller-relay`
**Protocol Version:** `CONTROLLER_RELAY_V1` / `CONTROLLER_RELAY_RECEIPT_V1`

---

## 1. Executive Summary & Phase Status

This document defines the normative architecture and implementation boundaries for the AOS-owned **Controller Relay Service V1 Foundation**.

### Phase Constraints & Non-Execution Invariants
This foundation phase implements strictly:
* Network-neutral `ControllerRelayService` core
* True Git Data compare-and-swap (CAS) Relay transport engine (`GitDataCASRelayTransport`)
* Injected `ControllerPrincipal` authentication boundary
* Injected credential & HTTP requester abstractions (`CredentialProvider`, `GitHubRequester`)
* Comprehensive offline test suites proving zero network calls and token safety

**Explicit Phase Non-Scope:**
* NO live service deployed
* NO credentials provisioned
* NO GitHub App installed
* NO live write to `control/controller-relay` branch (`LIVE_RELAY_WRITE_COUNT=0`)
* NO HTTP or MCP server runtime started
* NO mutation of Layer-3 governance or LARI files

---

## 2. System Architecture & Layering

The Controller Relay System operates across five distinct layers:

```
+-----------------------------------------------------------------------+
| 1. Controller Principal (AOS_CONTROLLER, LARI_CONTROLLER, etc.)       |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 2. Interface Adapters (Future MCP Server / HTTP API Adapters)          |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 3. ControllerRelayService (Network-Neutral Core & Validator Hook)      |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 4. GitDataCASRelayTransport (True Git Data API CAS Transport Engine)   |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 5. Immutable Git Storage (`control/controller-relay` branch)          |
+-----------------------------------------------------------------------+
```

### Layer Separation Rules
1. **Existing Engine Integration:** `aos.controller_relay` remains the canonical, deterministic protocol engine and receipt state machine. `ControllerRelayService` delegates all payload, sequence, thread, receipt lifecycle, and conflict validations directly to `aos.controller_relay`.
2. **Governance Invariant:** Canonical Layer-3 governance remains the single authority source. The Relay Service is a non-authoritative transport.
   * `RELAY_MESSAGE != AUTHORITY`
   * `RELAY_RECEIPT != AUTHORITY`
   * `RELAY_CONSUMED != AUTHORITY_CONSUMED`
   * `authority_effect` MUST equal `"NONE"` for all V1 messages. `authority_refs` remain opaque strings.

---

## 3. Trust Boundaries & Authentication Model

### ControllerPrincipal
* `ControllerPrincipal` represents an authenticated identity established **OUTSIDE** the Relay payload (e.g. via TLS client certificates, authenticated MCP session context, or signed OAuth token assertions).
* Relay payload fields (`message.from` or `receipt.actor`) are self-declared assertions within the JSON string and **DO NOT** constitute authentication.

### Authentication Enforcement Rule
Before any transport operation:
* For `publish_message`: `principal.controller_id` MUST equal `message.from`.
* For `publish_receipt`: `principal.controller_id` MUST equal `receipt.actor`.

Any mismatch triggers an immediate fail-closed rejection prior to any Git transport call (`TRANSPORT_CALL_COUNT=0`, `GIT_OBJECT_CREATE_COUNT=0`).

---

## 4. Credential Model & Minimum Permission Scope

### Permanent Target Identity
* Architecture target: **Dedicated GitHub App Installation Identity** scoped exclusively to `MertSGI/AOS`.

### Anti-Patterns Explicitly Rejected
* NO Personal Access Tokens (PATs).
* NO User OAuth tokens or workstation developer credentials.
* NO broad organization-level permissions.

### Minimum Required Permissions
* Repository Git contents (`read` / `write` for `control/controller-relay` ref only).
* NO Issues, NO Actions, NO Deployments, NO Administration permissions.

### Injected Credential Isolation
`CredentialProvider` is an abstract interface injected into the transport. No implicit environment fallbacks (e.g. `GITHUB_TOKEN` environment variable reads) are permitted in the service core.

---

## 5. Token Safety & Secret Protection

### Token Sanitization
* Authorization tokens appear **ONLY** in in-memory HTTP headers during request dispatch.
* Tokens are strictly suppressed from:
  * `repr()` and `str()` outputs of all objects, requests, and credentials.
  * Exception strings and stack trace messages.
  * Logging calls and telemetry artifacts.
  * Relay message or receipt payloads.
  * HTTP error response bodies propagated to callers.

### Pre-Transport Secret Defense
All message and receipt payloads undergo recursive secret scanning via `scan_for_prohibited_secrets()` before transport invocation. Any payload containing key words like `password`, `api_key`, `access_token`, `private_key` or high-confidence regex patterns (PEM private keys, GitHub tokens, Bearer tokens) is rejected fail-closed.

---

## 6. Internal Path Derivation Rules

Callers cannot specify, override, or manipulate storage paths within the Relay repository. Paths are internally derived by `ControllerRelayService`:

### Message Storage Path
`controller-relay/v1/messages/<FROM>--<TO>/<SEQUENCE_12>-<MESSAGE_ID>.json`

* Example: `controller-relay/v1/messages/LARI_CONTROLLER--AOS_CONTROLLER/000000000001-CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001.json`

### Receipt Storage Path
`controller-relay/v1/receipts/<MESSAGE_ID>/<ACTOR>-<EVENT>.json`

* Example: `controller-relay/v1/receipts/CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001/AOS_CONTROLLER-OBSERVED.json`

### Path Safety Checks
Rejection of path traversal (`..`), malformed components, unsupported record types, or arbitrary path inputs is enforced internally.

---

## 7. True Git Data CAS Transport Engine

`GitDataCASRelayTransport` implements atomic compare-and-swap (CAS) semantics over GitHub's low-level REST Git Data API.

### 11-Step Publication Pipeline
1. **Read Branch Head:** Fetch current HEAD SHA of `refs/heads/control/controller-relay`.
2. **Pre-CAS Head Check:** Assert `current_HEAD == expected_head`. If mismatch -> fail immediately without creating Git objects.
3. **Fetch Base Tree:** Retrieve commit and root tree for `expected_head`.
4. **Target Absence Check:** Traverse tree to verify target path does NOT exist. If path exists -> reject with `HOLD_RECORD_ALREADY_EXISTS`.
5. **Create Blob:** Call `POST /repos/MertSGI/AOS/git/blobs` with canonical UTF-8 JSON payload -> returns `blob_sha`.
6. **Create Tree:** Call `POST /repos/MertSGI/AOS/git/trees` with `base_tree=expected_tree_sha` and single file entry (`path`, `mode="100644"`, `type="blob"`, `sha=blob_sha`) -> returns `new_tree_sha`.
7. **Create Commit:** Call `POST /repos/MertSGI/AOS/git/commits` with `tree=new_tree_sha`, `parents=[expected_head]`, message -> returns `new_commit_sha`.
8. **Update Ref:** Call `PATCH /repos/MertSGI/AOS/git/refs/heads/control/controller-relay` with `sha=new_commit_sha` and `force=False`.
9. **Post-CAS Head Check:** Re-read branch HEAD to assert `HEAD == new_commit_sha`.
10. **Parent Verification:** Verify `new_commit.parents[0] == expected_head`.
11. **Record Verification:** Read tree at `new_commit_sha` and verify target record path and exact blob SHA/content.

### One-Record-One-Commit Invariant
* Exactly ONE Relay record is added per commit.
* Never batch multiple records into a single commit.
* Never edit, delete, or overwrite existing records. Records are strictly immutable.

### CAS Race Handling
If branch HEAD advances between step 1 and step 8, step 8 (`PATCH ref`) returns a non-200 / 422 HTTP status.
* Disposition: `HOLD_CAS_RACE`.
* NO automatic retries.
* NO force push fallback (`force=False` is strictly enforced).
* NO fallback to GitHub Contents API.
* Unreachable Git objects created by losing writers are harmlessly ignored by Git garbage collection; live branch history remains unpolluted.

---

## 8. Endpoint Allowlist & Restricted Scope

The HTTP requester is strictly restricted to the following endpoints on target `MertSGI/AOS` and target ref `refs/heads/control/controller-relay`:

```
GET   /repos/MertSGI/AOS/git/ref/heads/control/controller-relay
GET   /repos/MertSGI/AOS/git/commits/{sha}
GET   /repos/MertSGI/AOS/git/trees/{sha}
GET   /repos/MertSGI/AOS/git/blobs/{sha}
POST  /repos/MertSGI/AOS/git/blobs
POST  /repos/MertSGI/AOS/git/trees
POST  /repos/MertSGI/AOS/git/commits
PATCH /repos/MertSGI/AOS/git/refs/heads/control/controller-relay
```

Any attempt to access other repos, refs, or endpoints raises an immediate validation exception.

---

## 9. Failure Matrix & Resilience

| Scenario | System Behavior | Disposition / Status |
| :--- | :--- | :--- |
| **Principal Mismatch** | Rejects before transport call (`TRANSPORT_CALL_COUNT=0`) | `FAIL` |
| **Malformed Payload / Schema Error** | Rejects before transport call (`TRANSPORT_CALL_COUNT=0`) | `FAIL` |
| **Secret Pattern Detected** | Rejects before transport call (`TRANSPORT_CALL_COUNT=0`) | `FAIL` |
| **Target Path Already Exists** | Rejects before object creation | `HOLD_RECORD_ALREADY_EXISTS` |
| **`expected_head` Mismatch** | Rejects before object creation | `HOLD_CAS_RACE` |
| **Concurrent Writer Win (Ref Patch Failure)** | Ref update fails closed | `HOLD_CAS_RACE` |
| **GitHub Remote API Unavailable / 5xx** | Transport fails closed, sanitizes error | `FAIL_CLOSED` |
| **Rate Limit Exceeded (429 / 403)** | Transport fails closed, returns retry-after header | `HOLD_RATE_LIMITED` |

---

## 10. Verification & Test Architecture

Unit testing follows strict deterministic offline rules:
1. **Fake Requester:** `FakeGitHubRequester` records all API calls in memory without opening socket connections (`NETWORK_CALL_COUNT=0`).
2. **Fake Credential Provider:** `FakeCredentialProvider` supplies synthetic token strings verified to never leak into error logs, reprs, or test outputs.
3. **Exact Coverage:** Tests assert:
   * Validation failures prevent transport invocation.
   * Path derivation correctly maps messages and receipts.
   * 11-step CAS sequence creates exactly one blob, tree, and commit with `force=False`.
   * CAS races correctly produce `HOLD_CAS_RACE` without retries or force fallbacks.
   * Remote GitHub errors are sanitized of credentials.
