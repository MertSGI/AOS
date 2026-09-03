# AOS Controller Relay V1 Protocol Specification (CR-0)

**Implementation Authority ID:** `LARI-AOS-CONTROLLER-RELAY-CR0-20260903-01`  
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

## 7. Receipt State Machine & Lifecycle

Receipts track transport processing state over immutable message and receipt records:

```
[PENDING] -> OBSERVED -> VERIFIED -> ACKNOWLEDGED -> CONSUMED
```

* **Events:** `OBSERVED`, `VERIFIED`, `ACKNOWLEDGED`, `CONSUMED`.
* **Receipt Binding:** Every receipt strictly binds `protocol`, `message_id`, `message_content_sha256`, `message_commit_sha`, `actor`, `event`, and `created_at`.
* **State Invariants:**
  * Duplicate event receipts for the same `(actor, message_id, event)` are rejected.
  * Transitions must occur in strict linear progression order.
  * For messages with `requires_reply=true`, transition to `CONSUMED` is valid only when an outbound reply decision exists or an explicit `HOLD`/`REJECT` response is present.
  * `CONSUMED` indicates transport processing completion ONLY. It confers no execution authority.

---

## 8. Supersession & Conflict Resolution

### 8.1 Supersession
* Published records are immutable and never modified in place.
* A message MAY populate `supersedes_message_id`.
* Requirements: MUST belong to the same thread, originate from the same `from` controller, reference an existing non-superseded message in the thread, and not supersede itself.
* Transport meaning only: updates transport intent without modifying historical authority.

### 8.2 Conflict Resolution
* If two incompatible live decisions exist for the same inbound thread/request without valid supersession, the validator emits:
  `HOLD_AMBIGUOUS_CONCURRENT_DECISION`
* Silently picking a winner or auto-resolving conflicting decisions is strictly prohibited.

---

## 9. Break-Glass & Manual Bootstrap

In the event of network partition, invalid state deadlock, or transport failure, human operators may trigger a manual break-glass procedure. Break-glass operations bypass Relay transport queues by emitting direct Layer 3 canonical `control_request` governance events. Relay records CANNOT override or synthesize break-glass actions.

---

## 10. Future Architecture & Branch Layout

### Future Branch & Path Model (CR-1+)
* Dedicated Git transport branch: `control/controller-relay`
* Path layout:
  * Messages: `controller-relay/v1/messages/<FROM>--<TO>/<SEQUENCE>-<MESSAGE_ID>.json`
  * Receipts: `controller-relay/v1/receipts/<MESSAGE_ID>/<ACTOR>-<EVENT>.json`
* **Concurrency Primitive:** `FAST_FORWARD_COMPARE_AND_SWAP` via Git push assertions.

> [!IMPORTANT]
> **CR-0 Scope Boundary:** CR-0 DOES NOT CREATE OR OPERATE THE `control/controller-relay` LIVE BRANCH.

### Implementation Roadmap
* **CR-0:** Protocol specification, schemas, pure deterministic validation, state machine, CLI integration. *(Current)*
* **CR-1:** Git transport branch manager & storage layer.
* **CR-2:** Automatic inbound detector & daemon loop.
* **CR-3:** Event-driven autonomous controller agent integration.
