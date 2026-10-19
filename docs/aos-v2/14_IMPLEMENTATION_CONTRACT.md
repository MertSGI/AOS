# AOS V2 — Bağlayıcı uygulama sözleşmesi

Contract: `AOS-V2-SEAM-1`, Controller decision, 19 October 2026. This closes implementation choices inside the existing WP2/WP3; it creates no new phase. Decision remains **HYBRID_V2**. The original audit is not reopened. Source foundation is `7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506`; its four source files match the preserved local implementation plus the bounded WP1 path-scope correction. Engine/API/gateway/operator below are **PLANNED, NOT IMPLEMENTED**. No real model task has run.

The executor implements these decisions. It does not choose a replacement framework, weaken a boundary to pass tests, activate optional workers, or redesign the product. A newly discovered contradiction is reported with exact source/evidence and a proposed bounded delta; only the Controller changes this contract.

## 1. Product and implementation boundary

One explicit software goal, one command ID, one local journal, one writer process. Keep V1/history available; no migration, resume or cleanup of its 51 records. The thin V2 calls the existing pure exact-edit algorithm and native KCP receipt helpers. It does not invoke V1's planner/continuous worker, ModelNative network transport, hard-reset rollback, registered AG/Codex/Cline routes, coordination/Postgres fabric or self-repair.

MVP resources: native deterministic work and **explicitly eligible OpenAI-compatible chat-completion profiles**. The protocol does not mean the OpenAI paid service is enabled. No provider/model/endpoint default. Gemini-specific transport, AG/agy, Codex, local Qwen, FreeLLMAPI, multi-PC and council remain conditional after the one actual vertical slice. Existing V1 capabilities are preserved at their actual evidence level, not deleted to simplify the new entrypoint.

| Module | Required owner/interface | Writes |
|---|---|---|
| `__main__.py` | `main(argv)`; `serve`, `operator` | Only explicitly selected new V2 home |
| `registry.py` | `load_registry(path) -> RegistrySnapshot`; `authorize(project_id, now) -> AuthorizedProject`; `revalidate(binding)` | None; server-owned config is read-only |
| `api.py` | `LoopbackServer(engine, endpoint)`; strict request decoding and authenticated routes | Through Engine methods only |
| `engine.py` | `Engine.admit/resume/pause/cancel/tick/recover`; one owner lock; immutable plan | Journal and allowed effect protocol |
| `resources.py` | `ResourceGateway.generate(kind, context, budget) -> GenerationObservation` | Attempt reservation/completion in same journal |
| `bridge.py` | `prepare_candidate`, `publish_candidate`, `reconcile_task` | Private candidate objects, scoped Git/source effects |
| `sandbox.py` | `SandboxRunner.preflight/run_tests -> TestObservation` | Private sandbox scratch and output only |
| `acceptance.py` | `verify_task`, `verify_goal`, `export_outbox` | Immutable verification records and idempotent exports |
| `operator.py` (WP3) | One authenticated CLI; no direct journal mutation | API requests and its private observations |

Allowed WP2 source growth beyond the earlier packet is exactly `registry.py`, `sandbox.py`, `tests/test_aos_v2_registry.py`, `tests/test_aos_v2_sandbox.py` and the V2-only test workflow `.github/workflows/aos-v2-contract.yml`. These are the trusted authority and OS enforcement boundaries, not optional abstractions. Existing V1 entrypoints/configuration are unchanged. Minimal `lifecycle.py` integration is allowed under the schema/recovery rules below. Do not edit existing provider/native behavior to make V2 tests easier.

## 2. Entrypoint, API and authority

```powershell
python -m aos.v2 --home ABSOLUTE_NEW_V2_HOME serve --bind 127.0.0.1 --port 0 --registry ABSOLUTE_SERVER_REGISTRY
python -m aos.v2 --home THE_SAME_V2_HOME operator
```

