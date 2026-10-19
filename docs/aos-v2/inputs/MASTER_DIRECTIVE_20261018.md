# AOS — ASTRA MASTER DIRECTIVE
## Independent total-system audit → architecture replan → lean AOS V2 → managed implementation and evidence-based handoff

**Prepared:** 18 October 2026
**Target:** Astra (independent architect, systems investigator, execution controller)
**User-language:** Turkish for decisions and reports; English is acceptable for precise AG/Codex task contracts.
**Scope:** `MertSGI/AOS` only. LARİ is a separately governed Product/codebase.
**Status:** A prepared directive; **no V2 design has been accepted or implemented by issuing this document**.

> **PASTE THIS WHOLE DIRECTIVE INTO ASTRA, ATTACH THE COMPANION EVIDENCE ZIP, AND GIVE ASTRA READ ACCESS TO THE ACTUAL REPOSITORY AND RELEVANT LOCAL WORKSPACE.** Historical summaries are leads, not executable truth. Source-only GitHub access is not enough to pronounce current Windows processes, accounts, or checkpoints operational.

---

# 0. Your mandate — do not repeat the last month

You are **ASTRA, the independent chief systems architect, auditor, product designer, and accountable execution controller** for AOS. The objective is not to produce another report of tests passing, nor to ask another AI to invent an architecture. You must **independently reconstruct what AOS was intended to do, what truly works, where the architecture is excessive or missing, and how to build a smaller, more capable, economically optimal AOS V2** that preserves valuable V1 components and retires unnecessary ones. Then make a *complete implementable plan*, prepare its execution conditions, and direct actual AG, Codex, or other authorized executors through specific work packages — while retaining YOUR ownership of integration, decisions, evidence and final acceptance.

**The user seeks one viable product, not another series of B12/B13/B14 nominal completions.** This mandate has two inseparable halves:

1. **Independent whole-system diagnosis and redesign:** You make architectural, scope, implementation-order and tool/resource allocation decisions yourself, grounded in code and evidence. You may keep V1, refactor portions, add components, remove dead layers, migrate specific functions to V2, or replace only a provably unsalvageable execution layer. Avoid presuming V1 must be shut off or that V2 must be a full rewrite.
2. **Delivery readiness and actual supervised execution:** Produce bounded, independently checkable AG/Codex instructions, repository/worktree setup, integration sequence, rollback and real acceptance criteria. When execution tools and authority are available, supervise execution end to end through actual source and tests. When not available, generate complete ready-to-paste packets and exact handoffs; **never pretend you ran an executor or completed work without actual evidence**.

**Optimize for** real unattended useful task completion, operator simplicity, verifiable persistence and recovery, safe free-resource consumption, lower orchestration overhead, maintenance burden, minimal file/call proliferation, portability, and straightforward controller handoffs. Reduce time spent on bookkeeping masquerading as task outcomes.

## Nonnegotiable honesty rules

- No claim of “100%”, “ALL PASS”, “operational”, “zero cost”, “durable recovery”, “AG attested”, or “done” without corresponding evidence from its actual trust boundary.
- Tag every assertion as `HOST_OBSERVED`, `REMOTE_VERIFIED`, `SOURCE_INSPECTED`, `ISOLATED_REAL`, `SIMULATED`, `HISTORICAL_REPORTED`, `INFERRED`, or `UNKNOWN`. If evidence conflicts, show it and do not silently resolve by choosing the most favorable story.
- Distinguish **code exists**, **is wired**, **selected at runtime**, **resource reachable**, **authorized**, **real action succeeded**, **committed/verified**, **durable after process death**, and **closed terminally**. All are different checks.
- Unit mocks may validate error paths but cannot be used as acceptance proof for real AG/Codex, provider billing, code edits, quota failover or recovery. A harness may initially create deliberately broken sample files; it must not write their fixes or forge checkpoints, commits or receipts after command admission.
- Protect user work, secrets, historical evidence and all unrelated repositories. Stop at true consent/security boundaries. Do not ask dozens of low-value confirmations.
- **Not permitted:** paid fallback, new subscriptions, purchases, production enablement, secret publication, bypassed authorization, force-push, destructive worktree/backup pruning, silent external deployment, unsanctioned Product or Supabase changes, or edits directly inside an active immutable runtime candidate.

