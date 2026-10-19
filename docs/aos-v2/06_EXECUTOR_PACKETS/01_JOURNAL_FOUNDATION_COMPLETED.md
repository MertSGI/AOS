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

WP_ID: WP1. STATUS: IMPLEMENTED_SOURCE_CANDIDATE. DO NOT REIMPLEMENT.
Exact source commit: 7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506; parent: 9d3168afb35edf5ad06e6b5e419801493fe04572.
Files: src/aos/v2/__init__.py, src/aos/v2/lifecycle.py, tests/test_aos_v2_lifecycle.py, scripts/aos_v2_evaluator.py.
Objective: eliminate independently reproduced ghost ACTIVE admission and duplicated event sequence without any V1 runtime changes. Local focused result: 10 passed, real abrupt subprocess exits before/after SQLite commit and concurrent DB writers included. Fixture verification refs inside tests are SIMULATED, not authentic model-task evidence. Evaluator self-check created broken functions; both independent tests exit1; no source repairs.

Reviewer task only: verify the diff at exact commit and safety boundaries; confirm no V1 module imports/registers this package; independently inspect crash/idempotency/authority denial cases. Current public API is internal LifecycleStore, not an HTTP engine. TrustedGrant is an internal binding, not a credential or proof of authorization; future API must resolve it from trusted config. Receipt references are trusted-verifier callbacks and must never be exposed as request success fields.

Commands (inside a new isolated source checkout):
```powershell
git checkout --detach 7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506
py -3 -m venv .venv-v2
$AosPy = Join-Path (Get-Location) '.venv-v2/Scripts/python.exe'
& $AosPy -m pip install -e '.[dev]'
& $AosPy -m pytest -q tests/test_aos_v2_lifecycle.py
git diff --check 9d3168afb35edf5ad06e6b5e419801493fe04572 7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506
```
No full replay of baseline B3/B7/B11 experiments. On acceptance bind WP2 entry to the delivery carrier with this code commit; otherwise identify exact source defect and hold WP2. No deployment, provider call or grant materialization.


19Oct source review correction already completed: portable Git/Windows alias and hard-link scope denials; first4negative failures preserved,14tests now pass including a clean Python3.12 venv. Remote source above matches those bytes. Inspect its exact full CI before source acceptance; do not reimplement this fix. This source proof does not close WP2 or real task E2E.


Source acceptance receipt: AOS-SOURCE-CI-20261019-01. Exact source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506, CI39204426295/job117904058408 SUCCESS; checkout, all steps and full decoded log summaries independently inspected. Offline/full canonical1635passed,11skipped,26deselected; Postgres25passed. WP1 is accepted SOURCE_FOUNDATION only. Final containing control-head CI is distinct and must be observed before integration. Real task E2E remains NOT_RUN.
