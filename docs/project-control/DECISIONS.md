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

---

## AOS-DEC-022 — Exception-Only Human Approval and Policy-Issued Execution Authority
**Status:** ACCEPTED
**Decision:** Routine execution and retry operations in isolated, non-production environments proceed autonomously under policy-issued execution authority without requiring human re-authorization at every step.

Canonical authoritative outcomes:
- `AUTO_EXECUTE`: Autonomous execution authorized by policy.
- `AUTO_REMEDIATE`: Safe autonomous diagnostic or reprobe operation authorized.
- `HUMAN_REQUIRED`: High-consequence or out-of-scope operation requiring human intervention.
- `FORBIDDEN`: Violates hard security or policy boundaries; permanently blocked.

Authority sources:
- `POLICY_AUTONOMOUS`
- `HUMAN_EXPLICIT`
- `NONE`

Human-critical categories requiring explicit human authorization include:
1. `MATERIAL_ROADMAP_OR_SCOPE_CHANGE`
2. `PRODUCTION_OR_CUSTOMER_MUTATION`
3. `DESTRUCTIVE_DATA_OPERATION`
4. `SECURITY_OR_AUTH_BOUNDARY_CHANGE`
5. `TRUST_MODEL_CHANGE`
6. `SECRET_OR_SENSITIVE_DATA_BOUNDARY_CHANGE`
7. `BILLING_OR_PAID_PROVIDER_ACTIVATION`
8. `AUTHORITATIVE_EVIDENCE_CONTRADICTION`
9. `EVIDENCE_WAIVER_OR_FORCE_PASS`
10. `RETRY_CEILING_EXCEEDED_WITH_NO_SAFE_REMEDIATION`
11. `LEGAL_OR_COMPLIANCE_AUTHORITY_AMBIGUITY`
12. `UNAPPROVED_HOST_PERMISSION_OR_TRUST_CONFIGURATION_CHANGE`

Ordinary implementation failures, test failures, CI failures, worker timeouts, capability reprobes within budget, and routine retries do NOT by themselves require human intervention. Planner `human_gate_required` setting is advisory only; deterministic `HumanGatePolicy` owns the authoritative decision. Planner cannot downgrade `HUMAN_REQUIRED` or force unnecessary gates. Independent verification remains required to close tasks, and contradictions trigger `HOLD`.



## ADR-AOS-V2-20261018 — Hybrid isolated lifecycle

Owner: AOS_CONTROLLER. User mandate: Astra Master Directive18Oct2026. Decision: HYBRID_V2; detailed alternatives, weights, safety, pivot and fallback in docs/aos-v2/04_ARCHITECTURE_DECISION.md. Audited base 9d3168afb35edf5ad06e6b5e419801493fe04572; foundation source 59bba904e32caa3fdd7335f33537529d9d3561ff. Preserve accepted AOS0–5 and separate LARI/AOS6 pilot HOLD. No live/production/startup/paid authority. One new V2 command owner, server grant, shared non-paid gateway, explicit goal acceptance and transactional closure; optional resource activation deferred. Operational E2E NOT_RUN. Next: WP2 after source review.

Public disclosure gate: automatic approval review rejected the attempted new public feature-branch push. Do not bypass via another tool/ref. Prepare private review delivery; await explicit owner approval for exact payload publication. No new candidate CI observed.


## AOS-V2 public-disclosure consent supersession — 18 October 2026

The owner explicitly approved publication of the prepared source/audit/directive/evidence payload only to `feature/aos-v2-thin-lifecycle-20261018` in public MertSGI/AOS. Receipt: `AOS-OWNER-PUBLIC-20261018-01`. This supersedes the missing-public-consent blocker, retains the earlier rejection history, and authorizes a normal new-branch push plus read-only exact-head CI verification. No main merge, visibility change, protected runtime/51/LARI mutation, paid usage, production or startup authority is created. Implementation remains source `59bba904e32caa3fdd7335f33537529d9d3561ff`; this governance-only delta does not prove operational E2E.


Publication execution outcome: public disclosure is OWNER_APPROVED. HTTPS writer credentials are absent and the connected GitHub integration returned HTTP403 on the first blob write. Zero source objects/branches were published; main remains the audited SHA and candidate CI remains NOT_RUN. Technical receipt `AOS-PUBLICATION-TECHNICAL-20261018-01`. Retain the exact Git bundle and normal-push transport command; do not reconstruct commits, broaden permissions, switch payloads or ask for public consent again. No architecture/WP reset is warranted by this transport blocker.