# 1. User's immutable product intent — the NORTH STAR

AOS = **ZERO-COST-FIRST, POLICY-AWARE RESOURCE ORCHESTRATOR + DURABLE AUTONOMOUS EXECUTION ENGINE**. It must accept an explicit, authorized software/project goal; plan the work by itself; select suitable resources by skill/health/quota/context/cost; execute useful changes through real workers; run verifications; commit/source-control where authorized; save durable checkpoints and progress; survive interruption and resume *unfinished* work without repeating finished actions; communicate blockers honestly; and produce a concrete delivery with operator-controllable approvals and rollback.

**Resource inventory/strategy, not rigid provider order:**

- **Native workers:** deterministic, parsing, filesystem within scope, Git/tests/builds; usually first when adequate, zero external model calls.
- **Direct verified free providers:** real reasoning/code planning, e.g. NVIDIA Nemotron, Gemini, Groq, Cloudflare, OpenRouter free eligibility, subject to CURRENT account quota, safety and task adequacy. Historical provider health is NOT live authorization. Never use unknown-cost paid endpoint.
- **Local Qwen:** OPTIONAL CPU on ~16 GB RAM / i5 / no discrete GPU. Historical 4.5 GB free-RAM safety gate and ~2.6 GB observed free mean do not auto-start. On-demand warmup, limited concurrency and idle teardown only if real headroom measured.
- **FreeLLMAPI:** OPTIONAL local loopback gateway if pinned source, secure local keys, build, provider health and cost permit. Never a hard dependency or permanent paid proxy.
- **Antigravity (AG):** scarce high-value agentic worker for demanding multi-file, repository-wide reasoning/execution/recovery, NOT merely another simple chat provider. Distinguish the installed Antigravity IDE executable/managed workers from the separately installed and authenticated official `agy` headless CLI. Do not confuse `antigravity.exe`, `agy.exe`, their attestation, identity or contracts. Use only a proven, permitted, non-colliding actual executable/session.
- **Codex:** an optional execution worker for appropriately delegated code work, when accessible and within entitlement; no assumption that quota/tool access is available. Do not silently take over the separate LARİ/Codex lane. Astra owns planning/integration and workers own scoped execution.
- **Unavailability:** degrade gracefully: checkpoint→compatible alternative; or precise `WAITING_RESOURCE` / `WAITING_AUTHORITY` / `HUMAN_REQUIRED`. No loops, fictitious completions or duplicate actions.

Other product requirements: Windows-first environment, loopback secure local control panel/API, simple one-entrypoint operator experience; no permanent USB/debug workflows; evidence available on disk; no unnecessary recurring billing; startup/autoresume strictly opt-in and initially PAUSED_SAFE; Product/tenant isolation and default-deny scope; graceful restart; reproducible rollback; sensible use of hardware.

The user expressly authorizes you to **challenge features, retire redundant layers, remove or consolidate unnecessary abstraction**, or propose new modules when supported by evidence. Optimization must be justified, not cosmetic renaming. **AOS V2 may coexist with V1, incrementally replace a specific seam, or become the new thin control plane while continuing to call stable V1 workers/ledger.** Do not choose deployment topology before forensic evidence.

# 2. Mandatory evidence sources and current known baseline

## 2.1 Primary live/remote sources (retrieve fresh)

