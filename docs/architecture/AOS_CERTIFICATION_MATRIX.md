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
| E2E-10 | Stale Human Action -> SUPERSEDED/non-actionable | Action Center tests | E2E action freshness | PASS | Durable audit record retained |
| E2E-11 | Wrong command binding rejected | Action Center tests | E2E action freshness | PASS | Exact command binding enforced |
| E2E-12 | Unproven runtime mutation rejected | Action Center tests | E2E action freshness | PASS | Missing or non-PROVEN provenance rejects |
| E2E-13 | Maintenance isolation -> zero LARI/UI mutation | concurrent lane tests | No | PARTIAL | Lock/scope contract proven; no live maintenance run |
| E2E-14 | Cross-lane write collision prevented pre-mutation | No | `test_e2e_14_cross_lane_collision_prevented_before_mutation` | PASS | Durable exclusive-scope claim |
| E2E-15 | Accepted signature after restart/provider switch skipped | continuity tests | `test_e2e_15_and_16_dedupe_then_detect_lost_accepted_work` | PASS | Provider-neutral semantic signature |
| E2E-16 | Lost accepted work produces concrete failure | No | `test_e2e_15_and_16_dedupe_then_detect_lost_accepted_work` | PASS | Missing expected artifact yields finding |
| E2E-17 | Dev/test relay writer contamination rejected | No | `test_e2e_17_lower_priority_writer_cannot_replace_supervisor_snapshot` | PASS | Supervisor writer priority is authoritative |
| E2E-18 | Outcome partition sum equals total | No | `test_e2e_18_outcome_partition_projects_to_relay` | PASS | Six exclusive durable buckets projected by relay |
| E2E-19 | Material UI requires real browser evidence | DI pipeline tests | No | PARTIAL | `index.html`-specific and viewport policy not unified |
| E2E-20 | Candidate pipeline through PROMOTION_READY only | candidate/materialization tests | No | PARTIAL | Source tests exist; no candidate created by this task |
| E2E-21 | Candidate failure rollback preserves identity | runtime recovery/slots tests | No | PARTIAL | Deterministic components; no current candidate trial |
| E2E-22 | Self-maintenance converges before 100-batch churn | recovery churn tests | `test_repeated_zero_delta_replans_enter_technical_hold` | PASS | Three zero-delta batches -> REPLAN_NOOP/TECHNICAL_HOLD |
| E2E-23 | LARI human hold + maintenance running -> aggregate YES | No | `test_e2e_23_protected_human_required_survives_running_lane` | PASS | Exact reported three-lane regression |
| E2E-24 | Ledger separates health/quota/credentials/service/eligibility | ledger/governor/orchestrator tests | Control-panel normalized resource contract | PARTIAL | Quality history and latency remain UNKNOWN, not fabricated |

Local certification summary: 13 `PASS`, 11 `PARTIAL`, 0
`NOT_YET_EXECUTABLE`. The repository-wide offline suite completed with 1,107
passed, 8 skipped, and 26 deselected; exact-SHA hosted CI remains a separate
source-acceptance gate.
