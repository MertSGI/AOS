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

WP_ID: WP5. STATUS: BLOCKED_PENDING_WP4_AND_OWNER_LIVE_APPROVAL. Entry exact WP4 accepted source+CI and source/installed manifest; no prefilled future SHA/run/transaction. Effort 3–6h plus owner gates. Owner AG-AOS implementation operations; independent verifier AOS Controller + owner. This packet is a reviewable procedure, NOT permission to activate protected runtime.

Source work may complete in a new isolated branch: versioned backup/restore helpers using SQLite backup API, candidate packaging/import/entrypoint manifest, opt-in launcher with initial PAUSED_SAFE and no implicit Product/grant, read-only legacy view, exact rollback recipes and Windows ownership tests. Scope src/aos/v2 packaging helpers, tests/test_aos_v2_restore.py and documentation. Do not alter live installation or old state, cleanup 79 worktrees, migrate historic commands, or install startup while building the candidate.

Disposable restore acceptance: quiesce only the isolated V2 engine; obtain consistent SQLite backup including WAL semantics, receipt/resource export manifest and nonsecret registry version; checksum source+snapshot; restore into a new home on a compatible local disk; new token/nonce, PAUSED_SAFE, integrity_check; same accepted tasks+Git/artifact/receipt hashes; pending effects reconcile not replay; pending outbox exports once; original V1 history still byte-identical. Off-device encrypted backup and recovery custody are separate owner actions, not satisfied by same-C-drive bundle. Test true restore, not only copied-file hash.

Prepare exact concrete owner approval card: source SHA, CI run/head, installed candidate hash, verified protected baseline and backup identity, target home/port/slot, expected downtime, commands, rollback transaction discovery, zero paid/production/startup effects and separate gates. Ask for exactly the missing live action only after this source/restore work is complete. Existing consent for isolated source does not cover live protected mutation.

If and only if separately authorized: fresh protected read-only source/process/nonce/242hash/51 commands/admission/KCP snapshot; native quiesce; stage exact accepted source; validate import/asset identity; maintenance activate --without-startup with observed real transaction; verify API/panel/nonce/source/slot health; separate proof-bound promotion; only fresh authorized new goal resume, never old Product commands. Rollback is proven only after restored old supervisor/API/health/identity are observed; pointer metadata alone fails. No auto-merge main, production, startup or billing changes.

Tests/exit: disposable backup+restore/process-death and negative wrong-root/source/schema/version cases, no secret export, pre/post 51 preservation, exact-source CI, independent Windows health observation where available. Return separate SOURCE_READY_FOR_REVIEW, DISPOSABLE_RESTORE_PASS, LIVE_CUTOVER_NOT_AUTHORIZED / actual authorized result. Record every issued/consumed authority and one next action; do not conflate them.


Source acceptance receipt: AOS-SOURCE-CI-20261019-01. Exact source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506, CI39204426295/job117904058408 SUCCESS; checkout, all steps and full decoded log summaries independently inspected. Offline/full canonical1635passed,11skipped,26deselected; Postgres25passed. WP1 is accepted SOURCE_FOUNDATION only. Final containing control-head CI is distinct and must be observed before integration. Real task E2E remains NOT_RUN.