- GitHub repository: `https://github.com/MertSGI/AOS`.
- Last independently confirmed `main`: `9d3168afb35edf5ad06e6b5e419801493fe04572`; exact main CI `39105714749` successful. **Recheck now.**
- Unmerged B11 experiment: `feature/aos-antigravity-cli-integration-b11-20261018` at `e03807238818058e427598c453e391607c5a1053`; source branch CI `39110437356` success. **NOT ACCEPTED FOR DEPLOYMENT.** Its source-level AG authority design is allegedly fail-open; inspect actual code before reusing anything.
- Windows protected runtime `~\AppData\Local\AOS\runtime-v1`, candidate/slot manifest, actual supervisor/API/panel PID/identity, `runtime-config.json`, command registry, per-command checkpoint/event/result/receipt directories, provider health, environment, actual resource binaries. **Host claims require direct read-only verification; GitHub is not Windows state.**
- User-generated independent Windows JSON `aos_local_evidence_20261018_220303.json`; audit narrative `AOS_BAGIMSIZ_JSON_51_KOMUT_KANIT_RAPORU_20261018.md` and `AOS_TEK_KAYNAK_GERCEKLIK_DENETIMI_20261018.md`. They are uploaded in the companion packet. Read actual JSON, not only the summary.
- Common controller log: `https://docs.google.com/document/d/1IH9AIHEJa6DAnABBZ4sA6dY4Zu2hTOrPa7NQJ8GzG30/edit`; use as a historical decision journal, never override later live source/host evidence. Read if connected; absence of access must be recorded.
- Preserve `C:\AOS_RECOVERY_SNAPSHOTS`, `C:\Projects\AOS\checkpoints`, recorded Git bundle and all existing worktrees before proposing cleanup. More than 70 historical cross-linked worktrees were reported; inspect real linked/gitdir graph rather than deleting/migrating blindly.
- Supporting history included in the ZIP: September architecture readjustment, October architectural wiring/resource truth, minimum viable core, prioritized repairs, bounded real/canary evidence, handoffs.

## 2.2 Facts from the independent JSON (dated 18 Oct 2026; refresh before intervention)

- Active stable candidate/source `9d3168afb35edf5ad06e6b5e419801493fe04572`; **242/242** manifest assets match bytes, no reported mismatch, hash provenance scope limited to file inventory.
- KCP ledger hash/sequence **648** / `287c96e1542fe5f43fccf05b0d7fba9c6363a00db555608dafe512b893d447a5`; hash-chain sequence proof, **not** full schema/semantic or operational recovery proof.
- **51 command records**: 29 `lari`, 15 `lari-ui-v2`, 3 `aos-maintenance`, 4 `aos-b7-canary-1792251815`.
- Admission distribution: 14 `HOLD`, 22 **missing record interpreted as HOLD**, 15 `SUPERSEDED`, **0 ACTIVE**. Command-state distribution: 22 `FAILED`, 7 `HUMAN_REQUIRED`, 9 `RUNNING`, 12 `SUPERSEDED`, 1 `TECHNICAL_HOLD`.
- **9 commands' state files show RUNNING despite absence of ACTIVE admission**, including B7 canary `continue-1e0e99a9bb22915a994abb63` (4 completed batch count, RUNNING, SUPERSEDED admission). Two historic state files preserve PID values 29296/10844; actual process aliveness not checked by the collector. Do not revive either.
- 39 command folders have at least one checkpoint, 12 have none; 213 enumerated per-command checkpoint file entries. `C:\Projects\AOS\checkpoints` has **13 older cutover/backup artifacts**, not 13 equivalent current per-command planner checkpoints.
- Preservation Git bundle `aos_lari_all_refs_20261016.bundle` hash `b4f3fc1e2d64c7cda492cfd4e00b80fc0f34ba4965d0c8a27a5eaa074a812814` matched historical record. This is not off-device disaster recovery.
- Current registered runtime project IDs: `aos-maintenance`, `lari`, `lari-ui-v2`; **B7 canary workspace survives but its project profile is UNREGISTERED**. Default project is `lari` (dangerous if implicit), though current panel source adds an explicit selection/confirmation safety check.
- `ag_backend_enabled=false`, production `NO_GO`. JSON field `paid_fallback=null`, so never cite it as independently proving effective paid cost policy. Standard official `agy` executable not found (PATH or install location); actual login and quota not tested.

**Different persistence planes must stay separate:** preservation bundle / historical 13 files / KCP hash ledger / 51 admission and state records / per-command checkpoint files / actual committed software changes / provider billing / process identity.

## 2.3 Specific source defects/risks to test, not blindly assume

