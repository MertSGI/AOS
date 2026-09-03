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
GET   /repos/MertSGI/AOS/git/trees/{sha}?recursive=1
GET   /repos/MertSGI/AOS/git/blobs/{sha}
POST  /repos/MertSGI/AOS/git/blobs
POST  /repos/MertSGI/AOS/git/trees
POST  /repos/MertSGI/AOS/git/commits
PATCH /repos/MertSGI/AOS/git/refs/heads/control/controller-relay
```

Any attempt to access other repos, refs, query parameters (e.g., `?recursive=0` or `?foo=bar`), or endpoints raises an immediate validation exception.

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
| **Truncated Recursive Tree Observed** | Rejects before object creation / history load | `HOLD_GIT_TREE_TRUNCATED` |
| **Invalid Historical Record Encountered** | Fails closed, rejects publication | `HOLD_INVALID_RELAY_HISTORY` |
| **Receipt `message_commit_sha` Mismatch** | Rejects receipt publication | `HOLD_RECEIPT_MESSAGE_COMMIT_MISMATCH` |
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
   * Exact production tree allowlist restrictions, truncated tree fail-closed behavior, invalid history rejection, and receipt commit SHA binding.

---

## 11. Foundation R1 Hardening Specifications

Foundation R1 establishes mandatory normative security, fail-closed, and provenance invariants:

### 11.1 Production Recursive Tree Allowlist
* The `StdlibGitHubRequester` endpoint allowlist permits EXACTLY:
  * `/repos/MertSGI/AOS/git/trees/<40-lowercase-hex-sha>`
  * `/repos/MertSGI/AOS/git/trees/<40-lowercase-hex-sha>?recursive=1`
* Broad query strings (e.g. `?recursive=0`, `?recursive=true`, `?foo=bar`) are strictly prohibited and raise an immediate `ControllerRelayTransportError`.

### 11.2 Truncated Tree Fail-Closed Behavior
* Any recursive tree response from GitHub containing `"truncated": true` during path absence proof, history listing, or post-write verification causes an immediate fail-closed abort (`HOLD_GIT_TREE_TRUNCATED`).
* No partial history is returned and zero Git objects are created.

### 11.3 Invalid Immutable History Fail-Closed Behavior
* All historical records under `controller-relay/v1/messages/` and `controller-relay/v1/receipts/` MUST individually pass strict raw parsing and validation (`validate_controller_relay_message_raw` / `validate_controller_relay_receipt_raw`).
* If any historical record is malformed JSON, UTF-8 BOM, contains duplicate keys, or fails schema validation, listing and service publication operations fail closed (`HOLD_INVALID_RELAY_HISTORY`).

### 11.4 History Provenance Model & Publication Commit Derivation
* Each historical record's immutable publication commit SHA is derived independently by walking first-parent Git ancestry (`RelayRecordProvenance`).
* For candidate commit $C$ and parent $P$, if path exists in $C$ and does not exist in $P$, $C$ is established as the publication commit SHA.
* Publication commit SHA is NEVER inferred from current branch HEAD, receipt claims, or caller input.

### 11.5 Receipt Message Commit Binding
* Before publishing a receipt, `ControllerRelayService` resolves the exact target message and its derived publication commit SHA.
* `receipt.message_commit_sha` MUST match the derived publication commit SHA exactly. Mismatches return `HOLD_RECEIPT_MESSAGE_COMMIT_MISMATCH` with zero transport mutations.

### 11.6 True Reply Guard for `requires_reply`
* For a target message $M$ where `requires_reply=true`, a `CONSUMED` receipt is permitted ONLY if a real outbound reply $R$ exists in validated history satisfying:
  * $R.\text{message\_id} \neq M.\text{message\_id}$
  * $R.\text{in\_reply\_to} == M.\text{message\_id}$
  * $R.\text{thread\_id} == M.\text{thread\_id}$
  * $R.\text{from} == M.\text{to}$
  * $R.\text{to} == M.\text{from}$

### 11.7 Transport Read Failure Propagation & No Partial History
* Transport exceptions during history listing propagate immediately, preventing any message/receipt publication.
* Live identity phase remains blocked until R1/R2 foundation hardening is formally accepted.

---

## 12. Foundation R2 Hardening Specifications

Foundation R2 completes the service trust boundary and closes all lineage, host, credential, and reappearance trust gaps:

### 12.1 Single Global History Validator
* `ControllerRelayService` enforces a single, unified, fail-closed service-level validator `_validate_existing_relay_history(expected_head)`.
* Both `publish_message()` and `publish_receipt()` MUST invoke `_validate_existing_relay_history(expected_head)` before any Git object creation or ref mutation (`GIT_OBJECT_CREATE_COUNT=0`, `REF_UPDATE_COUNT=0`, `TRANSPORT_PUBLICATION_COUNT=0`).
* No separate or weaker history validation paths are maintained.

### 12.2 Trusted Bootstrap Boundary & Single First-Parent Lineage
* Trusted bootstrap boundary is fixed to `039232ecf10948bf55a9d9dab665828b6c06f7c6`.
* Every provenance walk validates single first-parent ancestry from `expected_head` back to `039232ecf10948bf55a9d9dab665828b6c06f7c6`.
* If trusted bootstrap cannot be reached, validation fails with `HOLD_RELAY_BOOTSTRAP_UNREACHABLE`.
* If any commit in the ancestry contains more than one parent, history fails closed (`HOLD_INVALID_RELAY_HISTORY`).

### 12.3 Complete Provenance Mapping & Binding
* Historical message records yield an exact mapping `message_id -> { message, publication_commit_sha, path, publication_ordinal }`. Duplicate `message_id` across history fails closed (`HOLD_INVALID_RELAY_HISTORY`).
* Historical receipts require:
  * Referenced `message_id` exists in history.
  * `receipt.message_content_sha256 == target_message.content_sha256`.
  * `receipt.message_commit_sha == target_message.publication_commit_sha` (derived from Git, never claimed from payload).

### 12.4 Actual Git Publication Order & Temporal Reply Validation
* Historical receipt lifecycle validation is performed in actual Git publication commit order (`publication_ordinal`). Out-of-order published events fail closed (`HOLD_INVALID_RELAY_HISTORY`).
* For `requires_reply=true` messages and `CONSUMED` receipts, a qualifying reply MUST have been published BEFORE the `CONSUMED` receipt publication commit (`reply.publication_ordinal < consumed_receipt.publication_ordinal`). Replies published after a `CONSUMED` receipt cannot retroactively repair history.

### 12.5 Delete / Re-Add & Record Continuity Detection
* Ancestry walking verifies that record paths are absent from all ancestors prior to first introduction (`i_pub`).
* If a path already exists at `039232ecf10948bf55a9d9dab665828b6c06f7c6`, publication provenance is unverifiable (`HOLD_RECORD_PROVENANCE_UNVERIFIABLE`).
* If a record disappears after introduction and reappears later (`PRESENT -> ABSENT -> PRESENT`), validation fails with `HOLD_RELAY_RECORD_REAPPEARED`.
* Record content bytes MUST remain identical across all descendant commits (`HOLD_RELAY_RECORD_MUTATED`).

### 12.6 Canonical GitHub API Host Pinning & Credential Non-Access Guarantee
* Production API host is strictly pinned to `https://api.github.com`.
* `StdlibGitHubRequester.__init__` validates `base_url` before calling `CredentialProvider.get_token()`. Invalid host configurations fail before credential retrieval (`CREDENTIAL_PROVIDER_GET_TOKEN_CALL_COUNT=0`), HTTP request construction, or network attempts.