Port `0` asks the OS for an available port and avoids a check/bind race or collision with V1. Atomically write actual address, owner nonce, executable/source identity and birth identity to private `endpoint.json`. Token is a new 32-byte random secret in a private file (POSIX0600; Windows user-only DACL); never argv, source, stdout or request logs. Existing invalid token/endpoint/home is HOLD, not silently replaced. First startup is PAUSED_SAFE, startup integration off. Operator checks discovery/owner identity and then uses the token locally.

PAUSED_SAFE startup means no recovered/existing command is dispatched automatically. After the operator has shown the explicit project/root/authority/resource preview, an explicit authenticated NEW goal submission starts only that newly admitted command. Existing commands require their own authenticated resume; submitting one goal never resumes all held work. This gives a one-submit test flow without an implicit startup grant.

Only bind `127.0.0.1`; reject another bind value. Require constant-time Bearer comparison, exact Host `127.0.0.1:<bound-port>`, absent Origin for CLI or exact local Origin for the same-origin UI; no CORS. Reject chunked bodies, duplicate JSON keys, nonfinite numbers, excessive nesting (>8), invalid UTF-8 and bodies >64KiB before admission. No arbitrary shell/command/test/grant endpoints.

| Route | Body / response / invariant |
|---|---|
| `POST /v2/goals` | Exactly `project_id`, `goal`, `idempotency_key`; goal 1–8000chars, nonblank; IDs follow lifecycle identifier rules. No root, backend, authority, receipt, state, cost or test command fields. `202`: command_id, binding_sha256, state, revision, delivery_status. |
| `GET /v2/commands/{id}` | Sanitized state/admission, verified task/criterion progress, observed owner identity, resource rejection reasons, result location and export status. No guessed process health. |
| `POST /v2/commands/{id}/pause` | Empty object only. Stops new dispatch; only an already-owned effect may finish/reconcile. |
| `POST /v2/commands/{id}/resume` | Empty object only. Revalidate authority/resource and existing artifacts; same command ID and immutable plan. Never an automatic new admission. |
| `POST /v2/commands/{id}/cancel` | Empty object only. Persist cancel request; forbid new effects, quiesce owned work, reconcile uncertain effects, then CANCELLED/CLOSED. |
| `GET /v2/health` | Nonsecret readiness only: owner lock, source identity, PAUSED_SAFE/serving; never account entitlement or runtime V1 health. |

Status codes: malformed400; missing/bad token401; unauthorized origin/host/project/scope403; unknown ID404; immutable-input/owner/source drift409; oversized413; eligible resource absent is a durable WAITING_RESOURCE command with admission HOLD, not synthetic success. Same `(project_id,idempotency_key)` and identical immutable input returns the same ID; changed input409 and zero dispatch.

Registry format1 contains explicit `projects` and `resources`; no request may create either. Load from outside every worker/source write root; check owner ACL/mode, canonical path and file identity before accepting it. Reject absent/duplicate/unknown IDs, defaults, Product classifications, `lari`/`lari-ui-v2`, protected runtime/backup/checkpoint/source-install roots, overlapping runtime/workspace and shared Git worktrees. The initial target must be a brand-new DISPOSABLE_SOURCE repo with its own `.git` directory and no remotes/hooks side effects.

AuthorizedProject binds project/authority ID, absolute root, initial HEAD, exact allowed source files, immutable independent test file hashes and test argv, data class, cost mode ZERO_PAID, production NO_GO, resource-profile capability hashes, expiry/revocation and budgets. It includes separate native Git authority for the owned disposable branch/index; `.git` is never a model source-write scope. Expiry/revocation is checked before every network/file/Git effect and resume. Trusted owner renewal may extend time only with the SAME capability digest and a new observation receipt; scope/test/root/resource expansion is not renewal. Request data cannot renew anything.