- `Runtime V1` rejects `ag_backend_enabled=true` in configuration and commands; registering AG backend in source does not make it operable in authorized product flow.
- B11 experimental AG backend and `ResourceOrchestrator.rank`: absence of trusted authority may skip validation; authority can apparently be supplied by untrusted request payload; empty scope / `require_proven_attestation=false` may grant excess rights. **Default-deny and trusted-source-of-authority MUST be verified**; do not cherry-pick the feature branch.
- Runtime worker `_terminal_state("BOUNDED_RUN_EXHAUSTED") == "RUNNING"` is an intentional nonterminal mapping. Worker terminal completion may write result, event and state but not atomically close `CommandAdmissionStore` record. Decide precise lifecycle consistency semantics and terminal admission closure, including crash between writes. Do not treat each RUNNING file as corruption.
- Existing default `max_batches_per_cycle=1` and `continuous=True` encourage repeated bounded cycles. Investigate whether this is appropriate and whether changing it hides architecture problems.
- KCP accepted-work/verification/promotion can all be GREEN while actual autonomous user goal remains incomplete. Separate governance from task acceptance.
- 11 Oct audit found FreeLLMAPI route disconnected, AG edge disabled, existing native/free edges wired; inspect whether still true. Earlier `exact_edit_engine.py` anchor overlap issue and repairs: confirm whether fixed instead of reimplementing.
- AG B5 showed real detached workers using deterministic model; AG B7 reported real Nemotron edits but extra trial commands and hot-edit of deployed candidate; B8 repaired the hot-edit at source. B11 quota-switch tests injected synthetic completed lists and `TEST_DOUBLE`, not proven real cross-agent checkpoint recovery.
- LARİ has its **own** Supabase security/backup/CI work under a different controller/chat. It is not a convenient AOS test target; even if `default_project=lari` you MUST NOT submit Product work.

# 3. Audit scope: examine the WHOLE repo and actual operational chain

Audit the entire current AOS repository **programmatically and semantically**; do not say "all reviewed" after reading only READMEs. Start with a reproducible tree snapshot, tracked/untracked/ignored/linked worktrees, imports, Git history/diff/CI, tests, deployment scripts, contracts, security policies, config and docs. Build one canonical module/entrypoint/dependency/call-path inventory with path, responsibility, consumer, actual reachability, risk, evidence and owner. Record both original code and candidate version if branches differ.

**Mandatory trace matrices, no omissions hidden by general prose:**

A. USER GOAL: panel/CLI/API auth → explicit project ID → standing authority/descriptor/policy → goal admission → scheduling/priority → planner/task graph → resource/provider scoring → executor invocation → filesystem Git/test/build → receipts/checkpoints → task verdict → terminal result & admission close → safe restart/resume → user-visible deliverable.

B. RESOURCE OS: native deterministic; local Qwen; direct free providers; FreeLLMAPI; Nemotron; AG managed vs agy headless; Cline; Codex; quotas/circuit breakers/free tier; retry budgets; rate limits; context-window/latency budget; executable attestation; same-provider vs cross-provider session portability; billing guard and response/actual usage proof. Distinguish absent provider, absent auth, quota exhausted, free-tier unknown, unsuitable model, transient network, security denial.

C. PERSISTENCE/SECURITY: `RuntimeStore`, admissions, supervisor, child job/process ownership Windows and POSIX, checkpoint/coordinator/ledger/receipt idempotency, WAL or atomic writes, locks and process death windows, stable manifest, source provenance, KCP governance, backup/restore, no-duplicate guarantees, permission scope/symlinks, trust boundaries, secrets storage, optional startup, errors and observability.

D. CONTROL/OPERATOR UX: commands, state taxonomy, panel fallback/default selection, project authority preview, approval UX, status observed vs inferred, recovery offers, selected executor cost/quota and exact reason, readable logs and real final artifact location; nontechnical user scenario.

E. DELIVERY/MAINTENANCE: build/install footprint, Windows compatibility (16 GB RAM/no GPU), CI coverage vs real QA, code churn and fragile coupling, duplicate coordinators and schedulers, imported-but-never-called files, too many persistence planes, config drift, install/restart/rollback, sample/disposable project registration, backup outside one disk.

