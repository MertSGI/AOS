# Copy-ready AOS execution contract

Repository: MertSGI/AOS ONLY.
Architect/integrator: AOS Controller (this decision is HYBRID_V2).
Default implementer: AG-AOS. Equivalent executor: a NEW explicitly AOS-scoped Codex session, never the separate LARI/Codex session.
Base audited main: 9d3168afb35edf5ad06e6b5e419801493fe04572. Implemented journal source: 7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506.
Delivery control branch: feature/aos-v2-thin-lifecycle-20261018. Source is now published through owner-authorized GitHub web commits; the original local commit chain is preserved separately, not misrepresented as the remote SHA. Current source 7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506; root tree3c2b658268150e75dd1723f8be2e26961a6248fd; exact four source-file bytes independently matched. Resolve and pin the actual delivery head before use. Read full contract docs/aos-v2/14_IMPLEMENTATION_CONTRACT.md at that same head; it closes concrete interfaces/algorithms and is part of this execution instruction.

Authorizing scope: the owner's 18 October 2026 Astra Master Directive and explicit request authorize isolated AOS source work and preparation of a reviewable delivery. The first public push was auto-review rejected for missing disclosure consent. The owner subsequently explicitly approved this prepared payload on the named new public feature branch; that missing-consent gate alone is superseded (AOS-OWNER-PUBLIC-20261018-01). They do NOT authorize protected Windows runtime/slots/51 historical commands, legacy worktrees/checkpoints, LARI, paid calls, production, startup, purchases, force push, destructive Git cleanup, or protected live activation. No historical command may be recovered or cloned as the test workload. A prompt/request cannot grant itself execution authority.

Required decisions: retain stable V1/tools/receipt provenance; one new V2 command owner; SQLite transactions for lifecycle/tasks/events/outbox; shared non-paid resource gateway for all model calls; explicit goal acceptance instead of entire-project canonical completion; optional AG/Qwen/FreeLLMAPI frozen until base actual vertical slice. Existing V1 code is not automatically safe to reuse: direct ModelNative transport and hard-reset rollback must be replaced at the seam. Do not merge/cherry-pick B11 e03807238818058e427598c453e391607c5a1053.

Publication status: source branch exists and CI runs are enabled normally. Owner public approval persists; no ZIP upload or repeated public-consent question is needed. Work only on this AOS repository. The live Windows/51/LARI/paid/production/startup boundaries remain unchanged.

Isolation/setup (PowerShell; select only the READY WP from canonical STATE):
```powershell
$AosCodeSha = '7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506'
$AosDeliveryBranch = 'feature/aos-v2-thin-lifecycle-20261018'
$AosTaskRoot = Join-Path $env:TEMP ('aos-v2-work-' + [guid]::NewGuid().ToString('N'))
git clone --no-checkout https://github.com/MertSGI/AOS.git $AosTaskRoot
if ($LASTEXITCODE -ne 0) { throw 'Clone failed' }
Set-Location $AosTaskRoot
git fetch origin "refs/heads/${AosDeliveryBranch}:refs/remotes/origin/${AosDeliveryBranch}"
if ($LASTEXITCODE -ne 0) { throw 'Delivery ref fetch failed' }
$AosControlSha = (git rev-parse "refs/remotes/origin/${AosDeliveryBranch}").Trim()
git merge-base --is-ancestor $AosCodeSha $AosControlSha
if ($LASTEXITCODE -ne 0) { throw 'Wrong source lineage' }
git show "${AosControlSha}:docs/project-control/STATE.json"
git diff --name-only $AosCodeSha $AosControlSha
```
Freeze the observed control SHA and inspect exact-head CI before editing. For WP2 the initial carrier may differ from AosCodeSha only in docs. Any additional source delta needs its newer accepted receipt. Later WPs use the literal predecessor accepted_head and branch from STATE; null/unverified => HOLD. Fetch that branch and verify its head, do not guess from this original source SHA. Create the package's new branch only after those checks; one worktree owner. Install the existing project in a new worktree venv (`py -3.12 -m venv .venv`; `.\.venv\Scripts\python.exe -m pip install -e ".[dev,postgres,browser]"`). No active runtime source edit.

Common verification and stopping: inspect first failure and preserve raw logs; at most ONE bounded root-cause correction/retest inside the same WP. Another same-class failure => STOP/REPLAN, not a new named phase. Mock/deterministic responses validate contracts only. Never produce LIVE/REAL_E2E_PASS from injected providers or preset completed lists. Never edit tests/criteria to excuse a failure. Return a truthful partial result when external resources are unavailable and continue unrelated authorized source work.

