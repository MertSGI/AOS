# Resource OS Evidence

Track: `AOS-RESOURCE-OS-MASTER-20260923-01`

## Phase 0 source evidence

- `git fetch origin --prune`: passed on 2026-09-23.
- `git ls-remote origin refs/heads/feature/aos-freellmapi-local-meta-provider-20260922-01`: `ccdebfd7b5bdc32f7e95634ef2c464293356c872`.
- Accepted `docs/codex/freellmapi-integration/STATE.md`: `STATUS=CONTROLLER_REVIEW_READY`.
- Accepted `docs/codex/freellmapi-integration/STATE.json`: `CONTROLLER_REVIEW_READY=true`, full canonical suite `982 passed, 8 skipped, 26 deselected`.
- Isolated worktree branch created directly at the accepted SHA.
- Mandatory runtime preflight read from `origin/audit/aos-resource-os-runtime-preflight-20260923-01` at `d0ea8064f2d349e0b63c1fe5009a4901981464a8`.
- Mandatory Codex CLI preflight read from `origin/audit/aos-codex-cli-backend-preflight-20260923-01` at `66d38917f60d17eedc1fa370649db1ddf8045681`.
- All six required preflight documents were read completely from those exact remote heads before implementation.
- `origin/audit/aos-jev-decision-layer-preflight-20260923-01` was absent at Phase 0; the local placeholder branch had no Jev implementation state, so Jev remains optional and unconsumed.
- Existing Antigravity worker/probe/adapter and autonomy-fabric registry/supervisor/coordinator/backend/router surfaces were inventoried for reuse.

## Safety evidence

- The original `C:\Projects\AOS` checkout contains unrelated user work and was not modified by this track.
- No runtime, provider, Antigravity, Codex, Qwen, or Jev live execution has been performed.
- No protected command ID or protected workspace has been read for mutation or changed.
- No credential, API key, paid endpoint, deployment, or production action has been used.

## Phase checkpoints

### Phase 0 — canonical program controls

- Verified the accepted FreeLLMAPI remote head and both mandatory audit remote heads after a fresh origin fetch.
- Verified the accepted FreeLLMAPI durable state reports `CONTROLLER_REVIEW_READY`.
- Verified the isolated Resource OS branch starts exactly at the accepted SHA and did not modify that source branch.
- Parsed `RESOURCE_OS_STATE.json` successfully with PowerShell `ConvertFrom-Json`.
- `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

Later phase checkpoint commits, focused commands/results, remote equality, and disposable proofs are appended here as they are accepted.

### R1 — provider observations and task-class health

- Added immutable, validated, sanitized `RateLimitObservation` plus closed task, source, failure-family, and contract-subtype enums.
- `Retry-After` delta/date and allowlisted numeric rate metadata survive adapter, attempt journal, circuit, and probe boundaries without raw headers/bodies or provider text.
- Provider contract failures preserve one of eight typed subtypes with bounded allowlisted details; known failure messages remain absent from attempt evidence.
- Circuit schema `2.0.0` stores task/model health records and independent family streaks; quota/rate/credit does not increment health streaks.
- Actual requests and probes declare a task class; `small_reasoning` evidence cannot clear or wake `structured_planning`, `repo_ui_planning`, `large_context`, or `agentic_execution` work.
- Focused provider/runtime suite: `166 passed, 1 deselected in 14.68s`.
- Planning-kernel/worker regression: `57 passed in 31.41s`.
- Python compileall: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.
