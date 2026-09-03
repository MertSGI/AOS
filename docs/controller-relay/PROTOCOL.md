# AOS Controller Relay V1 Protocol Specification (CR-0 R1)

**Implementation Authority ID:** `LARI-AOS-CONTROLLER-RELAY-CR0-20260903-01`  
**Correction Authority ID:** `LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01`
**Protocol Version:** `CONTROLLER_RELAY_V1` / `CONTROLLER_RELAY_RECEIPT_V1`

---

## 1. Executive Summary & Core Invariant

Controller Relay V1 is a deterministic, network-free, non-authoritative protocol for transport and message exchange between autonomous system controllers (e.g., `AOS_CONTROLLER`, `LARI_CONTROLLER`, `SECURITY_CONTROLLER`, `RELEASE_CONTROLLER`, `QUALITY_CONTROLLER`).

### Normative Invariants:
```
RELAY_MESSAGE != AUTHORITY
CONTROLLER_INBOX_CLAIM != AUTHORITY
CANDIDATE_SHA != AUTHORITY
RELAY_RECEIPT != AUTHORITY
RELAY_CONSUMED != AUTHORITY_CONSUMED
```

* **Relay is transport only.** It MUST NOT grant, imply, infer, or execute system authority.
* Canonical authority remains strictly external to Relay and derives exclusively from independently verified canonical governance (`STATE.json`, `EVIDENCE.jsonl`, signed Git authority bindings).
* All Controller Relay V1 messages MUST set `authority_effect` to exactly `"NONE"`.

### Opaque Authority References Rule:
The field `authority_refs` provides opaque references only. `authority_refs` cannot prove:
* existence
* validity
* freshness
* scope
* non-consumption
* execution authorization

Only independently verified canonical governance can establish authority.

---

## 2. Three-Layer Model

| Layer | Component | Authority Designation | Description |
| :--- | :--- | :--- | :--- |
| **Layer 1** | **Controller Relay** | `NON-AUTHORITATIVE` | Transport layer for message passing, state progression receipts, and thread tracing. |
| **Layer 2** | **Controller Inbox** | `NON-AUTHORITATIVE` | Handoff claims, execution reports, and executor notifications. |
| **Layer 3** | **Canonical STATE / EVIDENCE** | `AUTHORITATIVE` | Verified governance, signed execution preflights, and canonical Git authority binding. |

---

## 3. Controller Identity Model

Controller identities are generic, bounded, safe identifiers matching the pattern:
```regex
^[A-Z0-9_]+_CONTROLLER$
```
Supported identities include, but are not limited to:
* `AOS_CONTROLLER`
* `LARI_CONTROLLER`
* `SECURITY_CONTROLLER`
* `RELEASE_CONTROLLER`
* `QUALITY_CONTROLLER`

---

## 4. Message Schema & Hashing

### 4.1 Message ID Format
Message IDs are strictly deterministic and follow the format:
```
CRV1-<FROM>-<TO>-<12_DIGIT_SEQUENCE>
```
Example: `CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001`

### 4.2 Canonical Content Hashing (`content_sha256`)
* The `content_sha256` field is computed over the UTF-8 encoded canonical JSON serialization of the message object **excluding** the `content_sha256` key itself.
* JSON Canonicalization Rules:
  * UTF-8 encoding without BOM.
  * Keys sorted lexicographically (`sort_keys=True`).
  * Compact separators without whitespace (`separators=(",", ":")`).
  * `ensure_ascii=False`.
  * No trailing newline in the hash input bytes.
* Recomputation by any receiver MUST yield an exact SHA-256 match; any mismatch causes immediate fail-closed rejection.

### 4.3 Payload Limits & Secret Defense
* **Maximum Payload Size:** 64 KiB (65,536 bytes) serialized UTF-8 JSON.
* **BOM Prohibited:** UTF-8 Byte Order Mark (`\xef\xbb\xbf`) is strictly forbidden and causes fail-closed rejection.
* **Duplicate JSON Keys Prohibited:** Duplicate JSON keys at any nesting level cause immediate fail-closed rejection (`DUPLICATE_JSON_KEY`).
* **Secret / Credential Defense:** Serialized messages are scanned for structured key names and credential shapes, including: `password`, `passwd`, `secret`, `api_key`, `apikey`, `access_token`, `refresh_token`, `private_key`, `client_secret`, `authorization`, `bearer_token`. Secret values are never logged or exposed in validation errors.

---

## 5. Directed Channel Monotonic Sequence Model

* Each directed channel defined by `(from, to)` operates an independent sequence space starting at sequence `1`.
* Monotonic increment rule: $\text{sequence}_{n+1} = \text{sequence}_n + 1$.
* Out-of-order sequence, sequence regression, sequence gaps, or duplicate `(from, to, sequence)` pairs result in immediate fail-closed rejection.