Handoff schema (JSON; no secrets): WP_ID, status=SOURCE_READY_FOR_REVIEW|BLOCKED|FAIL_REPLAN, repo, branch, entry_sha, head_sha, changed_paths, commands[argv,exit_code,artifact_sha256], CI[id,head_sha,status,conclusion] or UNKNOWN, proof_class, receipt_ids, actual_worker_invoked, model_calls, protected_host_access, protected_host_mutations, authority_scope, authority_consumption, blockers, next_safe_action. Controller alone changes accepted_head/acceptance status. Prepared-payload public-disclosure consent is recorded. Use normal push to the named new package branch, then inspect exact-SHA existing CI; never push main, change visibility or auto-merge. Source CI is not task E2E.

WP_ID: WP4. Prerequisite: WP3 accepted exact source and CI, runnable evaluator, explicit new disposable project, actual resource authority. Entry SHA must be bound from STATE accepted_head, never moving main. No code changes are expected; this is one controlled acceptance run. Effort 2–4h excluding account availability. Worker = actual eligible free provider + AOS bounded/native worker. Evaluator/Controller independent from implementer. No AG invocation required, and no existing LARI/Codex resource may be consumed implicitly.

Before any call, independently verify host identity/source/installed hashes; protected 51 admission+commands uncapped hash manifest; no active historical admission; exact new runtime/project roots disjoint; authorized model endpoint/profile/free/included entitlement/overage policy and current quota; source/task/test data may legally be sent. Key stays local secure store and out of prompts/logs. Missing resource or permission: record RESOURCE_WAIT with exact missing observation, no paid fallback or installation/purchase. This is a finite attempt, no probe loops.

Run the implemented WP3 evaluator `run` once from a brand-new root using pinned installed source. It must perform the actual authenticated user flow and independently observe: real AOS plan; real non-deterministic model response; first faulty function changed/tested/committed; durable first accepted task; second function still faulty; real owned process stop and new process restart; same command recovered; first-task zero duplicate mutation/model/commit; second real function repaired; both immutable functional tests independently passed; approved changed paths; final PROJECT_COMPLETE/result.json/run.finished/verification receipts and CLOSED admission, export ack; unchanged V1 51/admission/sentinels. Use the protocol and goal fully specified in docs/aos-v2/11_PRODUCT_DEMO_ACCEPTANCE.md, but do not assume that document itself is proof.

Record real sanitized response IDs, usage/finish reason and actual permitted cost evidence. A zero in a local ledger is not account proof. Separate SOURCE_INSPECTED, SIMULATED and HISTORICAL_REPORTED facts. No evaluator fix, synthesized completed list, fake ledger, rewrites of tests/expected behavior, hot-edited installed source, manual terminal admission cleanup, or extra hidden canary commands. Do not use Product as the target.

Outcome: REAL_E2E_PASS only when every chain and trust boundary is independently observed. If entitlement/API/quota blocks: RESOURCE_WAIT, retain same command/checkpoint and actual effects for reconciliation. On concrete implementation failure preserve first raw record, permit only one Controller-reviewed focused root-cause correction in isolated source then one retest; a second implementation failure => FAIL_REPLAN and architecture reconsideration. Source correction requires new exact SHA/CI/immutable install; never edit installed candidate. Operational resume of historical lineages is never authorized by this package.

Deliver one outcome JSON with exact source/command/authority/evidence hashes and one human-readable record; update only owning AOS canonical progress after Controller review. Do not issue runtime promotion/production/startup. Rollback = stop only owned disposable processes, preserve all files/journal/Git; do not prune artifacts. Next safe action on PASS is WP5 planning/approval, on WAIT is exact missing resource observation, on FAIL is Controller replan.


Use AOS-V2-SEAM-1 section7 literally. The actual slice profile allows at most4 total network attempts and one active bounded edit at a time. Missing verified entitlement, sandbox, Windows/protected51 observation or immutable installed-source identity is RESOURCE_WAIT; it cannot be waived by a source/CI success. No third-party worker is mandatory when the approved direct free profile actually works.


Source acceptance receipt: AOS-SOURCE-CI-20261019-01. Exact source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506, CI39204426295/job117904058408 SUCCESS; checkout, all steps and full decoded log summaries independently inspected. Offline/full canonical1635passed,11skipped,26deselected; Postgres25passed. WP1 is accepted SOURCE_FOUNDATION only. Final containing control-head CI is distinct and must be observed before integration. Real task E2E remains NOT_RUN.