### 12.7 Non-Authority Invariant & Live Freeze
* Relay V1 records generate NO execution authority (`authority_effect` MUST equal `"NONE"`).
* Live Relay branch `control/controller-relay` remains frozen (`LIVE_RELAY_WRITE_COUNT=0`).

---

## 13. Identity Adapter R0 Specifications

Identity Adapter R0 defines the normative credential chain and one-shot handshake preparation boundaries:

### 13.1 Normative Credential Chain Architecture
Future live production credential flow operates across the following strict pipeline:
```
GitHub App private key (stored in external secure vault)
  -> trusted external secret injector (OUTSIDE service core)
  -> GitHub App JWT generation (OUTSIDE service core)
  -> GitHub App installation access token request (OUTSIDE service core)
  -> InjectedInstallationTokenCredentialProvider (memory-only, 120s safety margin)
  -> StdlibGitHubRequester (pinned to https://api.github.com)
  -> GitDataCASRelayTransport (11-step CAS over REST API)
  -> ControllerRelayService
```

### 13.2 Core Identity Boundaries & Safety Invariants
1. **External Key & JWT Generation:** Private key creation, JWT generation, and installation access token requests MUST occur strictly OUTSIDE the Relay service core.
2. **Short-Lived Installation Access Token:** The short-lived installation access token is the ONLY credential string entering the Relay process.
3. **Memory-Only & Redaction:** Installation tokens are process-memory-only and MUST NOT be logged, printed, serialized, or written to disk/artifacts (`token='***REDACTED***'`).
4. **120-Second Lifetime Safety Margin:** Before returning token material, `InjectedInstallationTokenCredentialProvider.get_token()` evaluates remaining lifetime against an explicit 120-second safety margin (`HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY`).
5. **Fixed Role-to-Principal Mapping:** Handshake planning strictly maps `AOS_ROOT` -> `ControllerPrincipal("AOS_CONTROLLER")` and `LARI_REPLY` -> `ControllerPrincipal("LARI_CONTROLLER")`. All caller overrides of principal or identity fields are rejected.
6. **R0 DRY_RUN Only:** Identity Adapter R0 operates strictly in `DRY_RUN` mode (`CREDENTIAL_ACCESS_COUNT=0`, `TRANSPORT_MUTATION_COUNT=0`, `LIVE_RELAY_WRITE_COUNT=0`). No live network calls or GitHub App provisioning occur during R0.