---

## 6. Threading & In-Reply-To Rules

### 6.1 Root Message
* `thread_id == message_id`
* `in_reply_to == null`

### 6.2 Reply Message
* `thread_id` MUST match the root message's `thread_id`.
* `in_reply_to` MUST reference an existing valid message ID within the same thread.
* **Direction Inversion Rule:** A reply MUST invert the directed channel:
  $$\text{reply.from} == \text{prior.to} \quad \text{and} \quad \text{reply.to} == \text{prior.from}$$
* Fail-closed conditions: unknown `in_reply_to`, cross-thread reply, self-reference (`in_reply_to == message_id`), invalid direction inversion, or reply against a stale superseded message.

---

## 7. Mandatory Inbound Check

```
MANDATORY_INBOUND_CHECK=YES
CHECK_LATEST_UNCONSUMED_INBOUND_RELAY_BEFORE_CROSS_CONTROLLER_ACTION=YES
```

A **cross-controller action** is defined as any action that:
* depends on another Controller's decision/state
* references another project's authority
* requests mutation/execution involving another Controller/project
* responds to a cross-controller request

### Mandatory Inbound Action Sequence:
Before executing any cross-controller action, a Controller MUST:
1. Obtain latest Relay branch state.
2. Validate sequence, thread, and receipt integrity.
3. Identify latest relevant unconsumed inbound record.
4. Independently verify canonical SHA and evidence.
5. Derive authority exclusively from canonical Layer-3 governance.
6. Act strictly inside independently proven authority boundaries.
7. Publish response as a new immutable Relay record where required.

*Pure local unrelated work does not require Relay.*

---

## 8. Relay Unavailable & Manual Transport Rules

If Relay is unavailable when a cross-controller action is required:
```
CROSS_CONTROLLER_ACTION=HOLD_RELAY_UNAVAILABLE
```

* **Silent Manual Fallback Forbidden:** Silent manual user copy/paste fallback is strictly forbidden after CR-1 becomes operational.
* **Manual Transport Classification:** Manual transport is permitted ONLY if explicitly classified as:
  ```
  MANUAL_BOOTSTRAP_OR_BREAK_GLASS
  ```
* **Manual Transport Scope & Limitations:** Manual transport MAY transport information. It MUST NOT itself:
  * create canonical authority
  * create a canonical `control_request`
  * mutate canonical `STATE` or `EVIDENCE`
  * imply execution authority
  * revive consumed authority
  * bypass a human gate

Any Layer-3 governance event remains independently governed by its own existing authority.

---

## 9. Receipt Binding & Lifecycle Distinction

### 9.1 Receipt Required Binding
Every receipt MUST bind:
* `protocol` (`CONTROLLER_RELAY_RECEIPT_V1`)
* `message_id` (exact referenced message ID)
* `message_content_sha256` (exact referenced message SHA-256)
* `message_commit_sha` (exact Git commit SHA containing the immutable message)
* `actor` (`^[A-Z0-9_]+_CONTROLLER$`)
* `event` (`OBSERVED`, `VERIFIED`, `ACKNOWLEDGED`, `CONSUMED`)
* `created_at` (`date-time`)

### 9.2 Lifecycle & Distinction
```
[PENDING] -> OBSERVED -> VERIFIED -> ACKNOWLEDGED -> CONSUMED
```

* **State Invariants:**
  * Duplicate event receipts for the same `(actor, message_id, event)` are rejected.
  * Transitions must occur in strict linear progression order.
  * For messages with `requires_reply=true`, transition to `CONSUMED` is valid only when an outbound reply decision exists or an explicit `HOLD`/`REJECT` response is present.

### Crucial Distinction:
```
# RELAY CONSUMED
transport processing complete

# CANONICAL AUTHORITY CONSUMED
authority consumption governed by canonical governance
```
Relay `CONSUMED` is a transport state transition only. It MUST NOT:
* consume execution authority
* mark authority unconsumed
* revive authority
* infer freshness

---

## 10. Supersession, Replay Protection & Concurrency

### 10.1 Supersession
* Published records are immutable and never modified in place.
* A message MAY populate `supersedes_message_id`.
* Requirements: MUST belong to the same thread, originate from the same `from` controller, reference an existing non-superseded message in the thread, and not supersede itself.

### 10.2 Future CR-1 Concurrency & CAS Contract
```
CONCURRENCY_MODEL=FAST_FORWARD_COMPARE_AND_SWAP
```
Future CR-1 publishing algorithm:
1. Fetch current Relay HEAD.
2. Validate current channel and thread state.
3. Create exactly one immutable record.
4. Push fast-forward only (NO FORCE PUSH).
5. On race loss: refetch, revalidate sequence/state, and retry ONLY if requested transport action remains valid.

