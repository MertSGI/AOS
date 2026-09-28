# AOS End-to-End Reference Architecture

Status: `SOURCE_RECONCILED_REVIEW_CANDIDATE`
Baseline: `5de4adb1b840f28a9c74a23054e2a8bda8e86a08`
Production: `NO_GO`
Paid fallback: `DISABLED`

## Authority and evidence boundary

AOS is a durable non-production execution system. A cockpit, relay, model,
agentic session, local cache, compatibility record, or moving Git ref is a
projection or input; none is terminal authority. Canonical control binds an
exact revision. Accepted source, hosted CI, a runtime candidate, promotion, and
field operation are separate evidence transitions.

The canonical flow is:

```text
Human Intent
  -> Versioned Control Request
  -> Canonical Revision Binding
  -> Authority Classification
  -> Objective / DAG Planner
  -> Task Capability Requirements
  -> Workspace + Write-Scope + Lease
  -> Resource Ledger / Quota Governor
  -> Capability/Scarcity-Aware Router
  -> Reasoning Provider OR Agentic Executor
  -> Durable Checkpoint
  -> Verification / Evidence
  -> Accepted Source
  -> Exact-SHA Remote CI
  -> Immutable Runtime Candidate
  -> Provenance
  -> Independent Promotion Boundary
  -> Runtime / Supervisor
  -> Relay / Cockpit Projection
  -> Reconciliation / Recovery
```

## Control planes and data planes

| Plane | Authority | Durable identity | Fail-closed rule |
|---|---|---|---|
| Project control | Versioned canonical control ref | project identity, canonical revision, control generation | Moving refs never transfer evidence |
| Runtime command | Runtime store | command lineage | Protected lineages are resumed, never recreated |
| Planning | Planning kernel and canonical authority resolver | objective, DAG, task signature | Completed accepted signatures cannot re-enter |
| Mutation | Execution authority plus integrity reconciler | workspace fingerprint, write scope, lease | Overlapping exclusive scopes enter HOLD before dispatch |
| Resources | Resource policy, ledger, quota governor, orchestrator | resource ID and class | Health, quota, credentials, service, scarcity, and eligibility remain separate |
| Agentic execution | Backend-neutral agentic contract | external session plus AOS checkpoint | Exact session resumes only under full compatibility |
| Evidence | Evidence aggregator and verification | evidence ID and exact source SHA | Tests and historical proof are not live promotion |
| Runtime delivery | Candidate store and runtime slots | candidate, slot, runtime source SHA | Promotion is independently authorized |
| Operations UI | Relay and cockpit | writer instance and sequence | Projection cannot mutate canonical truth |

## Required distinctions

- `REASONING_PROVIDER != AGENTIC_EXECUTOR`: providers return bounded reasoning;
  agentic executors can act in a bound workspace. An agentic backend can answer a
  reasoning request only through `AgenticStructuredPlanningBridge`.
- `SOURCE_IMPLEMENTED != LIVE_PROMOTED`: current source can be correct while the
  active slot remains older or unverified.
- `TESTED != REAL_EXECUTION_PROVEN`: deterministic tests prove contracts, not a
  current external CLI, browser, or provider execution.
- `RUNTIME_WIRED != CRITICAL_PATH_WIRED`: registration is not reachability from
  the protected worker and task envelope.
- `CAPABILITY_EXISTS != LANE_ELIGIBLE`: capability attestation, authority,
  health, quota, scarcity, workspace, and task class all gate selection.
- `HEALTH != QUOTA`: a healthy service may have no quota; an unknown health state
  is not healthy.
- `BACKEND_CHANGE != PROJECT_CHANGE` and `ACCOUNT_CHANGE != PROJECT_CHANGE`:
  routing changes do not change project identity, canonical revision, or lineage.
- `QUOTA_LOSS != STATE_LOSS` and `PROVIDER_FAILURE != PROJECT_FAILURE`: waits
  preserve checkpoints and command identity.
- `COMPLETED_WORK_MUST_NOT_BE_DUPLICATED`: accepted semantic signatures are
  restored without backend dispatch.
- `STALE_AGENT_SESSION_MUST_NOT_BE_BLINDLY_RESUMED`: backend, resource, source,
  workspace, adapter, executable, auth mode, and terminal state must match.
- `PROTECTED_LINEAGE_MUST_NOT_BE_RECREATED`: control actions mutate the exact
  existing command only after freshness validation.
- `PRODUCTION_REMAINS_HUMAN_GATED`: this repair ends at review readiness.

## Reasoning and agentic execution

`ExecutionRouter` is reachable from `runtime_worker -> run_autonomous_project ->
run_host -> PersistentCoordinator`. Native workers, Qwen, provider failover,
Antigravity, Codex CLI, Cline, and the three planning bridges are registered.
Selection still depends on advertised capabilities and current availability.

Antigravity is therefore not globally disabled in source. It is registered and
selectable for `LONG_HORIZON_AGENTIC_WORK`, or for `MODEL_REASONING` only through
its planning bridge, when current-machine capability attestation succeeds. Its
session identity is stored in the coordinator checkpoint. Resume is rejected on
source/workspace/adapter/executable/auth incompatibility. Historical CLI proof is
not evidence that the backend is currently eligible on every runtime machine.

Host, planning-kernel, relay, and cockpit telemetry now report registration,
availability, eligibility, and invocation count separately. A zero invocation
count means no observed invocation, not that source registration is absent.

## Integrity and convergence

The shared runtime integrity root holds exclusive leases, accepted semantic work,
and mutually exclusive execution outcomes. Mutation claims occur before backend
dispatch. Accepted records bind project, command, task signature, workspace,
checkpoint, and expected artifacts. The reconciler reports concrete duplicate,
lost-work, and collision counts only for explicitly instrumented stores; older or
incomplete stores remain `UNKNOWN`.

Planning persists progress, plan, and blocker fingerprints. Three consecutive
batches with no accepted-state delta produce `REPLAN_NOOP` and `TECHNICAL_HOLD`.
This is a technical convergence boundary, not a fabricated human decision.

## Design Intelligence boundary

Current source contains pre- and post-implementation Design Intelligence for
`repo_ui_planning`, including a real Playwright capture adapter and critic loop.
It is only partially compliant with the canonical LARI design protocol: the
policy is not yet a single versioned cross-project viewport contract and the
current path is specialized around workspace `index.html`. Historical six-view
proof does not establish current-baseline protected-lane execution.

Required final UI evidence remains: exact target SHA, real browser, canonical
viewports, screenshot hashes, overflow/CTA/accessibility findings, critic result,
and an evidence manifest. Synthetic capture is never final evidence.

## Source and runtime delivery

Source acceptance is isolated workspace -> tests -> scope/diff verification ->
commit -> fast-forward push -> exact remote SHA -> hosted CI success. Runtime
delivery is accepted source SHA -> immutable candidate -> inventory/manifest ->
exact-SHA provenance -> isolated smoke -> `PROMOTION_READY` -> independent
promotion -> `PAUSED_SAFE` trial -> exact slot/SHA/nonce health -> protected
lineage continuity -> stable promotion.

This work stops before candidate activation, runtime start, protected command
resume, or production promotion.
