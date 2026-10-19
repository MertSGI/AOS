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

WP_ID: WP3. Prerequisite/entry: exact independently accepted WP2 SHA in canonical STATE; null/unverified => HOLD. Branch feature/aos-v2-operator-proof-20261018, one isolated worktree. Effort 5–9h; native tools, no production/provider entitlement changes. Owner AG-AOS; independent verifier evaluator owner/Controller. Do not redo WP1/WP2 architecture.

Authorized files: src/aos/v2/operator.py, the minimal /v2 entrypoint additions, scripts/aos_v2_evaluator.py, tests/test_aos_v2_vertical_contract.py, docs/aos-v2 operator/acceptance references. Existing control_panel.py fallback remains V1 only. No generic dashboard rewrite, paid SDK, multi-PC/council/self-repair activation or Product work.

Implement one operator entrypoint: `python -m aos.v2 --home ABS_HOME operator` to display explicit registry project, goal, cost=ZERO_PAID, authorized root/source paths/tests, selected resource + rejection reasons, state/admission, observed PID/nonce/source identity, verified task progress, resource/authority blocker, output path, export status. A compact loopback page or CLI is adequate. No default Product. It calls the REAL authenticated V2 API; it cannot write journal status directly. Run/install is one command after env provisioning, startup disabled/PAUSED_SAFE.

Finish independent evaluator orchestration around the implemented `prepare/snapshot/compare/inspect` tools. Prepare creates only initially broken discount/tax plus immutable independent tests/sentinels and initial Git commit; the disabled registry template is not authority. Add an actual process controller that records supervisor/worker binary/source/nonce/parent/birth identity and only stops its OWN isolated processes. Test profile supports an observable dispatch barrier immediately after the first real task is durably verified/committed/accepted and before task2 side effects. Barrier may not write completed IDs, source fixes, receipts or checkpoints. Observer source write capability ends at admission. No worker/harness may hot-edit installed engine source.

Real protocol: one API submission; actual model generation and worker edit/test/commit for discount; observer snapshot including tax still broken; controlled owner process stop; restart from same isolated home+command ID; discount posthash/trailer/test reconcile with zero repeat model/edit/commit; tax completes; evaluator independently tests both; exact same test/sentinel hashes; one terminal run.finished/result/verification receipt+CLOSED admission+outbox acknowledged. All external provider/billing facts remain UNKNOWN unless separately proven. Missing eligible real resource => exit RESOURCE_WAIT before model call; offline injected run is SIMULATED, never REAL_E2E_PASS.

Add CLI `python scripts/aos_v2_evaluator.py run --root BRAND_NEW_ROOT --aos-source-sha EXACT_ACCEPTED_SHA --registry SERVER_OWNED_CONFIG --runtime-home BRAND_NEW_V2_HOME --protected-state-root READ_ONLY_VERIFIED_V1_STATE_ROOT`. `run` must not exist as a fake stub: implement or return an explicit unavailable error. It must snapshot all original 51 command files and admission registry (uncapped; original collector's 30 checkpoint cap is insufficient) before/after, outside files hash only. Do not require unknown host paths: resolve root from inspected actual host config and ask only for necessary access. `--offline-contract` separate mode can exercise source contracts but must visibly label every output SIMULATED.

Artifact schema: command/source+installed asset hashes; registry+entitlement refs; sanitized request/response IDs/usage; actual PIDs/nonces; preinterrupt first-task commit/posthash and untouched tax hash; postrestart samecommand; test argv/exit/log digests; Git changed paths/commits; accepted receipts; result/events/admission/outbox; before/after protected manifests; call/paid accounting evidence; limitations. Fixture receipts cannot replace real model responses or financial proof. Observer may write only its own new evidence files after admission, not source/Git/checkpoint.

Tests: missing resource early wait with zero calls; no-worker fails both initial tests; unauthorized token/project/grant; barrier sequencing; wrong-PID nonce kill denied; stale accepted artifact holds; process death real subprocess, not only server.shutdown in one Python process; final export failure visible and idempotent restart; observer cannot repair sources. Commands: `python -m pytest -q tests/test_aos_v2_vertical_contract.py tests/test_aos_v2_lifecycle.py`; `git diff --check`; existing exact-SHA CI. One focused repair maximum after first independently recorded failure. Exit: operationally runnable harness, operator flow, independent source review; keep REAL_E2E status NOT_RUN until WP4.


Operator/evaluator implementation must follow AOS-V2-SEAM-1 contract sections2,6,7: use discovered port0 endpoint and private local token; no operator direct journal status writes; server-owned post-discount dispatch barrier; only owned handle/nonce/birth-verified process stop; same command and zero first-task mutation/model/commit replay; actual source/index/receipt/export observation; evaluator read-only after admission. Preserve the actual original two files/tests; missing protected51/host/isolation proof remains an explicit gate.


Source acceptance receipt: AOS-SOURCE-CI-20261019-01. Exact source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506, CI39204426295/job117904058408 SUCCESS; checkout, all steps and full decoded log summaries independently inspected. Offline/full canonical1635passed,11skipped,26deselected; Postgres25passed. WP1 is accepted SOURCE_FOUNDATION only. Final containing control-head CI is distinct and must be observed before integration. Real task E2E remains NOT_RUN.