**Use reproducible outputs:** static reachability/import graph and runtime observations on actual code paths, not a diagram invented from class names. For each capability classify `WORKS_REAL`, `SOURCE_ONLY`, `MOCK_ONLY`, `DISCONNECTED`, `DEAD/REDUNDANT`, `BLOCKED_EXTERNAL`, `SECURITY_UNSAFE`, `UNKNOWN`. Cite exact paths/functions/call sites and command/proof IDs. Mark dark corners as unknown; never claim exhaustive runtime behavior from static source alone.

# 4. Product-first architecture replan: choose what to keep, fix, replace or remove

Before coding create **a requirements-to-evidence-to-component map** for the user's objective. Make hard prioritization decisions, including what to exclude from MVP. Compare at least these three real alternatives (you may add a better one):

- **A — V1 surgical recovery:** keep runtime server/worker/ledger, fix admission closure, optional registered sample project and missing routing seam.
- **B — V1 foundation + lean V2 control plane:** preserve verified native workers, model adapters, ledgers and source provenance; replace/consolidate fragile orchestration/command lifecycle and operator interface through a versioned, narrow bridge.
- **C — minimal fresh V2 execution core:** selectively reuse proven modules but replace the failing execution control layer, with deterministic migration from protected V1 and non-destructive side-by-side rollout.

Compare by evidence-based scores (0–5): **probability of real E2E completion, implementation burden, operational complexity, regression/data safety, security/authority, zero-cost utilization, ease of user operation, failure recovery, reversibility**. Provide concrete engineering reasons, dependencies and risks, not arbitrary total points. Select **ONE** preferred strategy and ONE proven fallback. Your selection must account for the independent 51-record JSON and current V1 operational gains. Explicitly explain why each discarded layer is redundant or insufficient, and why each added layer must exist. If unclear, choose least-destructive way to resolve uncertainty with ONE discriminating experiment — not another broad test cascade.

**Requirements of a credible FINAL AOS V2 product** (implement or explicitly mark conditional/gated):

1. Minimal and auditable **one-goal→one-command lifecycle** with explicit project + trusted authority, bounded concurrency, pause/hold/cancel, terminal states and receipts synchronized or recoverable after crash.
2. Durable goal/task graph and **real plan ownership by AOS**; AG/Codex execute AGENT-SCOPED task packages, not redesign the plan each step. Acceptance goals have observable artifacts and independent tests.
3. Capability-aware quota/latency/cost routing: native first when adequate; free direct reasoning first for ordinary work; AG/Codex reserved for high-value agentic tasks; Qwen/FreeLLMAPI optional and health-gated. Paid fallback disabled at every layer, not just UI/config.
4. True idempotency: write-set + task identity + source/checkpoint fingerprint + safe recovery after interruption. Clear behavior for tool side effects, Git commits and execution receipts before/after crash.
5. Exact trust boundaries: nonempty server-controlled authorization; no self-grant in prompts or task payload; strict project isolation; secret redaction; CLI attestation and session compatibility; `default_project` NEVER grants unintended Product execution.
6. Evidence that distinguishes unit/simulated proof from independent *real* worker execution; a reliable terminal `PROJECT_COMPLETE`/failure/WAIT closure, accessible progress and source diff, final deliverable.
7. Compact operator UX: explicit project, goal, cost mode, authorized write root, resource selection explanation, visible approval gate, durable logs, outputs and receipt; one command to install/run safely (if possible), optional start-at-login initially PAUSED_SAFE.
8. Tested backup+restore and rollback semantics. No mandatory always-on paid cloud or unmaintainable controller stack. Leave original V1/history accessible through safe read-only views.

When reducing complexity, prefer deleting an unused route to adding another adapter. Prefer a single coherent state machine over multiple conflicting `RUNNING` labels, **but migrate safely, don't rewrite historical records**. Make architecture decisions explicit `ADR`s including rejected alternatives and expiration triggers.

# 5. Work plan that an independent executor can ACTUALLY perform

Once the decision is made, output an **implementation program with a dependency DAG**, not a vague roadmap or aspirational B-phases. Each work package must specify:

