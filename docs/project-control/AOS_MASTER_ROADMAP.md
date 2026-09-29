# AOS Master Roadmap

Baseline: `5ce59e16be1490ac2c56fc92bdf9fcdfc16f1bec`. This roadmap separates source
completion from live promotion. Production remains `NO_GO`.

| Package | Status | Evidence and remaining gate |
|---|---|---|
| WP0 Canonical Architecture Reconciliation | COMPLETE | Reference architecture and archaeology matrix created |
| WP1 Integration Contracts | COMPLETE | Schema, JSON contract, Markdown contract, schema tests |
| WP2 Telemetry Truth | PARTIAL | Generic current-lineage HUMAN_REQUIRED, per-attempt backend/bridge/failover telemetry, resource dimensions, and writer priority fixed; cockpit visual outcome presentation remains |
| WP3 Integrity Reconciler | COMPLETE_SOURCE | Durable dedupe, lost-work finding, collision prevention, and independently derived execution-ID outcome partition; live promotion not performed |
| WP4 Resource Ledger / Quota Governor reconciliation | COMPLETE_SOURCE | Snapshot, ledger, quota governor, and orchestrator share one critical-path store; current-machine quality/latency observations remain an operational proof gate |
| WP5 Modern AG Critical-Path Rebinding/Reconciliation | PARTIAL | Worker path reaches registered executor and bridge; current protected-lane real execution is open |
| WP6 Local Qwen operational envelope | PARTIAL | Source/tests and historical lifecycle proof; current machine/protected-lane proof open |
| WP7 Capability/Scarcity Router | COMPLETE_SOURCE | Deterministic router records provider-neutral snapshots and preserves UNKNOWN; real protected-lane selection remains a later live proof |
| WP8 Durable Backend Re-entry | PARTIAL | Exact compatibility and dedupe enforced; live outage/fallback/re-entry proof open |
| WP9 Design Intelligence Critical Path | PARTIAL | Pre/post source path and project render contract exist; real-browser exact-SHA proof remains open |
| WP10 Self-Maintenance Convergence | COMPLETE_SOURCE | Progress fingerprints and bounded REPLAN_NOOP technical hold added |
| WP11 Detached Promotion Coordinator | OPEN | Must remain independently authorized; no promotion in this work |
| WP12 Full Certification | PARTIAL | 15 deterministic scenarios pass; 9 require candidate/current-runtime evidence; full offline suite is 1,135 passed, 8 skipped, 26 deselected; implementation checkpoint exact-SHA CI passed |
| WP13 Controlled Resume | BLOCKED | Independent Controller review, immutable candidate review, and separate resume authority required |

## Ordered next gates

1. Independent review of the final branch evidence; implementation checkpoint exact-SHA CI run `36553293959` passed.
2. Materialize an immutable candidate without activation; verify inventory,
   provenance, isolated smoke, and `PROMOTION_READY` only.
3. Collect current-machine quality, latency, credential/service, and lane
   eligibility observations through the authoritative snapshot without
   fabricating UNKNOWN fields.
4. Execute a disposable agentic outage/fallback/re-entry certification without
   protected-lineage or paid-provider mutation.
5. Generalize Design Intelligence beyond `index.html` and version the LARI
   canonical viewport set.
6. Only under new Controller authority, conduct a paused-safe candidate trial and
   controlled same-lineage resume. Production remains separately human-gated.
