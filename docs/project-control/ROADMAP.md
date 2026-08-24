# AOS Roadmap
## Gate-Based Delivery Plan

**Version:** 0.1.0
**Date:** 2026-08-19
**Planning mode:** Gate-based, evidence-driven
**Calendar estimate:** first useful pilot in 1–2 focused days; distributable/hardened initial system in approximately 3–5 focused engineering days, subject to tool integration findings. Calendar estimates do not override gate evidence.

---

# Delivery Train

```text
AOS-0 FOUNDATION
   ↓
AOS-1 CANONICAL MEMORY & SCHEMAS
   ↓
AOS-2 SHADOW ORCHESTRATOR
   ↓
AOS-3 CONTROLLED SINGLE-WORKER EXECUTION
   ↓
AOS-4 INDEPENDENT VERIFICATION & HOLD
   ↓
AOS-5 DISTRIBUTED MULTI-PC COORDINATION
   ↓
AOS-6 LARİ CONTROLLED PILOT
   ↓
AOS-7 PORTABLE BOOTSTRAP / SECOND PROJECT
   ↓
AOS-8 V1 HARDENING
```

AOS is developed parallel to LARİ. LARİ's accepted master delivery train is not replaced or reset.

---

## AOS-0 — Foundation & Charter
**Goal:** Convert the agreed operating philosophy into canonical documentation.

### Deliverables
- Master charter
- Roadmap
- Decision register
- Initial machine-readable state
- Autonomy/human-gate policy
- Definition of evidence
- LARİ integration guardrails

### Exit criteria
- No conflict with LARİ accepted control state.
- Authority boundary is explicit.
- AOS is declared reusable, not LARİ-specific.
- Human-required events are enumerated.

### Status
`CLOSED_PROVEN` (`E1_REMOTE_SOURCE_PROVEN`, verified SHA `8e2a3768b8506cf86d3649d43a11cc6419768481`)
---

## AOS-1 — Canonical Memory & Schemas
**Goal:** Make project knowledge machine-readable and versioned.

### Work
- Define JSON schemas for:
  - AOS state
  - project descriptor
  - task
  - lease
  - evidence
  - decision event
  - escalation
- Define global governance directory.
- Define project adapter contract.
- Create validation CLI.

### Evidence
E2-equivalent:
- schema tests,
- valid/invalid fixtures,
- deterministic validator CI.

### Exit criteria
- invalid state rejected,
- unknown fields/version drift handled intentionally,
- exact revision references required where applicable.

### Status
`CLOSED_PROVEN` (`E2_EXECUTABLE_EXACT_REVISION_PROVEN`, verified SHA `dba163276a141688685c23ecca884d4a17893e3a`)

---

## AOS-2 — Shadow Orchestrator
**Goal:** Let AOS decide the next action without mutating product code.

### Work
- Read canonical project state.
- Call a bounded PlannerProvider with deterministic structured output.
- Use deterministic provider routing: GPT-5.6 Sol high-consequence reference baseline; qualified free/lower-cost providers allowed for R0 shadow evaluation after benchmarking.
- Validate planner output against the same deterministic planner decision schema regardless of provider.
- Compare suggested next action against the independently pinned exact canonical control revision.
- Store decision trace without changing product state.

### LARİ proof case
The LARI proof case is evaluated against an independently pinned exact canonical control revision and shadow expectation. Project progress must not require editing AOS core roadmap text.

### Exit criteria
- repeated shadow decisions remain within accepted roadmap,
- no invented phase,
- no unauthorized mutation,
- ambiguous scenario transitions to HOLD.

### Status
`CLOSED_PROVEN` (`E3_ISOLATED_RUNTIME_PROVEN`, verified SHA `2c756c9772673adf887c770572409dfa71a83c93`, live proof run `32489975306`)

---

## AOS-3 — Controlled Single-Worker Execution
**Goal:** Execute one low-risk bounded task through an Antigravity worker.

### Work
- task specification,
- base SHA pinning,
- isolated branch/worktree,
- allowed path scope,
- worker adapter,
- command timeout,
- structured output capture,
- max retry policy,
- Human Control Ingress consumption,
- task control-generation / control-epoch pinning,
- stale-control task invalidation.

### Prerequisites
- Controlled execution requires the integrated project's normalized canonical control plane to expose a stable machine-readable execution-base authority for the current next action (e.g., `next_action_execution_base_sha`).

### First eligible task class
R1 isolated implementation only.

### Explicitly excluded
- production,
- destructive data,
- payment,
- secrets,
- roadmap changes.

### Exit criteria
- worker cannot legitimately mutate outside task scope,
- exact base revision is recorded,
- abandoned work is recoverable,
- tasks fail closed when operating against stale control authority.

### Status
`CLOSED_PROVEN` (`E3_ISOLATED_RUNTIME_PROVEN`, verified execution proof SHA `c96e1c8c29d8a1dfb0f881651a0588ee9d8c0906`, authorization SHA `fd5d2f5b4ed20b374fb319e8a8cc5191f6c3f8c2`, reference task `AOS3-REF-001`)

