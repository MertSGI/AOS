# Exact next resume action - 2026-10-13

Do not repeat the root-cause investigation, protected activation attempts, or completed local/hosted tests.

## First operation

1. Read back this checkpoint and verify the protected runtime is still exact SHA `85ee6772b02a0ab72f5c79633672083017380269`, `HEALTHY`, `PAUSED_SAFE`, `active=stable`, with zero ACTIVE admissions and zero live workers.
2. Verify local and remote `fix/aos-supervisor-hotswitch-20261012` still equal `edaab3890e7584024ebc499230f9dd7edd31ccff` and the repair worktree is clean.
3. Continue the already-created `C:\Projects\AOS-hotswitch-runtime-20261012-02`. It currently contains directories and a copied knowledge tree only. Do not recreate or delete it.
4. Locate the actual materialization entrypoint with `rg --files | rg "materialize.*slot|slot.*materialize"`; the attempted path `scripts/materialize_slot.py` was wrong.
5. In the isolated runtime only, create sanitized `NO_GO` configs with unique ports/singleton; materialize stable `85ee6772...` and candidate `edaab389...`; append genuine isolated implementation and verification receipts grounded in commit `edaab389...`, the four-file diff, local test results, and CI `38305877797`; then run official stage/validate.
6. Start stable `85ee6772...` in `PAUSED_SAFE`; run one real official activation of `edaab389...`; prove correlated supervisor handoff, no unexpected exit diagnostic, exact-SHA paused runtime/panel health, durable transition completion, restart/re-entry without duplicated work, and supported rollback readiness.
7. If any proof fails, stop isolated execution and checkpoint it. Do not touch protected state.
8. If all proofs pass, publish a new exact-SHA authorization request. Protected KCP append, activation, unpause, and LARI admission still require fresh explicit operator authorization.

## LARI sequence after future authorization only

- Product `lari` first: control `14589d3a68a1654281ceeec2f3b9445fbbaa807a`, base `06ebf176e411faf6820ded34987f894cc5e7dc9e`, Phase 7 Node 3 R3 visual/browser validation.
- Only after Product has genuine terminal execution and verification evidence, run `lari-ui-v2` from a fresh isolated worktree based on `06ebf176...` while preserving `C:\Projects\LARI-ui-v2-post-wp5-sync` unchanged.

No global resume, historical command restart, Supabase migration, production deployment, canonical acceptance change, or paid fallback is authorized.
