# AOS Decision Register

**Version:** 0.1.0
**Date:** 2026-08-19

Decisions here record the current agreement. A decision may later be superseded, but should not be silently rewritten.

---

## AOS-DEC-001 — Build a reusable orchestration platform
**Status:** ACCEPTED
**Decision:** Build AOS as a reusable multi-project control and orchestration platform, not as a LARİ-specific automation.

**Reason:** Lessons learned in LARİ must transfer to future products.

---

## AOS-DEC-002 — LARİ is the first pilot, not the owner of AOS
**Status:** ACCEPTED
**Decision:** LARİ will validate AOS, but AOS core will live independently from LARİ business logic.

---

## AOS-DEC-003 — Preserve existing LARİ governance
**Status:** ACCEPTED
**Decision:** AOS adoption must preserve LARİ's accepted roadmap, exact-SHA discipline, evidence levels, fail-closed behavior, HOLD semantics, reopen policy and human authority boundaries.

---

## AOS-DEC-004 — Canonical memory is versioned
**Status:** ACCEPTED
**Decision:** Project truth will not depend solely on ChatGPT/agent conversational memory. Accepted state, decisions, policies and evidence must be versioned and machine-readable.

---

## AOS-DEC-005 — GPT-5.6 Sol is the baseline critical reasoning engine
**Status:** ACCEPTED_AS_BASELINE
**Decision:** Use GPT-5.6 Sol for high-consequence planning and independent semantic review in the initial architecture.

**Constraint:** Model replacement or lower-cost routing may be evaluated later, but must be benchmarked and must not silently weaken governance.

---

## AOS-DEC-006 — Antigravity is an execution adapter
**Status:** ACCEPTED_AS_INITIAL_ADAPTER
**Decision:** Google Antigravity is the first execution-worker integration. It is not canonical roadmap authority.

**Reason:** AOS should remain able to support other worker adapters later.

---

## AOS-DEC-007 — Human remains critical-decision authority
**Status:** ACCEPTED
**Decision:** Routine execution can become autonomous; critical scope, production, destructive data, security, billing and equivalent decisions remain human-gated unless an explicit later governance decision changes a narrowly defined category.

---

## AOS-DEC-008 — Planner/executor/verifier separation
**Status:** ACCEPTED
**Decision:** The same executor claim is never sufficient by itself to close a gate. Independent verification is required.

---

## AOS-DEC-009 — Deterministic policy enforcement first
**Status:** ACCEPTED
**Decision:** Use code for enforceable rules such as schema validity, lease ownership, branch scope and exact SHA. Use model judgment for semantic reasoning that cannot be reduced safely to deterministic rules.

---

## AOS-DEC-010 — Git/GitHub is canonical; coordination state is disposable
**Status:** ACCEPTED
**Decision:** Durable project truth lives in Git/GitHub. Task leases and worker heartbeats use an operational coordination store and can be reconstructed.

---

## AOS-DEC-011 — Multi-PC execution is a core requirement
**Status:** ACCEPTED
**Decision:** AOS must support multiple independently authenticated workers running different projects or lanes in parallel.

---

## AOS-DEC-012 — Lease-based task ownership
**Status:** ACCEPTED
**Decision:** Distributed workers must atomically claim tasks with expiring leases. Two workers may not legitimately execute the same active task simultaneously.

---

## AOS-DEC-013 — AOS is introduced progressively
**Status:** ACCEPTED
**Decision:** Rollout sequence is:
`DOCUMENT -> SHADOW -> CONTROLLED EXECUTION -> VERIFIED AUTONOMY -> DISTRIBUTED AUTONOMY`.

Full autonomy is not the starting state.

---

## AOS-DEC-014 — AOS does not reset the LARİ timeline
**Status:** ACCEPTED
**Decision:** The AOS initiative is a parallel engineering/control-plane lane. It may not reset the existing LARİ delivery commitment or accepted delivery train.

---

## AOS-DEC-015 — Reusable lessons require promotion
**Status:** ACCEPTED
**Decision:** A project-specific lesson does not automatically become global policy. It is promoted to global AOS memory only after determining that it is truly cross-project.

---

## AOS-DEC-016 — Cost must be observable
**Status:** ACCEPTED
**Decision:** Model/API costs are measured per task/purpose. Optimization is evidence-based and cannot weaken safety standards.

---

## AOS-DEC-017 — Working name AOS is provisional
**Status:** PROVISIONAL
**Decision:** Use “AOS — Agent Operating System” as the project codename until branding/naming is intentionally reviewed. Naming is not on the critical path.

---

## AOS-DEC-018 — Python 3.12 Initial Runtime Baseline
**Status:** ACCEPTED
**Decision:** Python 3.12 is the initial implementation baseline for the AOS deterministic controller, schema validator and orchestration utilities.

**Rationale:**
* cross-platform support,
* strong process/subprocess orchestration,
* mature JSON/schema tooling,
* suitable OpenAI/API integrations,
* suitable future Postgres/Supabase coordination integration,
* low bootstrap complexity.

This does not prohibit future worker adapters or components from using other languages.

---

## AOS-DEC-019 — Agent-First Execution and Human Transport Elimination
**Status:** ACCEPTED
**Decision:** AOS exists to remove the human product owner from routine transport and execution mechanics between reasoning agents and execution workers.

If an operation can be safely performed by the AOS controller or an authorized execution worker, the human must not be asked merely to:

* run terminal commands,
* create or edit routine implementation files,
* start tests,
* perform routine Git add/commit/push operations,
* move artifacts between tools,
* relay routine planner/executor/verifier messages.

Human participation remains required for configured human gates, critical product decisions, authentication that genuinely requires human interaction, production-sensitive authority, and unresolved HOLD conditions.

Manual transport during AOS bootstrap is transitional behavior, not the target operating model.

---

## AOS-DEC-020 — Provider-Agnostic Reasoning, Cost and Data Routing
**Status:** ACCEPTED
**Decision:** AOS must not depend on a single model/provider. Provider/model selection is deterministic and policy-driven according to: task risk class, required capabilities, data classification, provider availability, observed benchmark quality, and billing/cost policy.

GPT-5.6 Sol remains the high-consequence reasoning reference baseline. Qualified lower-cost/free providers may be used for lower-risk work after benchmarking. Paid fallback is disabled by default. Provider routing must never weaken deterministic governance, evidence, human-gate or fail-closed requirements.

**Clarification:** This decision does not supersede AOS-DEC-005. It clarifies that DEC-005 establishes a baseline, not a permanent exclusive mandate.

---

## AOS-DEC-021 — Human Control Ingress and Canonical Change Transactions
**Status:** ACCEPTED
**Decision:** Human project authority communicates interventions, policy changes, roadmap/scope/priority changes and pause/resume requests through versioned control requests pinned to an exact canonical control revision.

A human request must NOT directly instruct execution workers. AOS performs impact analysis and required gates first. Accepted material changes become canonical transactions. Future controlled execution tasks will carry a control generation/epoch identity and fail closed when operating against stale control authority.

Safe PAUSE/HOLD may stop execution immediately. RESUME and material roadmap/policy changes require canonical validation.
