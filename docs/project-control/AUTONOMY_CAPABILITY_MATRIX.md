# AOS Autonomy Capability Matrix

## Program Directive: Zero-to-One-Hundred Project Autonomy Completion

**Evaluation Date**: 2026-09-15  
**Operating Principle**: AOS governs and plans; Native Deterministic Workers > CI/GitHub > Model Specialists execute. AG is an optional specialist worker, strictly non-mandatory.  
**Production Status**: `NO_GO` (Fail-closed)

---

### Capability State Legend

- `NOT_STARTED`: No implementation or specification exists.
- `PARTIAL`: Incomplete implementation or draft architecture without full integration.
- `SOURCE_PROVEN`: Code complete with comprehensive unit/contract tests passing offline.
- `RUNTIME_PROVEN`: Verified via isolated real subprocess/database/container or CI execution.
- `STABLE_ACCEPTED`: Formally accepted into canonical main branch with independent verification proofs.

---

### Autonomy Capability Matrix (21 Dimensions)

| # | Dimension | Capability State | Proven Scope & Exact Evidence | Remaining Structural Gap |
|:---|:---|:---|:---|:---|
| 1 | **PROGRAM_ORCHESTRATION** | `STABLE_ACCEPTED` | Gates AOS-0 through AOS-5 `CLOSED_PROVEN`. State machine, decision log (`DECISION-001`..`028`), policy enforcement. | Cross-project automatic portfolio balancer. |
| 2 | **PRODUCT_UNDERSTANDING** | `RUNTIME_PROVEN` | Domain modeling across Clinic, Aesthetic, HT, and POS verticals in LARI (`EV-081`..`EV-093`). | Fully autonomous natural language ambiguity resolution without Controller hints. |
| 3 | **ARCHITECTURE_AUTONOMY** | `STABLE_ACCEPTED` | Schemas, contract validators, SQL migration chains (90 migrations ordered), multi-tenant isolation. | Multi-database distributed replication topology automation. |
| 4 | **ROADMAP_AUTONOMY** | `RUNTIME_PROVEN` | 12-week roadmap DAG tracking, multi-node parallel worktrees (Nodes 1-6 in P5, Nodes 1-2 in P6). | Automated dynamic DAG re-weighting on unexpected external API deprecation. |
| 5 | **EXECUTOR_INDEPENDENCE** | `STABLE_ACCEPTED` | **NEF V2 integrated to main (`3c0e63d`)**. Native subprocess workers, patch engine, zero-AG fallback proven (631 passed). | Non-Windows containerized local execution profile. |
| 6 | **PERSISTENT_COORDINATION** | `STABLE_ACCEPTED` | SQLite single-machine & Neon Postgres distributed leasing (`AOS-5`, `PROOF-33309891238-1`). | Autonomous automatic lease renewal heartbeat daemons on long-running jobs. |
| 7 | **MODEL_FABRIC** | `STABLE_ACCEPTED` | **Nemotron-3 Ultra R3 integrated to main (`0aad619`)**. Multi-provider routing, JSON-RPC 2.0 validation, strict schemas (654 passed). *Offline reasoning & provider contracts proven; live external NVIDIA API calls remain strictly uncertified until authorized real API key is independently discovered.* | Automatic multi-provider failover routing when primary reasoning quota is exhausted. |
| 8 | **DESIGN_INTELLIGENCE** | `SOURCE_PROVEN` | DI engine, contracts, critics, visual QA, design DNA in `extensions/design-intelligence`. Offline suites passing. | Headless Playwright/Chromium runtime verification in non-interactive CI/container. |
| 9 | **SECURITY_AUTONOMY** | `RUNTIME_PROVEN` | Tenant RLS matrix, secret redaction, audit debt registry, live Postgres adversarial tests (`EV-088`, `EV-093`). | Automated dependency reachability analysis and bounded pull request creation. |
| 10 | **QA_AUTONOMY** | `RUNTIME_PROVEN` | 22 domain contract test suites, live concurrency races, negative cross-tenant testing (all passed in CI run `34835325826`). | Automated visual regression diff generator with perceptual hashing. |
| 11 | **CI_AUTOMATION** | `STABLE_ACCEPTED` | Disposable Supabase Postgres container workflow, exact-SHA verification, GitHub Actions (`34835325826` across 20 steps). | Dynamic matrix generation for arbitrary newly added test domains. |
| 12 | **GIT_AUTOMATION** | `STABLE_ACCEPTED` | Deterministic worktree management, linear history fast-forwards, carrier commit verification. | Multi-remote automated failover sync. |
| 13 | **BROWSER_AUTOMATION** | `PARTIAL` | `RealBrowserCaptureAdapter` and `extensions/autonomy-fabric/measured_browser_evidence.py`. | Seamless headless execution during detached cross-session coordinator runs. |
| 14 | **DATABASE_AUTOMATION** | `RUNTIME_PROVEN` | 90 ordered SQL migrations applied sequentially with zero errors, live constraint introspection. | Automated zero-downtime Blue/Green table migration generation. |
| 15 | **MULTI_WORKER_ROUTING** | `STABLE_ACCEPTED` | Native worker registry, capability matching, task-to-worker dispatch in NEF V2. | Dynamic cross-host worker load balancing over WebSocket relay. |
| 16 | **FAILURE_RECOVERY** | `RUNTIME_PROVEN` | Bounded repair loops (e.g. EV-093 run `34834828086` failure resolved in `34835325826`). | Deep heuristic automated bug triage from raw stack traces. |
| 17 | **EVIDENCE_INTEGRITY** | `STABLE_ACCEPTED` | SHA-bound evidence files (`EV-001` through `EV-093`), hash attestation, tamper-proof registers. | Automated cryptographic signatures on evidence manifests. |
| 18 | **CONTROLLER_RELAY** | `SOURCE_PROVEN` | CR2-Lite relay rebased on main (`b0bf518`), published as reviewable candidate branch `candidate/controller-relay-rebased-main`. Canonical `control/controller-relay` (`7e80378`) untouched. 164 unit tests green offline. Live deployment remains `HUMAN_REQUIRED`. | Formal controller review and promotion to canonical relay branch. |
| 19 | **CROSS_SESSION_AUTONOMY** | `RUNTIME_PROVEN` | Durable state in `STATE.json`, task DAG resumption, transcript checkpoints. | Dedicated detached coordinator host process for unattended execution. |
| 20 | **RELEASE_AUTONOMY** | `RUNTIME_PROVEN` | Vite client production build validation, TypeScript zero-defect compilation (`tsc --noEmit`). | Automated staging environment promotion and smoke testing. |
| 21 | **PRODUCTION_READINESS** | `PARTIAL` | Fail-closed policy (`NO_GO`), pre-production security debt cataloging. | Human-required production authorization checklist fulfillment. |

---

### Top Structural Priorities for True Zero-to-100 Project Delivery

1. **LARI Phase 6 Product Execution**: Advance Node 3 (*Mixed Cart, Multi-Branch Stock Allocation & POS Checkout Foundation*) through schema, contracts, and disposable CI verification.
2. **Controller Relay Audit Promotion**: Await external Controller audit of `candidate/controller-relay-rebased-main` before any canonical fast-forward.
3. **Cross-Session Coordinator Lifecycle**: Prove isolated headless process supervisor for persistent autonomous progression without active session coupling.