## AOS-V2-SEAM-1 — 19 October 2026 closed implementation contract

Owner AOS_CONTROLLER. Same HYBRID_V2/five-WP program, no audit restart. Source now published at7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506 via authenticated owner GitHub web commits;4source paths/bytes matched local reviewed correction. Original local history is preserved and not misrepresented as remote commit metadata. Four negative portable-scope/hard-link cases exposed one missing boundary; bounded correction passed14tests. Source/full carrier CI remains separately verified evidence.

The binding algorithm/type/security decisions are docs/aos-v2/14_IMPLEMENTATION_CONTRACT.md: explicit trusted registry/API, one kernel owner lock without TTL stealing, schema2 copy-first/fenced recovery, immutable AOS plan, unified bounded non-paid gateway, existing pure exact edits, mandatory real OS sandbox, commit-object/CAS/source-index reconciliation, independent criteria and idempotent isolated native KCP export. registry.py/sandbox.py and platform negative checks are necessary WP2 additions. WP2 estimate revised16–28h, no new phases or activation. Missing eligible provider/isolation/host evidence is a gate, never paid fallback or simulated real proof. Main/protected V1/51/LARI/production/startup/visibility rights remain absent.


WP1 source acceptance: exact source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506, full CI39204426295/job117904058408 SUCCESS, checkout/step/log evidence independently inspected. Accept the isolated storage/process/portable-scope foundation only. WP2 is READY under the closed AOS-V2-SEAM-1 contract. Operational E2E remains NOT_RUN; final carrier CI is a separate observation.


Concrete implementation defaults are closed: Python stdlib ThreadingHTTPServer for the loopback API; one Engine owner with serialized mutation methods and one dispatch loop; WP3 uses an authenticated CLI menu only, no new frontend framework or browser token bootstrap. State.implementation_constraints is the machine projection of AOS-V2-SEAM-1. Earlier optional page/CLI wording does not delegate a new UI architecture choice. Source/control carrier identity and CI are resolved from the actual containing Git commit plus external verified handoff; no self-referential SHA or future CI assertion.


Functional acceptance closure: AOS-VERIFIER-BOUNDARY-20261019-01 reproduces exit0/PASS spoofing before immutable assertions, in a disposable pre-admission module without AOS/provider calls. The same AOS-V2-SEAM-1 now mandates FUNCTION_ORACLE_V1: a pre-admission sealed private oracle, trusted versioned sandbox driver, fresh case nonces, primitive result frames and host-side comparison of every expected numeric result. Missing/forged/truncated/stale results fail independently of child exit/markers. The independent observer reruns that predicate, and immutable public tests remain supplementary required evidence. This closes an existing WP2/WP3 trust boundary; it neither adds a work package nor claims the unimplemented engine works.


## AOS-WP2-CORR-20261019-01 — focused WP2 Controller review

AOS_CONTROLLER reviewed actual remote7188d8995f09db00be17c6cb6207616c1b4dd7b2; the reported7188d89a2442c525f0a0d4c98a3c054cbfbfe497 does not exist. Exact-head V2 Contract39226672400 and AOS CI39226672477 succeeded but do not qualify the execution seam. Focused source comparison and genuine disposable HTTP/Git/SQLite/file counterexamples confirm F1-F9 in docs/aos-v2/evidence/wp2_review_20261019/review.json. Injected inference is SIMULATED, actual external model calls0, local nested-OS probe incomplete/unclassified and not OS-denial proof. WP1 acceptance is retained; WP2 is CORRECTION_REQUIRED and accepted_head remains null.

One closed correction instruction is docs/aos-v2/06_EXECUTOR_PACKETS/02_WP2_CORRECTION_20261019.md (A-D, mandatory Q01-Q20). It closes the existing interfaces/security/recovery/acceptance implementation choices without changing HYBRID_V2, AOS-V2-SEAM-1 or the five-WP DAG. Existing AG-AOS implements; AOS_CONTROLLER alone accepts exact source after independent source/artifact/CI review. Packet delivery via canonical repository is not an observed AG session invocation/acknowledgement. No corrected implementation has yet been received. WP3 waits WP2 acceptance, WP4 remains NOT_RUN, WP5 requires actual WP4 plus separate owner live approvals. No protected V1/51, user/legacy files, LARI/Codex, paid, production, startup, visibility or main-merge action is authorized.