---

## AOS-4 — Independent Verification & HOLD
**Goal:** Prevent executor self-attestation from becoming closure.

### Work
- deterministic changed-file check,
- build/test execution,
- exact-SHA check,
- evidence validator,
- independent GPT semantic review,
- contradiction detector,
- HOLD report generator.

### Required test scenarios
1. valid PASS,
2. failing deterministic test,
3. executor claims browser proof but artifact is absent,
4. stale SHA,
5. scope violation,
6. contradictory accepted evidence.

### Exit criteria
- invalid cases do not close,
- post-HOLD unauthorized mutation count is zero,
- evidence requirements are gate-specific.

---

## AOS-5 — Distributed Multi-PC Coordination
**Goal:** Run independent work lanes from multiple machines safely.

### Architecture
- canonical state: Git/GitHub,
- ephemeral coordination: pluggable backend,
- recommended first distributed backend: Postgres/Supabase lease table,
- local-only backend: SQLite.

### Work
- worker registration,
- heartbeat,
- atomic task lease,
- lease expiry/recovery,
- task ownership,
- worker capability tags,
- branch naming,
- stale-base handling.

### Required proof
Two workers attempt to claim the same task concurrently:
- exactly one succeeds,
- loser receives another task or idles safely.

### Exit criteria
- no duplicate active ownership,
- crashed worker task can be recovered,
- canonical history remains intact if coordination DB is lost.

---

## AOS-6 — LARİ Controlled Pilot
**Goal:** Prove AOS on the real LARİ process without weakening LARİ governance.

### Pilot sequence
1. Shadow-read the LARİ control state.
2. Select one R1 task from an approved lane.
3. Execute in isolated worktree.
4. Verify.
5. Produce evidence.
6. Stop before any human-gated action.
7. Compare AOS result with independent manual review.

### Parallel lane candidate
UI V2 may later be used as a non-blocking parallel work lane, consistent with existing LARİ governance.

### LARİ invariants
- Core RC accepted baseline is not rewritten.
- Existing CLOSED_PROVEN gates remain closed unless empirical contradiction satisfies reopen policy.
- AOS does not invent a new LARİ roadmap phase.
- 12-week roadmap is not reset to accommodate AOS.

### Exit criteria
- at least one successful evidence-backed closure,
- at least one intentionally injected HOLD scenario,
- no unauthorized LARİ control-plane update.

---

## AOS-7 — Portable Bootstrap & Second Project
**Goal:** Prove AOS is a platform, not a LARİ automation.

### Work
- install/bootstrap script,
- `aos init`,
- environment diagnostics,
- worker registration,
- project template,
- migration/import helper for an existing repo,
- onboarding documentation.

### Target
Second PC:
- predictable installation,
- local secrets setup,
- worker identity,
- project clone,
- ready to claim compatible tasks.

Second project:
- onboard by configuration and governance files,
- no copying LARİ-specific business logic into AOS core.

### Exit criteria
- second PC works,
- second independent repository reaches shadow mode,
- onboarding friction is measured.

---

## AOS-8 — V1 Hardening
**Goal:** Make unattended execution trustworthy enough for regular use.

### Work
- crash recovery,
- audit log,
- cost telemetry,
- rate-limit handling,
- model failure handling,
- backup/export,
- observability dashboard or summary,
- overnight digest,
- permissions review,
- threat model,
- versioned migrations.

### V1 release gate
All Master Charter V1 Definition-of-Done conditions pass.

---

# Parallelization Plan

After AOS-5:

```text
Worker PC-1
  └── LARİ Package/Customer Customization

Worker PC-2
  └── LARİ UI V2

Worker PC-3 / later
  └── Other Project

Shared:
  AOS governance
  reasoning policies
  task/evidence schemas
  distributed coordination
```

Parallelism is allowed only when workstreams are dependency-safe and branch ownership is isolated.

---

# Time Budget

These are engineering estimates, not contractual commitments.

| Work package | Estimated focused effort |
|---|---:|
| AOS-0 documentation/foundation | 2–4 h |
| AOS-1 schemas/memory | 3–5 h |
| AOS-2 shadow planner | 3–5 h |
| AOS-3 worker adapter | 3–5 h |
| AOS-4 verifier/HOLD | 4–7 h |
| AOS-5 distributed coordination | 4–7 h |
| AOS-6 real LARİ pilot | 4–8 h |
| AOS-7 packaging/second PC/project | 3–6 h |
| AOS-8 initial hardening | 4–8 h |

Total initial V1 engineering envelope:
approximately **30–55 focused engineering hours**, refined after AOS-2/AOS-3 integration measurements.

The first useful shadow/controlled version should arrive much earlier than full V1.

---

# LARİ Schedule Protection Rule

AOS implementation does not earn schedule priority merely because it is interesting.

If AOS begins to block the current LARİ critical path, the default action is:

`AOS_PARALLEL_OR_PAUSE`

not:

`RESET_LARI_ROADMAP`.

AOS earns more autonomy only after it demonstrates net benefit without reducing evidence quality.
