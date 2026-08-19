# AOS — Agent Operating System
## Master Project Charter & Control Plan

**Document version:** 0.1.0
**Date:** 2026-08-19
**Status:** FOUNDATION_ACCEPTED / IMPLEMENTATION_NOT_STARTED
**Working name:** AOS (Agent Operating System)
**First pilot project:** LARİ
**Primary objective:** Preserve the decision quality, memory, governance and evidence discipline developed between the human product owner and GPT-5.6 Sol, while removing the human from routine message transport and execution-loop orchestration.

---

## 1. Executive Summary

AOS is not an “AI that writes code automatically.” It is a reusable **development control system** that coordinates AI reasoning, deterministic policy enforcement, code-execution agents, verification, evidence, and human approvals.

The system must support this operating model:

```text
Approved Project State
        ↓
AOS Planner / Orchestrator
        ↓
Risk & Permission Gate
        ↓
Execution Worker (e.g. Antigravity)
        ↓
Deterministic Verification + Evidence
        ↓
AOS Reviewer
   ┌────┴─────┐
 PASS         HOLD
   ↓           ↓
NEXT TASK    HUMAN
```

The human product owner remains the authority for critical product, production, scope, legal, financial, security and irreversible decisions. Routine task transportation, retry loops, test/fix cycles and evidence packaging should become automated.

AOS is a **separate reusable platform**, not a LARİ-specific script. LARİ is the first proving ground.

---

## 2. Why This Exists

The existing manual workflow is effective but contains avoidable transport latency:

```text
GPT → Human → Antigravity → Human → GPT → Human → Antigravity
```

The most valuable component is not the transport. It is the accumulated operating discipline:

- accepted roadmap authority,
- scope discipline,
- fail-closed behavior,
- exact-SHA verification,
- evidence hierarchy,
- contradiction handling,
- explicit HOLD states,
- human gates,
- separation of implementation claims from empirical proof,
- immutable/traceable decisions.

AOS converts these principles from conversational memory into **versioned, machine-readable project governance**.

---

## 3. Non-Negotiable Design Principles

### P-001 — Canonical truth must live outside conversational memory
Chat history and AI memory may assist reasoning but are not canonical project state. Accepted state, decisions, evidence and policies must exist in versioned project-control files.

### P-002 — AI may execute approved plans; AI may not silently redefine them
Agents may decompose and execute work within an approved roadmap. New canonical phases, scope expansions or irreversible product decisions require an explicit decision record and, where policy requires, human approval.

### P-003 — Evidence outranks claims
An agent statement such as “tested successfully” is not sufficient proof. A gate closes only when the required evidence class is actually present and linked to the exact tested revision/environment.

### P-004 — Exact revision identity is mandatory
Builds, tests, runtime evidence and closure decisions must identify the exact product commit SHA or immutable revision under test.

### P-005 — Fail closed
If the system cannot establish that an operation is allowed, safe and sufficiently evidenced, it must not proceed. The default uncertain state is HOLD, not GO.

### P-006 — Production authority remains human-gated by default
Production mutation, payment activation, customer-impacting destructive actions, sensitive-data operations and equivalent high-risk actions are never inferred from ordinary roadmap execution.

### P-007 — Contradictory evidence reopens truth
Previously accepted state is not protected from empirical contradiction. If new authoritative evidence conflicts with an accepted claim, AOS must stop, record the conflict and apply the project’s reopen policy.

### P-008 — Deterministic controls before model judgment
Schema validation, branch ownership, path allowlists, test results, migration checks, locks, permissions and exact-SHA comparison should be enforced by code wherever possible. Models reason about what cannot be safely reduced to deterministic checks.

### P-009 — Separate planner, executor and verifier responsibilities
The component deciding “what should happen” should not be the only component deciding “whether it worked.”

### P-010 — Portability is a first-class requirement
AOS must support:
- multiple projects,
- multiple PCs/workers,
- Windows/macOS/Linux where underlying tools support them,
- independent worker identities,
- branch/worktree isolation,
- a repeatable bootstrap procedure.

### P-011 — Temporary coordination is not canonical project history
Worker leases, transient queues and heartbeats are operational state. Canonical decisions and evidence live in version control.