All path components must be normalized and portable: case-insensitive Git metadata, Windows reserved device names/ADS/trailing dots/spaces, symlinks/junctions/reparse points, hard-linked writable files and directory-as-file scopes are denied. Check handles/file identity again immediately before publish; lexical validation does not prove absence of races or OS isolation. Source foundation now rejects the observed `.GIT/config`, `NUL.py`, trailing-dot and hard-link cases; actual Windows behavior is still an independent test boundary.

Disabled example (illustrative values are NOT a live grant):

```json
{"schema_version":1,"projects":[{"project_id":"aos-v2-disposable","enabled":false,"classification":"DISPOSABLE_SOURCE","authority_id":"AOS-V2-DISPOSABLE-ONLY","workspace":null,"base_sha":null,"write_files":["src/discount.py","src/tax.py"],"criteria":[{"id":"discount-correct","test_id":"discount"},{"id":"tax-correct","test_id":"tax"}],"resources":[],"cost_mode":"ZERO_PAID","production":"NO_GO","expires_at":null}],"resources":[]}
```

## 3. One owner, schema2 and state machine

Use the SAME journal SQLite file (WAL, synchronous FULL, local disk). Extend schema1 to schema2; no second task state owner. Add immutable plan rows, append-only attempts/verification rows and one engine_meta owner record. Commands gain immutable contract JSON, mutable pause/cancel control JSON and owner epoch; tasks gain effect phase, fence, expected parent, prepared tree, commit SHA and effect JSON. Change INSERTs to explicit column lists. All schema1 fields/receipts remain readable. Do not rewrite accepted history or assign unverifiable old records fresh acceptance.

Migration: inspect marker/version/schema read-only BEFORE WAL/schema writes; refuse unknown/corrupt versions. Snapshot SQLite through its backup API with checksum, migrate a disposable copy transactionally, validate counts/identities/foreign keys and rerun old schema1 cases. Opening schema2 with the old v1 writer must fail closed. Rollback uses preserved copy and exact old source, never down-migrates the only DB. Protected V1 files are not migration inputs.

One process obtains an exclusive kernel owner lock for the complete home lifetime: POSIX `flock(LOCK_EX|LOCK_NB)`; Windows `LockFileEx` equivalent. Store PID, birth identity, executable hash, source identity, home identity and a random nonce; PID alone never authorizes stop/resume. Increment journal owner_epoch only while holding this lock. Every mutation and publish checks that epoch and expected revision. No time-based lease stealing: a still-live or suspended owner retains its kernel lock; replacement returns OWNER_BUSY. Process death releases the lock. Only the owner process publishes Git/source; sandbox children never have those permissions. This removes the stale mutable-child-writer problem rather than hiding it behind heartbeat timestamps.

States retain the existing contract: QUEUED→PLANNING→RUNNING→VERIFYING→PROJECT_COMPLETE; RECOVERING enters only through trusted recovery. HELD states PAUSED_SAFE/WAITING_RESOURCE/WAITING_AUTHORITY/HUMAN_REQUIRED have admission HOLD. Terminal PROJECT_COMPLETE/FAILED/CANCELLED/SUPERSEDED have CLOSED admission. Do not invent a second RUNNING taxonomy. CANCEL_REQUESTED and EXPORT_PENDING are control/delivery views, not alternate authoritative command states.

Add `claim_recovery(command_id, expected_revision, owner_epoch, observation_ref)` for held/inflight tasks: it enters RECOVERING/ACTIVE only after fresh trusted checks. Do not route this through ordinary HELD→QUEUED, which intentionally blocks uncertain RUNNING tasks in the current foundation. Reconciliation preserves task IDs. A normal held task with no uncertain effect may queue under the same plan; accepted artifacts are rechecked and skipped. Cancellation cannot finalize while an unfenced source writer may remain. A pending bounded network request may take up to its120s timeout; operator shows this, not immediate fictitious cancellation.

Terminal finalize is the existing single transaction: command/result+CLOSED admission+one run.finished+outbox. Success requires nonempty accepted mutation tasks and every independently verified goal criterion. Whole-project canonical STATE status is irrelevant. Repeated finalize is an immutable conflict or same-payload observation, never another finished event.