### 13.3 Intended Next-Phase GitHub App Installation Contract
For future authorization phases:
* `IDENTITY_TYPE`: Dedicated GitHub App Installation.
* `REPOSITORY_SELECTION`: Pinned exclusively to `MertSGI/AOS`.
* `PERMISSIONS`: `Contents: Read & write` only (`refs/heads/control/controller-relay` branch target).
* `PROHIBITED PERMISSIONS`: NO Actions write, Workflows write, Issues, Pull requests, Deployments, Administration, or Organization permissions.

---

## 14. Live One-Shot Invoker R0 Specifications

Live Invoker R0 defines the normative one-shot live publication boundary for CR-1 capability handshake wiring:

### 14.1 DRY_RUN Planner vs LIVE One-Shot Invoker

| Aspect | `execute_cr1_dry_run` | `execute_cr1_live` |
| :--- | :--- | :--- |
| **Mode** | `DRY_RUN` — pure in-memory calculation | `LIVE` — publishes through service/transport |
| **Transport** | None (zero transport calls) | Receives already-constructed `ControllerRelayService` |
| **Credential Discovery** | None | None — invoker does not inspect credential-provider internals |
| **Network** | Forbidden | Delegated to transport via service; R0 uses fake/in-memory requester |
| **Git Objects Created** | 0 | 1 blob + 1 tree + 1 commit per message (via transport) |
| **Ref Updates** | 0 | 1 per message (via transport, `force=False`) |
| **Output** | Safe metadata dict with zero-mutation counters | Safe metadata dict with publication commit SHA |

### 14.2 Live Invoker Receives Pre-Constructed Service
* `execute_cr1_live(role, service, expected_head, created_at)` receives an already-constructed `ControllerRelayService` instance.
* The invoker performs **zero credential discovery** — it does not instantiate requesters, credential providers, or transport layers.
* The caller is responsible for constructing the full stack: `requester -> transport -> service`.

### 14.3 Fixed Controller-Role Mapping
* `AOS_ROOT` -> `ControllerPrincipal("AOS_CONTROLLER")`
* `LARI_REPLY` -> `ControllerPrincipal("LARI_CONTROLLER")`
* No arbitrary principal, token, or credential parameters accepted.

### 14.4 Expected Head & CAS Rules
* **Root expected parent:** Trusted bootstrap SHA `039232ecf10948bf55a9d9dab665828b6c06f7c6`.
* **Reply expected parent:** Root publication commit SHA (derived from live root observation).
* Before message construction/publication: `service.get_head() == expected_head` is required. Mismatch -> `HOLD_CAS_RACE`.
* CAS failure has **no automatic retry** (`AUTOMATIC_RETRY_COUNT=0`).

### 14.5 Observation Through Service History APIs
* LARI_REPLY obtains root through `observe_exact_cr1_root(service, expected_head)` which:
  1. Verifies `service.get_head() == expected_head`
  2. Calls `service.list_message_provenances(ref=expected_head)` triggering complete accepted history validation
  3. Locates exact root message by ID
  4. Reads raw immutable bytes through `service.read_record(path, ref=expected_head)`
  5. Validates raw bytes and enforces exact R0-R1 root binding
* AOS reply verification is **read-only** — zero additional blobs, trees, commits, or ref updates.

### 14.6 R0 Wiring Proof Boundaries
* R0 performs **ZERO real Relay writes** (`REAL_LIVE_RELAY_WRITE_COUNT=0`)
* R0 performs **ZERO credential access** (`REAL_CREDENTIAL_ACCESS_COUNT=0`)
* R0 uses a fake/in-memory requester that proves actual service-to-transport wiring
* Receipt live lifecycle remains next-phase authority
* Relay remains non-authoritative: `RELAY_MESSAGE != AUTHORITY`

### 14.7 Error Mapping

| Scenario | Disposition |
| :--- | :--- |
| Wrong expected/current head | `HOLD_CAS_RACE` |
| Root missing | `HOLD_CROSS_CONTROLLER_HANDSHAKE` |
| Reply missing | `HOLD_CROSS_CONTROLLER_HANDSHAKE` |
| Invalid observed root binding | `HOLD_INVALID_OBSERVED_ROOT` |
| Wrong reply contract | `HOLD_CROSS_CONTROLLER_HANDSHAKE` |
| Service publication validation failure | Propagate accepted disposition without retry |
| Transport CAS failure | `HOLD_CAS_RACE` |
