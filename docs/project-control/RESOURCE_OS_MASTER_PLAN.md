# Resource OS Master Plan

Track: `AOS-RESOURCE-OS-MASTER-20260923-01`  
Repository: `MertSGI/AOS`  
Branch: `feature/aos-resource-os-master-20260923-01`  
Accepted base: `ccdebfd7b5bdc32f7e95634ef2c464293356c872`  
Production: `NO_GO`  
Council: `SHADOW_ONLY`  
Paid API fallback: `DISABLED`

## Program invariants

- Durable project state is authoritative; resource health, quota, sessions, and the resource ledger are reconstructable operational state.
- Backend change, quota loss, provider failure, Antigravity loss, or Codex loss never implies project-state loss.
- Completed work is never duplicated, and stale agent sessions are never blindly resumed.
- Provider health, task-class capability, quota, credentials, local-service state, scarcity, and policy eligibility remain separate axes.
- Production, destructive action, payment, and protected-lineage authority remain human-gated.
- Protected LARI and LARI-UI-V2 command IDs and workspaces are out of scope and remain untouched.

## Checkpoint protocol

Every phase updates `RESOURCE_OS_STATE.md` and `RESOURCE_OS_EVIDENCE.md`, runs its focused deterministic acceptance suite plus `git diff --check`, creates one focused commit, pushes fast-forward only, and verifies local `HEAD` equals the remote branch head. A failed phase is repaired in place; accepted earlier checkpoints are not rewritten. Rollback means reverting the phase commit while retaining forward-readable durable files and canonical project work.

## Phase graph and gates

| Phase | Deliverable | Dependencies | Acceptance gate | Rollback boundary |
|---|---|---|---|---|
| 0 | Canonical plan, decisions, state, and evidence ledgers | Accepted source SHA and both mandatory preflights | Documents name every phase, dependency, test gate, safety invariant, and exact base | Documentation-only commit |
| 1 / R1 | Typed sanitized provider observations; contract subtypes; task-class/family health | Phase 0 | RM-01..05, FH-02..04, CT-01..03, TH-01..03; provider/runtime regression | New observation schema and backward-compatible circuit migration |
| 2 / R1B | Content-aware read identity and bounded hash-bound read reuse | R1 | RD-01..06 and long-history/restart tests | New checkpoint fields are optional/read-compatible |
| 3 / R1C | Same-batch/no-progress recovery churn guard | R1B | RC-01..05 and pause/provider-wait regression | Existing `HUMAN_REQUIRED` state; no new lineage |
| 4 / R2 | Durable `QuotaGovernor`, separate from health | R1C | FH-01, QG-01..03, PAY-01 and restart tests | Governor resource file may be ignored by older source; fail closed |
| 5 / R3 | Append-only/reconstructable `ResourceLedger` | R2 | RL-01..04, INV-01..02, PAY-01 | Ledger is non-canonical; valid prefix replay and fail-closed corruption |
| 6 | Shared agentic execution contract, `ContextPack`, content-sensitive workspace fingerprint | R3 | Contract/schema tests plus staged/unstaged/untracked/binary/delete/rename/mode/symlink/submodule fingerprint cases | Additive typed fields and schema-versioned checkpoints |
| 7 | Antigravity as first-class subscription-included agentic backend | Phase 6 and existing AG proofs | Availability mapping, exact conversation resume gate, quota reroute, stale-workspace supersession, Zero-AG regression | Disable resource policy entry without losing AOS checkpoint |
| 8 | Codex CLI as first-class subscription-included agentic backend | Phase 6, accepted Codex preflight | Strict JSONL/terminal contract, ChatGPT-only auth, scrubbed child env, exact UUID resume, quota/failure injection, no provider-factory registration | Disable resource policy entry; retain/supersede session journal |
| 9 | Loopback Qwen3-4B Q4_K_M bounded local reasoning resource | R3 and Phase 6 contracts | Offline adapter tests plus authorized machine-local benchmark for classification, triage, structured decisions, latency/memory/context/CPU | Resource unavailable/disabled; scheduler continues |
| 10 | Optional Jev decision layer | R3 and Jev preflight marked integration-ready | Advisory-only decisions, dynamic cost state, forbidden-authority tests, unavailable degradation | Adapter omitted/disabled; deterministic scheduler remains authoritative |
| 11 | Capability- and scarcity-aware Resource Orchestrator | R1-R3 and Phases 6-10 | Deterministic ranking across capability/risk/context/quality/health/quota/scarcity/latency/cost/session compatibility; paid-default denial | Fall back to deterministic/native and existing free provider policy |
| 12 | Bounded durable cross-resource `ContextPack` continuity | Phases 6-11 | Schema/redaction, same-objective lineage, completed-work/artifact preservation, stale-session rejection | AOS checkpoint stays authoritative; discard incompatible agent session only |
| 13 | Failure-injection continuity E2E | Phases 7-12 | AG and Codex quota/re-entry scenarios, cloud/local optional outages, Retry-After, task-class wake, churn hold, restart persistence, paid non-invocation | Disposable-only test state |
| 14 | Full validation | All implementation phases | Focused suites, provider/resource tests, execution fabric, runtime recovery, planning kernel, schemas, full `pytest -q`, `git diff --check`, bounded zero-cost disposable proofs | Repair only failing phase; no activation |
| 15 | Controller-review source candidate | Phase 14 green | Exact base/head/remote equality/cleanliness and all phase statuses recorded | Documentation-only carrier commit if code is unchanged |

## Resource preference policy

The orchestrator prefers adequate resources in this order, subject to risk and capability: deterministic/native; adequate local reasoning; free decision/reasoning; free cloud/meta-provider; subscription-included agentic capacity; explicitly enabled optional micro-paid decision capacity; separately authorized paid API safety layer. Local or free never overrides capability or risk requirements. Jev can advise but cannot authorize.

## Durable boundaries

- Project checkpoints, objective lineage, completed work IDs/signatures, and verified artifact hashes are canonical.
- Quota snapshots, health observations, resource ledger summaries, and external agent sessions are operational evidence.
- Context packs contain only objective/authority, source SHA, workspace fingerprint, checkpoint, completed work and artifact hashes, bounded fresh read context, remaining work, current typed availability/failure state, and explicit boundaries.
- Credentials, auth stores, hidden reasoning, unrestricted transcripts, raw tool parameters, raw response bodies/headers, prompts, cookies, and unredacted stderr are forbidden from durable resource evidence.

## Live-safety boundary

No phase deploys or activates this branch, changes the live runtime, resumes protected work, modifies protected workspaces, creates replacement protected lineages, enables paid API fallback, or changes production from `NO_GO`.