## 4. AOS-owned plan and resource policy

The actual eligible reasoning profile generates one plan; evaluator supplies faulty sources, behavior tests and user goal, never fixes/tasks/completed lists. Plan JSON1 has `goal_digest`, `base_fingerprint`, `tasks`; each node has exactly `id`, `objective`, `write_files`, `depends_on`, `criterion_ids`. MVP node kind is bounded source edit; deterministic reads/tests/Git are engine steps. Validate unique IDs, acyclic dependencies, scope/criterion coverage, nonempty graph and budgets before persisting immutable canonical JSON+SHA. Maximum16nodes; reject plans exceeding the call budget. No arbitrary tool names/argv/authority or task-generated acceptance criteria.

For the authentic ordered two-function goal, require separate coverage of discount and tax with a dependency/barrier cut. A task that fixes both before the first observable accepted milestone is invalid for this test. This is a user-goal constraint, not a ready-made implementation plan. Resume reads the stored plan; it does not call a planner again for already accepted work.

ResourceGateway handles BOTH planning and edits. Eligibility precedes scoring: enabled trusted exact endpoint/provider/model; real FREE/INCLUDED entitlement and disabled overage proof; current quota/health; credential PRESENT; supported JSON/code/context capability; authorized data class; circuit not open. Unknown cost, paid fields/endpoints, missing entitlement/auth and incompatible models have zero backend calls. Never scan another app's credentials or auto-install/login a provider.

ResourceProfile1 keys are exactly id, enabled, provider, model, protocol, base_url, credential_ref, capabilities, context_window, token_count_method, entitlement, quota, health, allowed_data_classes and circuit. Entitlement binds FREE|INCLUDED, hard_no_overage=true, evidence_ref and observed/expires UTC; maximum freshness24h. Quota and health observations expire after5min. UNKNOWN/missing/expired observations deny. The owner may supply current account/dashboard proof; no unbounded inference probes are allowed. Only HTTPS exact allowlisted direct-provider origins, no URL credentials/query secrets, no redirect downgrade or unexpected host; optional loopback gateways remain disabled. Credentials are resolved from the explicit server-local reference only. The real acceptance profile's data class is PUBLIC_DISPOSABLE_CODE; private project source needs its own explicit provider/data authorization.

MVP protocol is OpenAI-compatible `chat.completions.create` with SDK `max_retries=0`, no tools, redirects to a different endpoint disallowed, no background alternate-model catch-all. Resource SDK object is built only after eligibility. Native deterministic work has zero model attempts. Stable ranking among eligible profiles: adequate capability first, then fresh remaining quota, fit/context headroom, observed latency, finally registry ID. Cost is a gate, never a score that trades safety for speed. Unknown values cannot outrank known adequate resources.

Per default goal: max8 total network attempts, max1800active seconds, max64KiB context bytes and max16KiB output bytes per response. Actual two-function acceptance profile tightens this to **max4 attempts total**, max32KiB context bytes and max8KiB output bytes per response: nominal one plan+two edit calls, one bounded compatible-resource fallback available. Every attempted network send reserves its budget durably first, including failed/unknown-outcome calls. SDK retries are0. Per request timeout120s; test timeout30s. Context must fit the verified model profile using its supported token-count method; use byte upper bounds plus explicit protocol reserve only for a verified compatible tokenizer. Missing fit evidence is unsuitable, not an invitation to truncate source silently.

Record attempt ID, command/task, purpose, profile/model/endpoint ID, request SHA, sanitized response ID, token usage or UNKNOWN, finish reason, normalized allowed quota headers, latency and evidence reference. Keep credentials/prompts out of public logs. A free label or local zero counter is not independent financial proof; billed amount remains UNKNOWN without account evidence. No REAL_E2E_PASS if required no-paid evidence is missing.

