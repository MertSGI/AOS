# AOS Capability Archaeology Matrix

Baseline: `5de4adb1b840f28a9c74a23054e2a8bda8e86a08`. `HISTORICAL` means an evidence
artifact exists for an older exact SHA; it does not move to this baseline.
`REPORTED` means supplied Controller observation, not re-executed proof in this
repair. `UNKNOWN` never means healthy or absent.

| Capability | SOURCE_PRESENT | TESTED | HISTORICAL_REAL_PROOF | CURRENT_SOURCE_STATUS | RUNTIME_WIRED | CRITICAL_PATH_WIRED | LIVE_PROMOTED | CURRENTLY_OPERATIONAL | SUPERSEDED | CONTRADICTED | MISSING_INTEGRATION | NEXT_REQUIRED_PROOF |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Canonical project truth | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | old Slice 2 pointer | NO | none confirmed | Exact-SHA current control read plus runtime binding |
| Human/controller ingress | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | permissive stale action path | FIXED | none confirmed | Hosted exact-SHA action rejection suite |
| Authority routing | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Current runtime denied-scope receipt |
| Objective planner | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | DEGRADED_REPORTED | NO | maintenance churn | convergence acceptance | Meaningful source/evidence delta |
| Task DAG | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Restart plus accepted-work no-replay |
| Workspace identity | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Current agentic stale-session rejection |
| Write scopes | YES | YES | PARTIAL | TESTED | YES | YES | NO_NEW_PROMOTION | CANDIDATE_TESTED | planner-only collision handling | FIXED | none confirmed | Multi-process collision run |
| Leases | YES | YES | YES | TESTED | YES | YES | NO_NEW_PROMOTION | CANDIDATE_TESTED | task-only lease model | PARTIAL | distributed lease unification | Cross-machine exclusive-scope proof |
| Coordination | YES | YES | YES | TESTED | YES | YES | HISTORICAL | UNKNOWN | NO | NO | integrity lease and coordination lease remain distinct | Reconciled multi-machine proof |
| Native execution | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | NO | NO | none confirmed | Current protected-lane accepted task |
| Provider registry | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | DEGRADED_POSSIBLE | NO | NO | resource semantic projection | Current observation per provider |
| Provider failover | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Same-lineage current-runtime failover |
| Circuit breakers | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | stale generic health | NO | none confirmed | Newest command-local observation |
| ResourceLedger | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | capability snapshot is incomplete | Current resource event reconciliation |
| QuotaGovernor | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | health/quota UI conflation | PARTIAL | Independent current quota observations |
| ResourceOrchestrator | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | unified quality history absent | Deterministic ranking plus live selection |
| FreeLLMAPI | YES | YES | HISTORICAL_BOUNDED | TESTED_SOURCE | PARTIAL | PARTIAL | NO | UNAVAILABLE_OR_UNKNOWN | operational wording | YES | managed built service | Local readyz plus real safe request |
| Local Qwen / llama.cpp | YES | YES | HISTORICAL_OLD_SHA | TESTED_SOURCE | YES | YES | UNKNOWN | UNKNOWN | operational wording | YES | current machine lifecycle proof | Benchmark, lifecycle, protected selection |
| Antigravity executor | YES | YES | HISTORICAL_OLD_SHA | REGISTERED_GATED_ATTEMPT_TELEMETRY_TESTED | YES | YES | UNKNOWN | UNKNOWN | disabled-by-default prose | FIXED | protected-lane real execution | Current attestation plus bounded task |
| Codex CLI executor | YES | YES | HISTORICAL_BOUNDED | REGISTERED_GATED_ATTEMPT_TELEMETRY_TESTED | YES | YES | UNKNOWN | UNKNOWN | quota hard-code | FIXED | current executable/auth/quota proof | Current bounded execution and resume |
| Cline executor | YES | YES | HISTORICAL_PROCESS_ONLY | REGISTERED_GATED_ATTEMPT_TELEMETRY_TESTED | YES | YES | UNKNOWN | DEGRADED_OR_UNKNOWN | candidate operational wording | YES | successful terminal execution | Current successful bounded task |
| Agentic planning bridge | YES | YES | NO | UNDERLYING_ATTEMPT_TELEMETRY_TESTED | YES | YES | UNKNOWN | UNKNOWN | operational wording | YES | protected-lane planning selection | Structured plan from current backend |
| Session continuity | YES | YES | HISTORICAL | TESTED | YES | YES | UNKNOWN | UNKNOWN | NO | NO | fresh-context fallback orchestration | Backend loss/change/re-entry E2E |
| Workspace fingerprinting | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Current exact-session compatibility run |
| Checkpoint/re-entry | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | NO | NO | technical-hold resume policy | Worker restart no-replay proof |
| Evidence aggregation | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | accepted-work linkage strengthened | Current evidence-to-checkpoint audit |
| Verifier | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Exact-SHA CI plus artifact verification |
| Design Intelligence | YES | YES | HISTORICAL_OLD_SHA | PARTIAL_CRITICAL_PATH | YES | PARTIAL | UNKNOWN | UNKNOWN | operational wording | YES | versioned viewport and generic app adapter | Protected UI task real-browser manifest |
| Real browser capture | YES | YES | HISTORICAL_OLD_SHA | TESTED_SOURCE | YES | PARTIAL | UNKNOWN | UNKNOWN | NO | current baseline proof absent | YES | Current exact-SHA six-view capture |
| Candidate store | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | NO | NO | none confirmed | Candidate through PROMOTION_READY only |
| Exact-SHA provenance | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | checkout-equality assumption | FIXED_BASELINE | none confirmed | Hosted CI bound to final repair SHA |
| Runtime slots | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED | stale slot path | FIXED_BASELINE | none confirmed | Candidate smoke without activation |
| Promotion/rollback | YES | YES | YES | TESTED | YES | YES | HISTORICAL | NOT_EXERCISED | NO | NO | detached review boundary documentation | Independent promotion trial later |
| Supervisor | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | REPORTED_HEALTHY | NO | NO | none confirmed | Restart plus slot identity proof |
| Pause-safe | YES | YES | YES | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Supervisor reboot restoration |
| Controller relay | YES | YES | YES | GENERIC_CURRENT_LINEAGE_AGGREGATION_TESTED | YES | YES | REPORTED_BASELINE | REPORTED_WITH_DEFECT | protected-ID-dependent HUMAN_REQUIRED aggregate | FIXED_SOURCE | hosted exact-SHA relay proof | E2E-23 in hosted CI |
| Cockpit | YES | YES | HISTORICAL | TESTED_PROJECTION | YES | YES | REPORTED_BASELINE | UNKNOWN | hard-coded resource truth | PARTIAL_FIXED | outcome UI presentation | Browser projection verification |
| Human Action Center | YES | YES | NO | TESTED_REPAIRED | YES | YES | NO_NEW_PROMOTION | CANDIDATE_TESTED | commit `5f0e722` raw form | ADAPTED | none confirmed | Controller review of fail-closed semantics |
| Deliberation council | YES | YES | PARTIAL | SHADOW_ONLY | YES | PARTIAL | REPORTED_BASELINE | SHADOW_ONLY | NO | NO | primary-path acceptance intentionally absent | Budgeted shadow quality sample |
| Self diagnosis | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | SHADOW_ONLY | NO | NO | none confirmed | Current blocking finding reconciliation |
| Self repair | YES | YES | HISTORICAL | BOUNDED_SHADOW | YES | PARTIAL | REPORTED_BASELINE | SHADOW_ONLY | NO | NO | independent acceptance/promotion | Exact-SHA candidate-only repair |
| AOS maintenance lane | YES | YES | REPORTED_98_BATCHES | CONVERGENCE_GUARD_ADDED | YES | YES | NO_NEW_PROMOTION | NON_CONVERGENT_REPORTED | infinite churn | FIXED_SOURCE | real acceptance proof | Bounded terminal result with delta/no-op |
| Multi-project/lane isolation | YES | YES | HISTORICAL | TESTED_REPAIRED | YES | YES | NO_NEW_PROMOTION | CANDIDATE_TESTED | workspace lock only | PARTIAL_FIXED | cross-machine integration | Two-lane exclusive-scope proof |
| Source transport recovery | YES | YES | HISTORICAL | TESTED | YES | YES | REPORTED_BASELINE | UNKNOWN | NO | NO | none confirmed | Outage and same-lineage resume |
| Integrity reconciliation | YES | YES | NO | INDEPENDENT_EXECUTION_PARTITION_TESTED | YES | YES | NO | NOT_LIVE_PROMOTED | bucket-derived total | FIXED_SOURCE | historical stores remain unknown | Instrumented protected command proof |

## Historical Action Center commit classification

Commit `5f0e722dd7e441005171d173b4c555b351d7be12` changes only
`src/aos/action_center.py` and `tests/test_action_center.py`.

| Change | Classification | Reconciliation |
|---|---|---|
| Status constants and audit-visible supersession | STILL_REQUIRED | Integrated |
| Batch/generation freshness | STILL_REQUIRED | Integrated |
| Exact command and canonical revision binding | STILL_REQUIRED | Integrated |
| Protected lineage preservation | ALREADY_PRESENT plus hardening | Preserved |
| Provider/quota wait is not human-required | ALREADY_PRESENT | Regression retained |
| Default missing provenance to `PROVEN` | CONFLICTS_WITH_CURRENT_ARCHITECTURE | Replaced with fail-closed explicit provenance |
| Force generated expected lane state to `HOLD` | REQUIRES_ADAPTATION | Bound to actual observed lane state |
| Mechanical commit application | SUPERSEDED | No cherry-pick performed |
