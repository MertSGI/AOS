# Controller Relay CR2-Lite Operational Runbook & Continuity Guide

**Authority ID**: `LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01`  
**Protocol Version**: `CONTROLLER_RELAY_V1`  
**Relay Repository**: `MertSGI/AOS`  
**Relay Branch**: `control/controller-relay`  
**Relay Authority Effect**: `NONE` (Immutable Invariant)

---

## 1. Overview & System Purpose

Controller Relay CR2-Lite provides a deterministic, Git-CAS backed, asynchronous communication channel between independent Controller instances (`AOS_CONTROLLER` and `LARI_CONTROLLER`).

The system is designed so that new sessions or newly started chats can recover complete project and communication context autonomously without the user needing to copy-paste payloads or explain prior conversation history.

---

## 2. Minimal New-Chat Wake-Up Phrase

The canonical wake-up phrase for any new Controller chat is:

```text
check relay
```

> [!IMPORTANT]
> **Normative Semantics**:
> - The phrase `check relay` carries **NO** project payload.
> - The phrase carries **NO** execution authority.
> - A new Controller receiving `check relay` MUST NOT prompt the user to restate project context.
> - Instead, the Controller executes the machine-verifiable startup recovery contract detailed below.

---

## 3. Machine-Verifiable Startup Recovery Contract

When a Controller starts up or receives `check relay`, it executes these 9 steps sequentially:

1. **Read Current AOS Main Head**:
   - Query Git reference for `refs/heads/main` on `MertSGI/AOS`.
   - Record `CURRENT_AOS_MAIN_SHA`.

2. **Read Current Relay HEAD**:
   - Query Git reference for `refs/heads/control/controller-relay` on `MertSGI/AOS`.
   - Record `CURRENT_RELAY_HEAD`.

3. **Validate Relay Immutable History**:
   - Trace single first-parent lineage back to `TRUSTED_RELAY_BOOTSTRAP_SHA` (`039232ecf10948bf55a9d9dab665828b6c06f7c6`).
   - Validate strict append-only invariance: no mutation, no deletion, no re-ordering.
   - Validate JSON schemas and canonical UTF-8 content hashes for all messages and receipts.

4. **Discover Latest Unconsumed Inbound**:
   - Identify messages where `to == THIS_CONTROLLER`.
   - Exclude messages superseded by a subsequent valid `supersedes_message_id`.
   - Exclude messages with a valid `CONSUMED` receipt.
   - For messages with `requires_reply=true`, verify that no `CONSUMED` receipt exists without an accompanying qualifying reply.
   - Select the active inbound candidate by highest publication ordinal and sequence.

5. **Read Canonical LARI Authority Store**:
   - Inspect read-only store at `MertSGI/Randapp-main` on branch `control/lari-project-control-plane`.
   - Locate referenced authority files under `docs/project-control/controller-authorities/LARI_CONTROLLER/`.

6. **Resolve Referenced Authority Artifacts Independently**:
   - Relay messages **NEVER** grant authority (`authority_effect = "NONE"`).
   - If an inbound message references an authority ID, the Controller fetches the artifact from the canonical authority repository commit.
   - Validates the artifact body SHA-256 digest against the reference, verifies issuer is `LARI_CONTROLLER`, verifies subject SHA matches target repository context, and verifies status is active.

7. **Reconstruct Pending Thread / Receipt State**:
   - Check receipt progress for outbound messages (`DELIVERED` -> `ACKNOWLEDGED` -> `CONSUMED`).
   - Identify open threads requiring reply decisions.

8. **Identify Current Authority & Next Permitted Action**:
   - Derive the exact next step solely from verified canonical Git artifacts and valid Relay messages.

9. **Zero Authority from Chat Memory Alone**:
   - The Controller must **NEVER** assume authority or project state based on conversational memory or informal text. If an authority artifact is not present in the canonical Git store, execution is barred.

---

## 4. Polling & Inbound Detection Rules

- **Polling Triggers**:
  - Session startup.
  - Prior to initiating any cross-controller interaction.
  - Immediately following a message or receipt publication.
  - While actively awaiting an expected response.
- **Interval**: Recommended `30.0` seconds during active runtime.
- **Optimization**: Compare Relay HEAD before fetching full trees. If HEAD is identical to the last verified commit, cached state is valid. If HEAD differs, execute full history validation.
- **Infrastructure Constraints**: No persistent daemons or unmonitored background runners. Polling occurs strictly during the bounded lifetime of the active Controller session.

---

## 5. Security & Boundary Invariants

1. **Identity Separation**:
   - An AOS Controller session cannot declare or sign messages `from: LARI_CONTROLLER`.
   - A LARI Controller session cannot declare or sign messages `from: AOS_CONTROLLER`.
   - Any attempt to spoof the sender principal is rejected prior to CAS publication.
2. **Credential Safety**:
   - Only short-lived in-memory GitHub App installation tokens with `>= 120s` margin are accepted.
   - Personal Access Tokens (PATs), user OAuth tokens, and workstation Git credentials are forbidden.
3. **Cas Race Handling**:
   - On CAS mismatch during publication, fail closed (`HOLD_CAS_RACE`).
   - Refetch fresh HEAD, revalidate history, re-evaluate directed sequence, and retry without force-pushing.