Error policy: SECURITY/DATA/COST_UNKNOWN→WAITING_AUTHORITY or HUMAN_REQUIRED with no route relaxation; AUTH→profile unavailable and precise credential blocker; QUOTA/429→honor retry-after and use at most the one already-authorized compatible alternative within budget, otherwise WAITING_RESOURCE; transport/5xx→same bounded alternative policy; malformed/oversized/truncated model JSON→HOLD with first raw sanitized evidence, no hidden second model. Compatible fallback rehydrates AOS-owned plan/task/source context, not opaque provider conversation state. Never rotate accounts to evade service limits.

## 5. Exact edit, sandbox and Git effect transaction

Model edit response is JSON with task_id, pre-fingerprint and file entries `{path, expected_sha256, edits:[{old,new}]}` only. No command, receipt, test, authority or success fields. Use the existing **pure** `extensions/autonomy-fabric/exact_edit_engine.py:apply_exact_edits(original_bytes, edits, expected_sha256)` on staged bytes. Its anchor/hash/newline/overlap checks remain authoritative. Read source only from the task's allowed file list; initial source/test/HEAD/index fingerprints must match the immutable contract. No remote fetch/push, hooks, fsmonitor, external filters or arbitrary shell commands during an actual goal.

Task durable phases (same task row): INTENT→PREPARED→VERIFIED→COMMIT_INTENT→REF_PUBLISHED→MATERIALIZED→ACCEPTED. Never infer completion from phase text alone; reconcile actual bytes/trees/commits/tests.

1. Persist intent with command/task/epoch, parent HEAD/ref/index hash, immutable tests and pre-file hashes BEFORE model/edit/Git effects.
2. Build candidate Git blobs/tree using a private index under V2 home; original repo source/index are untouched. Store candidate bytes, patch/effect manifest and hashes durably. Candidate cannot contain paths outside the approved write set.
3. Execute trusted tests against that exact candidate snapshot in the OS sandbox below. Worker success text is not a verification receipt. Preserve failed candidate/logs; no reset or silent extra edit call.
4. Create the commit object with fixed message/parent and command/task/fence trailers through plumbing; store exact commit SHA and prepared index hash as COMMIT_INTENT. Commit object creation does not publish the branch.
5. With owner lock and freshly checked epoch/grant/source/index, acquire standard index.lock exclusively. It must be new, or a previously OWNED lock whose hash/intent is independently reconciled. Publish only the pinned owned branch with compare-and-swap `git update-ref <owned-ref> <commit> <expected-parent>`; never force/HEAD guessing. Disable hooks/fsmonitor on native Git invocations. No unrelated staged files are allowed.
6. Atomically materialize only intended source files from the committed tree; each current hash must equal its pre- or post-hash. Complete the prepared index lock→index rename only if actual old/new index hashes match the intent. No blanket checkout/reset/clean. Crash between ref/source/index writes is reconciled from persisted hashes, not hidden.
7. Independently verify actual branch/tree/source/index and immutable tests in a fresh sandbox observation, then accept the task atomically with receipt references. Completed tasks on restart get read-only hash/test verification and **zero repeated model/edit/commit**.

| Observation on restart | Decision |
|---|---|
| No published commit, all source/index pre-hashes, no candidate | NO_EFFECT_PROVEN; same task may continue within remaining budget after trusted recovery, not blind replay |
| Prepared candidate/test proof exists, parent/source pre-hashes | Revalidate and continue the stored candidate; no model regeneration |
| Exact intended commit published, source/index partly pre/partly post | Finish only proven missing materialization from that commit; no new commit or model call |
| Exact intended commit+source/index+tests verified, receipt absent | Reconstruct the trusted observation and accept the existing effect once |
| Already ACCEPTED and all post-hashes/commit/test identities agree | Skip mutation; append only a read-only recovery observation |
| Unknown lock, unexpected HEAD/ref/index/hash/path/test drift | HUMAN_REQUIRED; preserve every byte and pending intent; no cleanup/reset/replay |