### P-012 — AOS must not reset an existing product roadmap
Adopting AOS must not retroactively alter a product’s accepted delivery sequence, baseline, gate state or delivery-date policy merely to accommodate the automation project.

---

## 4. Roles and Authority

### Human Product Owner
Owns:
- product strategy,
- commercial commitments,
- critical architecture exceptions,
- scope expansion,
- production launch,
- irreversible/destructive approval,
- final decision on ambiguous high-risk conflicts.

### AOS Brain / Planner
Recommended primary reasoning engine: GPT-5.6 Sol via OpenAI Responses API for high-consequence planning/review.

Responsibilities:
- read canonical state,
- determine the next allowed objective,
- generate bounded execution specifications,
- assess semantic evidence,
- recommend PASS/HOLD,
- explain escalation clearly.

It does **not** independently grant permissions that policy reserves to humans.

### AOS Controller
Deterministic orchestration service.

Responsibilities:
- state loading,
- schema checks,
- task selection,
- lease acquisition,
- worker dispatch,
- retry ceilings,
- branch/worktree rules,
- evidence collection routing,
- escalation triggers.

### Execution Worker
Initial adapter: Google Antigravity CLI / agent environment.

Responsibilities:
- inspect code,
- implement bounded tasks,
- run approved commands/tools,
- produce raw artifacts,
- report exact changes.

It is not the canonical roadmap authority.

### Verifier
Combination of deterministic checks and an independent semantic reviewer.

Responsibilities:
- changed-file scope,
- test/build status,
- exact-SHA match,
- browser/runtime evidence validity,
- evidence-class requirements,
- regression checks,
- claim/evidence consistency.

---

## 5. Human Required Conditions

The controller must transition to `HUMAN_REQUIRED` or `HOLD` when any of the following occurs:

1. Canonical roadmap or milestone sequence must change.
2. Product scope materially expands or contracts.
3. Production deployment/mutation is requested.
4. Destructive or irreversible database/data operation is required.
5. Real customer PII/sensitive information enters the execution path outside an already approved policy.
6. Authentication/security trust boundaries materially change.
7. Billing/payment/subscription activation changes.
8. Secrets/API-key ownership or trust model changes.
9. New evidence contradicts accepted evidence/state.
10. An executor needs to modify files outside its permitted task scope.
11. Required evidence cannot be produced.
12. Retry ceiling is exceeded.
13. A verifier cannot determine PASS with the configured confidence/evidence policy.
14. Two authoritative sources conflict.
15. Legal/compliance/contractual interpretation is necessary.
16. The next action is not derivable from the approved roadmap without inventing requirements.

Default response after escalation:
- stop mutations,
- preserve raw evidence,
- explain expected vs observed,
- state the exact decision required,
- offer bounded options where possible.

---

## 6. Memory Model

AOS memory is deliberately layered.

### Layer A — Global Principles
Reusable across every product.

Examples:
- fail closed,
- evidence before closure,
- do not fabricate runtime/browser proof,
- do not infer production permission.

Storage:
`aos-core/governance/`

### Layer B — Project Charter
Product-specific stable truths.

Examples for LARİ:
- product architecture,
- master delivery train,
- commercial modes,
- package relationship,
- brand/domain rules.

Storage:
`<project>/.aos/PROJECT_CHARTER.md`

### Layer C — Decision Log
Append-oriented record of accepted/superseded decisions.

Storage:
`<project>/.aos/DECISIONS.jsonl` and a human-readable projection.

### Layer D — Current State
Small, machine-readable, frequently updated state.

Storage:
`<project>/.aos/STATE.json`

### Layer E — Evidence
Immutable evidence records referencing exact revisions and artifacts.

Storage:
`<project>/.aos/EVIDENCE.jsonl`

### Layer F — Lessons
Reusable lessons promoted from a project only after review.

Example:
“Do not accept simulated browser execution as isolated runtime E2E evidence.”

Storage:
- project lesson first,
- explicit promotion to `aos-core/lessons/` if globally applicable.

---

## 7. Canonical vs Operational Data

### Canonical — Git/GitHub
- policies,
- roadmap,
- accepted decisions,
- project charter,
- state transitions,
- evidence records,
- schemas,
- release/baseline identity.

### Operational — Coordination Backend
- task lease,
- worker heartbeat,
- transient retry count,
- temporary queue position,
- worker availability.

