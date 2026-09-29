# AOS End-to-End Certification Matrix

Classification is evidence-specific. `PASS` is used only after the named local
test passes on this branch. Historical live evidence remains historical.

| ID | Scenario | EXISTING_TEST | NEW_TEST | Classification | Current evidence / limitation |
|---|---|---|---|---|---|
| E2E-01 | Intake -> DAG -> route -> execute -> verify -> checkpoint | `test_resource_os_continuity_e2e.py`, coordinator tests | No | PARTIAL | Components execute together; no single hosted whole-story proof |
| E2E-02 | Rate limit -> alternate free resource -> same lineage -> no duplicate | provider continuity/fabric tests | Integrity dedupe coverage | PARTIAL | Deterministic provider and dedupe proofs are separate |
| E2E-03 | Cloud unavailable -> local/native continues; unsafe waits | provider fabric and Qwen tests | No | PARTIAL | Deterministic routing proof; no current protected-lane run |
| E2E-04 | AG quota -> checkpoint -> fallback -> safe re-entry | agentic backend/continuity tests | Integrity dedupe coverage | PARTIAL | No live AG quota event in this repair |
| E2E-05 | Stale AG session after workspace mutation rejected | Antigravity backend tests | No | PASS | Exact compatibility gate exercised deterministically |
| E2E-06 | Worker restart -> no replay | durable restart tests | E2E-15 accepted signature test | PASS | Checkpoint plus semantic accepted-work dedupe |
| E2E-07 | Supervisor reboot -> PAUSED_SAFE restore | runtime pause-safe tests | No | PASS | Deterministic runtime-store proof |
| E2E-08 | Source outage -> wait -> same lineage resume | source adapter/recovery tests | No | PASS | Deterministic transport failure classification |
| E2E-09 | Stale runtime path -> identical server/worker assets | runtime assets and health identity tests | No | PASS | Exact active-slot resolution regression |
| E2E-10 | Stale Human Action -> SUPERSEDED/non-actionable | Action Center tests | Recovery Director admission/proof tests | PASS | Durable audit record retained; SUPERSEDED admission is irreversible |
| E2E-11 | Wrong command binding rejected | Action Center tests | E2E action freshness | PASS | Exact command binding enforced |
| E2E-12 | Unproven runtime mutation rejected | Action Center tests | E2E action freshness | PASS | Missing or non-PROVEN provenance rejects |
| E2E-13 | Maintenance isolation -> zero LARI/UI mutation | concurrent lane tests | No | PARTIAL | Lock/scope contract proven; no live maintenance run |
| E2E-14 | Cross-lane write collision prevented pre-mutation | No | `test_e2e_14_cross_lane_collision_prevented_before_mutation` | PASS | Durable exclusive-scope claim |
| E2E-15 | Accepted signature after restart/provider switch skipped | continuity tests | `test_e2e_15_and_16_dedupe_then_detect_lost_accepted_work` | PASS | Provider-neutral semantic signature |
| E2E-16 | Lost accepted work produces concrete failure | No | `test_e2e_15_and_16_dedupe_then_detect_lost_accepted_work` | PASS | Missing expected artifact yields finding |
| E2E-17 | Dev/test relay writer contamination rejected | No | `test_e2e_17_lower_priority_writer_cannot_replace_supervisor_snapshot` | PASS | Supervisor writer priority is authoritative |
| E2E-18 | Outcome partition is independently complete and exclusive | No | `test_e2e_18_rejects_double_outcome_classification`, `test_e2e_18_rejects_known_execution_without_outcome`, `test_e2e_18_deduplicates_exact_outcome_replay`, `test_e2e_18_six_unique_outcomes_project_to_relay` | PASS | Total derives from unique durable execution IDs; double/missing/invalid classifications fail and exact replay cannot inflate counts |
| E2E-19 | Material UI requires real browser evidence | DI pipeline tests | Project render-entry contract tests | PARTIAL | Project-aware static entry discovery and typed unavailable evidence are proven; no real browser run on this branch |
| E2E-20 | Candidate pipeline through PROMOTION_READY only | candidate/materialization tests | Platform recovery source-boundary and isolated source-repair pipeline tests | PARTIAL | Isolated lineage, clean commit, exact-SHA publication/certification, candidate binding, and no activation/promotion are enforced; no candidate created by this task |
| E2E-21 | Candidate failure rollback preserves identity | runtime recovery/slots tests | No | PARTIAL | Deterministic components; no current candidate trial |
| E2E-22 | Self-maintenance converges before 100-batch churn | recovery churn tests | `test_repeated_zero_delta_replans_enter_technical_hold` | PASS | Three zero-delta batches -> REPLAN_NOOP/TECHNICAL_HOLD |
| E2E-23 | Any current lineage in HUMAN_REQUIRED -> aggregate YES | No | protected three-lane regression plus generic current, historical exclusion, and supersession tests | PASS | Generic durable current-lineage projection; no protected command ID is needed for aggregation |
| E2E-24 | Ledger separates health/quota/credentials/service/eligibility | ledger/governor/orchestrator tests | Provider-neutral snapshot-ledger and shared-router-wiring tests | PARTIAL | One critical-path snapshot/ledger/quota/orchestrator plane preserves credential/service/quota/quality/latency as UNKNOWN when unobserved; live quality history remains unproven |

Additional bootstrap invariants are covered by
`tests/test_autonomous_recovery_director.py`: durable selective admission,
legacy fail-closed HOLD, atomic selected activation, irreversible supersession,
proof binding/freshness/replay rejection, real repair postconditions, persistent
repair mode, relay-to-platform recovery job wiring, a concrete isolated source
repair pipeline, non-product finite system-repair jobs, no self-promotion,
truthful resource UNKNOWN, shared resource-ledger/router wiring, and
project-aware render entry resolution.

Bootstrap classification remains 15 `PASS`, 9 `PARTIAL`, 0
`NOT_YET_EXECUTABLE`; source implementation does not automatically promote a
scenario. The current repository-wide offline result is recorded in
`docs/project-control/AOS_CURRENT_TRUTH.json`: 1,135 passed, 8 skipped, and 26
deselected. Implementation checkpoint `06d7f92a5af7c1477e073ce912e92d879d641de4`
passed hosted exact-SHA CI run `36553293959`; immutable candidate certification,
live promotion, and real protected-lineage execution remain separate gates.