**OS execution isolation is mandatory before generated code runs.** Path validation/sentinels are not an OS sandbox. Server holds provider credentials and Git publishing rights; test worker gets neither. The candidate project/tests are read-only, `.git`, registry, tokens, user profile, protected roots and runtime journal are not mounted/granted; only private scratch/log outputs are writable. Minimal allowlisted environment, pinned interpreter hash, explicit argv, `-I -B` for Python tests, no shell. Limit worker memory512MiB, test runtime30s, output256KiB per stream, one active test process, no child process creation and no network including broker loopback. Gate launch on measured ≥1GiB available RAM for this small profile; no local model startup. These are configured limits, not measured performance claims.

Windows-first implementation: ctypes AppContainer profile with no network capabilities, explicit read-only candidate/runtime ACLs, private scratch ACL and Job Object with KILL_ON_JOB_CLOSE, memory/process limits. No loopback exemption, global ACL weakening or reused user credential token. Linux adapter: installed trusted bubblewrap with isolated user/mount/PID/network namespaces, read-only runtime/project mounts, no home/broker state, scratch-only write mount and seccomp process/network denial. Validate actual host support first. Unsupported isolation means WAITING_RESOURCE/EXECUTION_ISOLATION_UNAVAILABLE before generated code; there is no same-user unsandboxed fallback. Native source/storage unit tests do not establish these OS boundaries.

Real negative sandbox tests must attempt outside writes, registry/token reads, loopback/external network and child spawning and prove denial, while the trusted simple Python tests pass. Windows and Linux claims are separate. Add V2-only Linux/Windows CI checks; unavailable native isolation cannot be replaced with a mocked PASS. Microsoft documentation establishes the mechanism, not this implementation: https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation and https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer . Bubblewrap reference: https://github.com/containers/bubblewrap . Controller inference: these enforce the required execution boundary with less footprint than a mandatory local VM.

## 6. Independent acceptance, KCP and delivery

Goal contract is server-owned criteria/test IDs+argv+file hashes and the submitted goal digest. `verify_task` and `verify_goal` ignore model/worker completion claims, execute those exact immutable tests in a fresh sandbox against observed artifacts and bind output/exit/commit/tree/file hashes. A green canonical governance ledger does not satisfy a goal predicate. Empty plans or Python merely exiting0 without the required predicate evidence cannot finalize success.

Outbox consumer writes deterministic result/receipt files under `<home>/commands/<id>/`, fsyncs/atomically publishes and reads back hashes. It also calls `aos.knowledge.receipts.record_implementation_receipt` / `record_verification_receipt` with an explicitly injected `KnowledgeLedger(<THIS_V2_HOME>/knowledge)` only; never environment/cwd/default V1 resolution. Stable idempotency keys bind command/task/result digest; use native event validation. `KnowledgeLedger.verify()` and expected receipt-file readback must agree before outbox acknowledgement. KCP receipt-file/index loss after a preexisting append must be reconciled from the existing event; do not append a second event or claim the helper's early idempotent return repaired missing exports.

If export fails, SQLite remains terminal/CLOSED but delivery view is EXPORT_PENDING. Restart retries only idempotent exports, no model/edit/commit. UI shows result path and pending reason. Local process-death recovery is the claimed target; power-loss/network-filesystem durability and off-device recovery require separate actual tests.

## 7. One real vertical slice and observer boundary

Keep the existing evaluator's initial two faulty files: `src/discount.py` and `src/tax.py`; independent `test_discount.py`, `test_tax.py`; outside sentinel; initial Git commit and disabled registry template. Both tests initially exit nonzero. Requested goal: repair percentage discount, then percentage tax, preserve public signatures/tests, and support a verified milestone after the first function before the second executes. Never include ready-made fixes or completed task lists.

Before admission only, evaluator prepares fixture, snapshots immutable tests/sentinel and uncapped protected51/admissions roots resolved from actual read-only host configuration. After API submission evaluator loses all source/Git/journal write capability. It may write only its own new observation files outside those trees and stop/restart its OWN verified engine process using real handles/nonce/birth/source identity. It may not create receipts, edit checkpoints or hot-patch installed engine code.

