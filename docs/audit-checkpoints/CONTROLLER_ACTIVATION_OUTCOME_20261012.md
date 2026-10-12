# AOS protected activation outcome — 2026-10-12

Captured after the operator-authorized bounded transaction. This document supersedes only the activation-readiness conclusion in `CONTROLLER_ACTIVATION_PREFLIGHT_20261012.md`; the canary and source-qualification evidence remains valid.

## Gate result

**ACTIVATION_GATE: FAIL_CLOSED**

Candidate `9dddf1e202da8e227adca492ba1c5020961c65b3` passed exact-SHA CI, protected KCP receipt coverage, official stage, and official validate, but it did not pass the protected exact-SHA paused health proof. Two official activation transactions failed with:

```text
AOS_RUNTIME_DEPLOY_HOLD: Activation exact-SHA paused health proof failed
```

Both transactions were automatically restored and durably aborted:

| Attempt | Transaction | Transition | Result |
|---|---|---|---|
| 1 | `activate-1791832355-43421ab3` | `6cffe9380c884763cb1482a7672a4af8371ab2270ea6f734b91739ef50ee1a89` | `ABORTED`; stable slot restored |
| 2 | `activate-1791832629-72aad81a` | `ba16348796cb01f5d9fadcd7cb9976ca455e0605424f1a92fdd36e5d09dc27f6` | `ABORTED`; stable slot restored |

No LARI command was admitted. No global resume was issued.

## Confirmed protected KCP evidence

- Implementation receipt: `4bf16f279dee53369777afce620a8856c6fc251326b2aa5a64ada60442866f1a`
- Operator-grounded verification receipt: `ef8e10f8c872bf1d1534eeca7ec2e8678e6b6119ebc809a9a830704720e08a25`
- Official candidate validation receipt: `42af27870169d66ca355e545d350052d3bd2aa57e4638e8ecd56c6d0a0166ff2`
- Official materialization receipt: `4d5789732e085952f2e41e785ea8974572171bf7ca1d2999aefd73a77d17ca41`
- Candidate tree: `21ae2aad3e2fadaaa071e5d3c296a1717f20e8b445d4868185490420500a54ad`
- Hosted CI: run `38259843751`, exact head `9dddf1e...`, `completed/success`
- The two unreproducible historical canary artifact hashes were not used as acceptance evidence.

At the post-failure readback, the ledger was `READY`, sequence `548`, with no unresolved runtime transitions. `PRODUCTION=NO_GO` and `PAID_FALLBACK=DISABLED` remained intact.

## Rollback and current runtime truth

- Active/stable source: `85ee6772b02a0ab72f5c79633672083017380269`
- Slot: `candidate-runtime-v1.8-85ee6772b02a`
- Runtime: `HEALTHY`
- Maintenance: `PAUSED_SAFE`
- Startup authority: absent
- AG backend: disabled
- Command admissions: 12 `HOLD`, 12 `SUPERSEDED`, 0 `ACTIVE`
- Stable supervisor was relaunched through the official candidate entrypoint after each rollback and its health was read back at exact SHA `85ee6772...`.

The original trial rollback requested by the operator was recorded as transition `18522cff3879ee0b251f19bfcd935b52f5e2e689a19727b195b7340c21a42686`. That historical transaction restored a stale runtime config, so the supported older rollback transaction `activate-1791546818-e853496c` was used to restore the config and slot consistently to `85ee6772...`; its rollback transition is `1f127e37474bca0385b23378af70244823d18b6f48d83469d3fc973d1ad17c64`. The restored AOS-owned Startup file was removed again to honor `--without-startup`; its recovery copy remains in the transaction backup.

## Activation failure diagnosis

The candidate did not emit a `RunStatus` traceback and the canary repair did not regress. In both protected attempts, the running stable supervisor exited during the slot hot-switch before candidate health could be established. The supervisor state ended as `SHUTDOWN` on stable `85ee6772...`; no candidate `HEALTHY` state was observed.

The precise internal exception is not recoverable from current logs because `runtime_supervisor.main()` converts any exception to exit code 1 and the production `pythonw.exe` launch routes stdout/stderr to `DEVNULL`. This is therefore a proven activation/supervisor-handoff failure with an **unobserved internal exception**, not evidence of candidate health or activation success. A repair must first add durable exception evidence and a regression for live stable-to-candidate handoff; do not guess at the exception or retry activation blindly.

## LARI lanes

- Product `lari`: **NOT_STARTED**. Canonical control SHA remains `14589d3a68a1654281ceeec2f3b9445fbbaa807a`; execution base remains `06ebf176e411faf6820ded34987f894cc5e7dc9e`; R2 remains accepted; R3 visual/browser validation remains pending.
- `lari-ui-v2`: **NOT_STARTED**. Synchronization/productization remains pending. The dirty worktree `C:\Projects\LARI-ui-v2-post-wp5-sync` was preserved and no fresh lane worktree was created.
- LARI P0 CI candidate `e12ada695da17d937b6a32a0a9af01f0e02cbaa1` / run `38222159846` remains review-only hosted evidence, not canonical acceptance or live staging remediation.

## Exact next action

1. Preserve the healthy paused stable runtime; do not resume or submit commands.
2. From `C:\Projects\AOS-runstatus-repair-20261012`, add durable supervisor exception capture for the hot-switch path and a deterministic stable-to-candidate handoff regression.
3. Reproduce and repair the actual exception in an isolated runtime only.
4. Produce a new exact candidate SHA, hosted CI, canary, KCP coverage, and explicit operator activation authorization.
5. Only after the replacement candidate passes protected paused health may Product then UI-V2 be admitted sequentially.

No Supabase mutation, production deployment, canonical acceptance change, paid fallback, or historical-command restart occurred.