### 10.3 Conflict Resolution
When two incompatible live decisions exist without valid supersession:
```
HOLD_AMBIGUOUS_CONCURRENT_DECISION
```
Silently picking a winner or auto-resolving conflicting decisions is strictly prohibited.

---

## 11. Complete Fail-Closed Condition Matrix

The following conditions MUST fail closed immediately:

1. `RELAY_BRANCH_UNAVAILABLE` — Relay branch unreadable or absent.
2. `RELAY_BRANCH_HISTORY_REWRITTEN` — Git history discontinuity or forced rewrite detected (HOLD).
3. `MESSAGE_CONTENT_HASH_MISMATCH` — Recomputed content SHA-256 does not match `content_sha256`.
4. `INVALID_UTF8` — Raw payload contains invalid UTF-8 bytes.
5. `UTF8_BOM_PRESENT` — UTF-8 Byte Order Mark header present.
6. `MESSAGE_TOO_LARGE` — Message payload exceeds 64 KiB (65,536 bytes).
7. `RECEIPT_TOO_LARGE` — Receipt payload exceeds 64 KiB (65,536 bytes).
8. `DUPLICATE_JSON_KEY` — Duplicate JSON key present at any nesting level.
9. `UNKNOWN_CONTROLLER_ID_FORMAT` — Controller identity does not match `^[A-Z0-9_]+_CONTROLLER$`.
10. `UNKNOWN_PROTOCOL_VERSION` — Protocol string is not `CONTROLLER_RELAY_V1` or `CONTROLLER_RELAY_RECEIPT_V1`.
11. `SEQUENCE_GAP` — Monotonic sequence contains a gap.
12. `DUPLICATE_CHANNEL_SEQUENCE` — Sequence number already used on directed channel.
13. `DUPLICATE_MESSAGE_ID` — Message ID already exists in repository history.
14. `UNKNOWN_THREAD` — Reply references non-existent thread.
15. `THREAD_ID_MISMATCH` — `thread_id` differs from root message ID.
16. `INVALID_REPLY_DIRECTION` — Reply direction does not invert `(from, to)` channel.
17. `UNKNOWN_IN_REPLY_TO` — `in_reply_to` references non-existent message ID.
18. `STALE_IN_REPLY_TO` — Reply targets a superseded message ID.
19. `INVALID_SUPERSESSION` — Cross-controller, cross-thread, or self-supersession attempt.
20. `DUPLICATE_RECEIPT_EVENT` — Duplicate receipt event emitted for same actor and message.
21. `INVALID_RECEIPT_ORDER` — Receipt event emitted out of linear progression order.
22. `RECEIPT_MESSAGE_HASH_MISMATCH` — Receipt message hash does not match target message hash.
23. `AMBIGUOUS_CONCURRENT_DECISION` — Multiple live conflicting decisions exist without supersession.
24. `CANONICAL_REFERENCE_UNVERIFIABLE` — `authority_refs` target absent or unparseable object.
25. `CANONICAL_SHA_MISMATCH` — Referenced canonical object SHA does not match governance snapshot.
26. `CANONICAL_AUTHORITY_ABSENT` — No canonical governance decision authorizes requested action.
27. `CANONICAL_AUTHORITY_CONSUMED` — Referenced canonical authority was already consumed.
28. `REQUESTED_ACTION_EXCEEDS_CANONICAL_AUTHORITY` — Requested next action exceeds scope of canonical authority.
29. `RELAY_MESSAGE_ATTEMPTS_AUTHORITY_EFFECT` — Message `authority_effect` is not strictly `"NONE"`.
30. `SECRET_OR_CREDENTIAL_PRESENT_IN_RELAY` — Structured secret key or high-confidence credential detected.

---

## 12. Future Architecture & Branch Layout

### Future Branch & Path Model (CR-1+)
* Dedicated Git transport branch: `control/controller-relay`
* Path layout:
  * Messages: `controller-relay/v1/messages/<FROM>--<TO>/<SEQUENCE>-<MESSAGE_ID>.json`
  * Receipts: `controller-relay/v1/receipts/<MESSAGE_ID>/<ACTOR>-<EVENT>.json`

> [!IMPORTANT]
> **CR-0 Scope Boundary:** CR-0 DOES NOT CREATE OR OPERATE THE `control/controller-relay` LIVE BRANCH.

### Implementation Roadmap
* **CR-0:** Protocol specification, schemas, pure deterministic validation, state machine, CLI integration. *(Current)*
* **CR-1:** Git transport branch manager & storage layer.
* **CR-2:** Automatic inbound detector & daemon loop.
* **CR-3:** Event-driven autonomous controller agent integration.