Initial recommendation:
- single-machine mode: SQLite/local state,
- multi-machine mode: small Postgres/Supabase coordination adapter,
- canonical results always committed back to Git.

The coordination database must be disposable without losing project truth.

---

## 8. Distributed Worker Model

Each worker has a stable ID, for example:

```text
mert-main-pc
mert-ui-pc
cloud-worker-01
```

Every executable task receives:
- `project_id`
- `task_id`
- `allowed_scope`
- `base_sha`
- `worker_id`
- `lease_id`
- `lease_expires_at`
- `branch_name`
- `evidence_requirements`
- `risk_class`

Rule:
**One active lease per task. One isolated branch/worktree per worker task.**

If a worker disappears:
1. heartbeat expires,
2. lease becomes reclaimable,
3. previous work remains preserved,
4. the next worker must inspect the abandoned branch before reuse or replacement.

No worker may silently continue against a base SHA that has become stale when policy requires exact-baseline execution.

---

## 9. Risk Classes

### R0 — Read-only
Examples:
- inspect repository,
- analyze code,
- compare state,
- prepare plan.

Can auto-run.

### R1 — Isolated low-risk implementation
Examples:
- implementation on isolated worktree,
- unit tests,
- documentation,
- non-production UI work.

Can auto-run after policy checks.

### R2 — Integration-sensitive
Examples:
- schema migrations in disposable test environment,
- shared contract changes,
- cross-layer refactors.

Requires enhanced verifier and may require approval depending on project policy.

### R3 — Customer/production sensitive
Examples:
- production data,
- auth/security boundary,
- payment configuration,
- irreversible migration.

Human approval required.

### R4 — Forbidden by default
Anything outside the explicit authority model or unable to satisfy safety/evidence constraints.

Must not execute.

---

## 10. Evidence Model

AOS adopts the LARİ evidence-first philosophy and generalizes it.

Suggested generic levels:

- **E0 — Claim only:** narrative statement; never closes a technical gate.
- **E1 — Source/static proof:** code/config inspection.
- **E2 — Executable exact-revision proof:** deterministic tests/CI against exact revision.
- **E3 — Isolated runtime proof:** real application/runtime execution in an isolated environment.
- **E4 — Shared staging/live-like proof:** verified behavior in the defined shared staging environment.
- **E5 — Production observation:** production evidence; does not imply permission to mutate production.

Each project defines the minimum level needed for each gate.

Evidence record minimum:
- evidence ID,
- project,
- gate/task,
- exact SHA,
- environment,
- method,
- result,
- artifact/run reference,
- timestamp,
- producer,
- verifier,
- limitations.

---

## 11. Retry Policy

Automatic retry is bounded.

Default:
- implementation-fix retry: max 2 autonomous correction cycles,
- infrastructure/transient retry: configurable separately,
- repeated semantic failure: HOLD,
- evidence contradiction: no blind retry; HOLD and diagnose.

Retries must never silently change scope to “make tests pass.”

---

## 12. Project Integration Contract

A product opts into AOS by adding a minimal project descriptor, for example:

```yaml
schema_version: "0.1"
project_id: "lari"
repository: "MertSGI/Randapp-main"

control:
  state: "docs/project-control/STATE.json"
  decisions: "docs/project-control/DECISIONS.md"
  evidence: "docs/project-control/EVIDENCE.jsonl"
  roadmap: "docs/project-control/ROADMAP_12W.md"

authority:
  production_mutation: human_required
  roadmap_change: human_required
  destructive_data: human_required

workers:
  branch_prefix: "aos/"
  isolated_worktree_required: true
```

Existing project-control structures should be adapted, not rewritten unnecessarily.

---

## 13. LARİ Integration Guardrails

Verified LARİ control-plane state at AOS foundation:

- LARİ Core: `FROZEN / CLOSED_PROVEN`
- Current main delivery focus: `Package / Customer Customization`
- UI V2: parallel, non-blocking frontend lane
- Core RC baseline: `release/core-rc4`
- accepted Core RC SHA: `e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a`
- CORE-RC.4: `CLOSED_PROVEN`
- AOS adoption must not reset the existing 12-week delivery policy.
- The LARİ roadmap sequence remains:
  `LARİ CORE -> PACKAGE/CUSTOMER CUSTOMIZATION -> LARİ CLINIC -> LARİ HEALTH TOURISM -> FINAL DELIVERY`