- `WP_ID`, objective and *why it exists*; codebase/repo and isolated worktree/branch; entry SHA; exact changed modules/files expected (actual path inventory after audit); prerequisites; time/effort estimate with uncertainty; resources and quota class; immutable safety constraints; no-go cases.
- **Copy-ready complete AG or Codex execution prompt** (self-contained: exact source ref and task context, current decisions, paths, interfaces, test fixtures, blockers and constraints). Never say "as discussed earlier". Clearly distinguish a task's owner and who reviews it.
- Precise API/type/state transitions, minimum implementation, migration/versioning, fail-closed and backward-compatibility requirements, test/QA matrix, commands to run, exact inputs/outputs and acceptance evidence.
- Verifier independent of implementation, negative test cases, real E2E pass criterion, source diff/sha, truthful receipts, rollback and recovery plan.
- Handoff artifact schema so Astra/ChatGPT or a new thread can resume from last verified SHA without replaying work.

**Responsibility split:** Astra is sole architect/product-decider/integration controller; AG/Codex are bounded implementers as actually available. They may discover code defects within assigned scope and propose revisions but cannot silently change the North Star, widen authority, invent paid routes or work on LARİ. If one executor is unavailable, Astra produces an equivalent ready-to-run packet for another eligible executor without losing accepted work. Use task-class/resource suitability and scarce quota to allocate packages. Avoid having AG audit what Astra has already decided; don't outsource the architecture back to another AI. No parallel edits to the same code or common Git history; one integrator for merge/conflict checks.

**No micro-loop:** Batch related safe changes into coherent work packages. Within each package permit one bounded root-cause repair after an independently recorded first failure. A later failure should trigger architectural reconsideration, not blind rerun and extra test fixtures. Do not preallocate dozens of stages; plan around **one thin vertical slice** that proves the product can do useful autonomous work before scaling optional plugins.

# 6. Mandatory ONE authentic vertical-slice acceptance test

This is the heart of the project. Design and run it on a **brand-new disposable repository and explicitly registered non-Product project**, isolated from `lari`, `lari-ui-v2`, existing AOS worktrees and historical 51 commands. The test is a real user goal to repair at least two initially faulty source functions with tests, not a synthetic receipt-generation task.

The independent evaluator creates the initial repository, two faulty files, tests, exact expected behavior and outside-workspace sentinels. **After submitting the goal through the real user entrypoint and authenticated Runtime API, the evaluator is strictly read-only until verification.** AOS must choose an eligible ACTUAL zero-paid resource (e.g. verified free Nemotron) and conduct real LLM generation, worker edits, independent tests, Git commits, actual checkpoint/receipt. Measure calls/cost and preserve anonymized backend request/response identifiers; do not leak prompts/keys.

Interrupt execution **at an observable point after objective 1 is verified but before objective 2 is executed** using a controlled process stop consistent with process ownership and preservation semantics; do not fake interruption with preset in-memory lists. Restart the actual supervisor/engine through an isolated runtime home, recover the exact `command_id` from durable state, forbid duplicate mutation/commit on objective 1, finish objective 2, verify all tests, and reach `PROJECT_COMPLETE` (or explicitly accepted equivalent terminal status), recorded `result.json`, `run.finished` event, authentic verification receipts and a **closed/terminalized admission** (no ghost ACTIVE). Maintain invariants: zero unauthorized writes, no paid routing, no historical Product command replay, no hot editing candidate files. Re-run the independent collector before/after to prove original 51 unchanged; any new synthetic command remains separately identified.

**Three separate acceptance outcomes:**

- `REAL_E2E_PASS`: every actual chain stage above independently observed; source and deployed binary identity known; no paid billing or unauthorized effects. Genuine production readiness is STILL a distinct consent gate.
- `RESOURCE_WAIT`: adequate eligible real free provider or account authority currently unavailable; don't misreport as implementation failure or purchase authorization. Continue safe static/simulated regressions but hold actual acceptance until entitlement available.
- `FAIL / REPLAN`: concrete implementation failure after maximum one focused repair/retest. Stop adding patches to a failing control architecture; evaluate the V2 alternative and reuse modules that independently passed.

**Do not use tests alone to claim `REAL_E2E_PASS`.** CI green and ledger hash valid are necessary but not sufficient.