Keep two actors in the harness: the operator client owns the already-authorized goal submission, process lifecycle and same-command authenticated resume; the independent observer has no API bearer or source/journal write capability and validates disk/API read observations. Operator control requests are recorded separately and cannot create verification receipts. Combining these roles into one script is not permission for the observer to fix state or declare its own acceptance.

Server-owned disposable test profile enables a dispatch barrier after discount's independent accepted receipt and before tax dispatch. It records PAUSED_SAFE and waits; it does not supply completion information. Observer sees real discount commit/posthash/test receipt and tax's original faulty hash, terminates the owned engine process, starts a new process with the exact immutable source/home and resumes the SAME command. No first-task model/edit/commit replay; tax then completes, both immutable tests pass, result/run.finished/CLOSED admission/export ack and protected/sentinel before/after equality are independently observed.

WP3 must implement `scripts/aos_v2_evaluator.py run` with these actual process/API operations, not a stub. Arguments remain `--root`, `--aos-source-sha`, `--registry`, `--runtime-home`, `--protected-state-root`; all absolute and validated. `--offline-contract` is separate and every output is SIMULATED. Missing provider/entitlement/host/isolation evidence records RESOURCE_WAIT before the blocked action; absent Windows/protected51 observation cannot be turned into full REAL_E2E_PASS by saying this Linux session did not touch it.

Outcome schema1: outcome REAL_E2E_PASS|RESOURCE_WAIT|FAIL_REPLAN; command/source/install/registry/authority identities; actual model attempt/response/usage/entitlement evidence; preinterrupt and postrestart process/commit/hash/test records; same-command/no-replay proof; final state/admission/event/outbox/KCP receipt IDs; uncapped protected manifests; limitations and one next action. Raw evidence hashes are mandatory. CI/storage mocks alone cannot populate real-worker fields.

## 8. WP execution and stop rules

WP1: source foundation and path-scope correction,14 local tests; source-only acceptance requires exact remote source/full CI review. Do not recreate it. WP2: implement the above registry/API/engine/gateway/bridge/sandbox/acceptance, migration and failure matrix, then independent review; revised estimate16–28focused hours, Windows isolation uncertainty explicit. WP3: operator/evaluator integration5–9h. WP4: one actual interrupted free-resource goal2–4h plus real resource/host waits. WP5: backup/restore and separately approved rollout3–6h plus owner gates. These replace earlier lower WP2 effort estimates; same five WP IDs/DAG and no progress percentage invented.

WP2 tests: forged/absent/expired/revoked registry; project/root/test/body injection; unknown/paid profile zero calls; SDK retries counted; malformed/truncated response; empty/cyclic/over-budget plan; actual link/hard-link/Windows alias denial; owner-lock contention and stale epoch; unrelated index/source drift; real child death at each effect phase; same task reconciliation; sandbox escape denials; missing KCP/result export and idempotent repair. Run source focused matrix plus existing full exact-head CI. WP3 adds no-worker negative control, actual owned-process kill/restart barrier, wrong nonce/PID denial, unchanged tests/observer read-only and export-pending recovery.

Preserve the first independent failure, allow one Controller-approved bounded root-cause correction/retest within the SAME WP. A later implementation failure at that boundary is FAIL_REPLAN; resource/permission unavailability is not a code failure. No criteria/test weakening, fake success, hidden probe tasks or new stage names. Optional resources stay frozen. No executor changes protected V1/51, LARI/Codex, production, startup, paid policy or repository visibility. Live activation/backup/cutover commands remain the separately gated WP5 procedure and require fresh exact host/source/backup/health evidence.

**One next source action:** implement the existing WP2 packet at its accepted predecessor and this exact contract. No architectural choices are delegated; unresolved external facts remain explicit gates.