AOS is initially a **parallel control-plane engineering lane**. It may observe LARİ before it is allowed to execute LARİ tasks.

---

## 14. Definition of Done for AOS V1

AOS V1 is not complete merely because an agent can run unattended.

V1 requires:

1. A project can be bootstrapped from a documented config.
2. Planner reads canonical state and selects only approved work.
3. Worker task has a bounded scope and exact base SHA.
4. Antigravity adapter can execute a bounded task non-interactively or through supported automation.
5. Deterministic verifier runs required checks.
6. Evidence is structured and tied to exact revision.
7. PASS closes only an allowed gate/task.
8. Contradiction causes HOLD.
9. Human-required events stop further mutations.
10. Two machines cannot legitimately own the same task simultaneously.
11. A second PC can join with a documented bootstrap procedure.
12. Secrets remain outside source control.
13. Worker failure is recoverable.
14. LARİ controlled pilot proves at least:
    - one read-only/shadow decision,
    - one isolated implementation task,
    - one verifier rejection/HOLD test,
    - one successful evidence-backed closure.
15. A second independent repository can be onboarded without copying LARİ-specific logic into AOS core.

---

## 15. Success Metrics

Track from first real pilot:

### Reliability
- false PASS count,
- false HOLD count,
- evidence mismatch count,
- stale-SHA execution count,
- duplicate task claim count.

Targets for V1:
- false PASS: 0 accepted incidents,
- stale-SHA closure: 0,
- duplicate active ownership: 0.

### Efficiency
- human transport messages eliminated,
- median autonomous task cycle time,
- retries per closed task,
- human minutes per task,
- cost per closed task,
- overnight completed tasks,
- parallel throughput.

### Portability
- time to onboard a new PC,
- time to onboard a new project,
- project-specific code added to AOS core (target: near zero).

---

## 16. Cost Philosophy

Use expensive reasoning only where it adds value.

Recommended:
- GPT-5.6 Sol: critical planning, semantic verification, ambiguous evidence, HOLD explanation.
- deterministic code: validation, locking, exact-SHA checks, status collection.
- lower-cost model may be evaluated later for routine classification/summarization, but only after behavior is benchmarked against the Sol baseline.

Every model call should be metered:
- input tokens,
- cached tokens,
- output tokens,
- model,
- task,
- purpose,
- cost estimate.

No cost optimization is allowed to silently lower safety or evidence standards.

---

## 17. Security Baseline

- Secrets never committed.
- Principle of least privilege.
- Separate production credentials from development credentials.
- Workers receive only required repository/project permissions.
- Production access disabled by default.
- Logs must redact secrets.
- Prompt/output logs containing sensitive data follow project retention policy.
- External tools are adapters; no external tool becomes canonical authority by itself.

---

## 18. Source/Capability Assumptions Verified at Foundation Time

As of 2026-08-19, official documentation confirms relevant capabilities used in this plan:

- Google Antigravity provides a CLI intended for terminal/headless workflows.
- Antigravity supports Projects and isolated Git worktree mode.
- Antigravity supports scheduled tasks.
- Antigravity supports concurrent subagents and isolated worktree options.
- OpenAI GPT-5.6 Sol is available through the Responses API and supports structured/tool-based workflows.

These capabilities are implementation dependencies, not governance authority.

Official references:
- https://www.antigravity.google/docs/cli-overview
- https://www.antigravity.google/docs/features
- https://antigravity.google/docs/subagents
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/guides/latest-model

---

## 19. Change Control

This charter may be amended only through a recorded decision.

Changes are classified:

- **Editorial:** clarification without semantic change.
- **Operational:** implementation detail that preserves principles.
- **Architectural:** changes AOS component boundaries or trust model.
- **Governance:** changes authority, evidence, human gates or safety behavior.

Architectural and governance changes require explicit review. Governance must never drift through implementation convenience.

---

## 20. Foundation Statement

AOS exists to make autonomy **more controlled, more observable and more reusable**, not less accountable.

The target end state is:

> The human stops transporting instructions.
> AI and deterministic systems execute the accepted plan.
> Evidence decides technical closure.
> The human remains the authority for decisions that should remain human.