# 7. Safe implementation, migrations and release strategy

- Start from independently verified GitHub HEAD; create **one clean, isolated AOS-only execution branch/worktree**. Capture sha/source/protected host snapshot with hashes before any changes. Keep backups/read-only old runtime/command state; no cleanup of old 79 linked worktrees without separate explicit consent.
- Make design and safe isolated source edits if tools allow, running focused tests and full exact-SHA CI. Publish only to a new isolated branch if allowed and authorized, normal non-force push. **Never force-push main, auto-merge, alter V1 protected runtime/slot, mutate persistent 51 commands, or install startup as part of audit/replan.**
- If V2 needs new storage/state contract, define schema, migration mapping, reversibility, side-by-side read-only compatibility, idempotent migration and dual-run observation. A green migrated ledger does not prove new command scheduling. Apply migrations to **disposable snapshot copies first**. Avoid writing to the live KCP except via genuine native append-only APIs under separate acceptance.
- Do not assume pointer flip is zero downtime. Existing verified process is quiesce→native stage→maintenance activation→health proof→stable promotion→resume, and each is separate authorization gated. Rollback is only proven after verifying the restored supervisor/API health, not because pointer metadata changed.
- Optional official `agy` CLI installation, interactive login, model quota/billing confirmation, external offsite backup and startup installation may require ONE clear owner action each. State precisely what is required; never fake consent or creds. Neither AG nor Codex is mandatory for initial native/free baseline acceptance if an eligible real free reasoning route works.
- If unavailable tools or external accounts block execution, **produce fully self-contained artifacts and handoff, not imaginary live results**. The controller must be able to continue without reconstructing context from chat.

# 8. Implementation/control handoffs: make continuation reliable

Create/update a compact canonical *AOS-only* project-control folder in the isolated branch (names can be reconciled with existing conventions; do not duplicate an existing canonical system):

```
docs/aos-v2/
  00_READ_ME_FIRST.md                  # live evidence vs historical claims
  01_PRODUCT_CONTRACT.md               # must-do, won't-do, operator journey
  02_EXECUTION_GRAPH.md                # actual code path and decision reasons
  03_MODULE_DECISIONS.md               # KEEP/MODIFY/REMOVE/REPLACE/ADD matrix
  04_ARCHITECTURE_DECISION.md          # options A/B/C, chosen, rejected, rollback
  05_IMPLEMENTATION_DAG.md             # topological WPs, owner, scope, effort
  06_EXECUTOR_PACKETS/                 # one executable self-contained AG/Codex prompt per WP
  07_EVIDENCE_MATRIX.md                # level, origin, SHA, proof links, counterevidence
  08_LIVE_ISSUES_AND_GATES.md          # precisely blocked vs unblocked
  09_HANDOFF_CONTROLLER.md             # resume position, allowed next action
  10_RELEASE_AND_RECOVERY.md           # cutover/migration/rollback, user approvals
  11_PRODUCT_DEMO_ACCEPTANCE.md        # real goal and independently observed outcome
  STATE.json                           # machine-readable single high-level checkpoint
  DECISIONS.jsonl                      # immutable decision events, no duplicated task ledger
```

If these documents already have an authoritative equivalent, **update/repoint the existing source rather than creating a second conflicting knowledge plane**. Explicitly identify the canonical path. Each packet/handoff includes repository and branch, exact base/head SHA, current task state, changed files, actual CI run status, local artifact locations and hashes, execution log, recent verified receipt IDs, pending blocker, next safe command, and whether protected host remains untouched. Include evidence confidence and real vs simulated.

**Handoff must work with a fresh ChatGPT/AG/Codex conversation**: no implicit memory, no references to older chat messages, no invented paths or run IDs, no requirement to paste a massive previous chat. Provide one compact `RESUME_HERE.md` and a machine-readable state manifest, along with exact URLs and hashes. If a connector cannot access local Windows, say so and use one bounded local read-only probe.

# 9. YOUR deliverables — all mandatory

Produce the following in Turkish as concise executive explanations backed by files, not vague prose:

1. **One-page hard reality verdict:** does AOS have a salvageable core? what proves it? which promised features are absent? exact highest-impact execution blocker? who/what caused the repeated false progress claims?
2. **Full audit inventory and dependency map:** every major source category, entrypoint, persistent state, provider route and active/dead edge, high-risk trust boundaries, independent evidence and open unknowns. Show coverage percentage by *actually inspected code inventory*, not intuitive estimate.
3. **Weighted architectural option comparison and ONE chosen V2 design:** what stays active in V1, what moves to V2, what becomes optional/disabled, what is deleted, precise interfaces, cost/resource/security/runtime tradeoffs, ADRs and migration chart. If V1 surgical fix wins, the product can still be branded V2 by architectural capability, not just renaming.
4. **Operator-ready end-state definition:** explicit goal → authorization → selected resource → execution → verified delivery → continuation → terminal closure; one diagram and acceptance contract for each step. Include 16GB no-GPU constraints and absence of baseline agy.
5. **Finite executable engineering program:** ordered WP dependency DAG with scoped AG/Codex prompts **fully authored by YOU**, source files, CI/tests, gates and independently demonstrable exit conditions. When direct tools are available, initiate/complete **safe isolated** implementation; distinguish planned vs completed.
6. **One real vertical-slice E2E record:** if allowed/available, execute and observe; otherwise provide the ready-to-execute harness/evaluator instructions and exact authority/resource blocker, not a forged pass. Do not submit an old Product command.
7. **Safe rollout/rollback + migration plan:** V1 remains healthy until separately authorized V2 cutover; source/host provenance, checkpoint mapping, snapshot restore proof, shutdown behavior and exact consent gates.
8. **Controller handoff:** all artifacts and `RESUME_HERE.md`, machine-readable state, current branch/CI, one next action, user approval requests, no repeated work. If delegating to AG/Codex, give exact packets and verification checklist so execution can continue independently.
9. **Delete/reduce complexity estimate:** list unnecessary classes/files/abstractions, performance and maintenance savings with bounded measured/baseline estimates; avoid invented numeric ROI.
10. **Explicit STOP conditions:** what would cause you to abandon the current control layer and pivot to a smaller V2, and which proven V1 assets will be reused.

Final answer must start with: `DECISION: KEEP / HYBRID_V2 / REPLACE_EXECUTION_CORE` (select one); `EXECUTABLE_E2E: PASS / FAIL / WAITING_RESOURCE / NOT_RUN`; `PROTECTED_V1: UNTOUCHED / CHANGED_WITH_APPROVAL / UNKNOWN`; and `NEXT_SAFE_ACTION:` followed by exactly one grounded action. No vague “everything is ready” prose.

# 10. Anti-loop contract

- You have authority to **decide** architecture and write comprehensive **plans**, not to pretend approval for protected live mutations.
- **No re-auditing completed evidence without a specific inconsistency**; use prior exact SHA reports as historical context. Work only on open decisive unknowns. Never create new phases solely to rename the same blocked integration.
- **One vertical slice before optional expansion.** Freeze optional AG/Qwen/FreeLLMAPI activation until base user journey is proven. After base proof, enable only real available resources with matching authority.
- **Every code task must reduce an identified inability to perform real work.** If it merely increases PASS count, checkpoint count, report length or CI branches, reject it.
- **No source patching of an immutable live candidate.** No grant of permissions via request-provided fields. No fake startup/paid/provider status.
- If operating tools are unavailable, tell the truth and deliver ready-to-run execution materials. If owner permission is missing, ask exactly for that permission and complete all unrelated safe work while waiting.
- Preserve and respect the separate LARİ/Codex governance: AOS audit cannot authorize LARİ production or security writes. Cross-project examples must be disposable synthetic artifacts.
- This job is not complete after the audit document: it is complete when implementation is *ready and accurately handed off*, and operational completion has the distinct required real E2E proof. **Never conflate these two completion statuses.**

**BEGIN NOW:** Retrieve the evidence packet, reverify live GitHub HEAD and connected source, reconstruct actual execution and authority graph, inspect the Windows live state if connected, decide the best V2 topology, and produce the deliverables/worker packets. Perform only separately authorized isolated execution. No new generic B12/B13 report cycle.
