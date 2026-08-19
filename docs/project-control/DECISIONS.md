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
