# AOS Project-Control Update Protocol

**Version:** 0.1.0
**Default posture:** canonical state changes require explicit versioned updates.

## Three update classes

1. **Canonical governance updates** are versioned changes to accepted program/gate state, policy, roadmap, or decisions. `STATE.json` belongs to this class; it is not live runtime telemetry.
2. **Immutable evidence updates** append evidence for a specific revision, environment, and observation time. Historical evidence remains historical and is never promoted into current-machine truth by naming or age.
3. **Operational observation/projection** is generated at read time with `python -m aos.current_truth --runtime-home <runtime-home> --repo-root <repo-root>` and atomically persisted to `<runtime-home>/current-truth.json`. It is machine-local telemetry, not canonical Git history.
4. **Knowledge continuity updates** append decisions, receipts, relationships,
   findings, blockers, supersession, and handoffs to
   `<runtime-home>/knowledge/ledger.jsonl`. The Knowledge Ledger is continuity
   truth, not governance or live operational truth. Its index, materialized
   documents, Drive outbox, and second-brain sources are derived. Second-brain
   answers are advisory only and cannot accept changes.

Runtime transitions, health changes, slot changes, and command/admission changes MUST refresh the operational projection. They MUST NOT create Git commits solely to synchronize an embedded "current SHA" or other mutable runtime field in a tracked document.

## A control-plane update is required when

1. A gate changes state.
2. A new exact baseline/revision is accepted.
3. New authoritative evidence closes or reopens a gate.
4. A blocker is added, removed, or reclassified.
5. An architectural/governance decision is accepted, amended, or superseded.
6. The canonical next action changes.
7. An AI/worker claim is rejected or corrected by stronger evidence.
8. Human approval changes the authority state of a task.

## A control-plane update is not required for

- routine retries that produce no state change,
- speculative discussion,
- read-only analysis,
- uncommitted experiments,
- duplicate evidence runs that add no new authoritative information.

## Commit discipline

Project-control updates should be isolated when practical and use a clear message such as:

`docs(control): <state or decision change>`

Implementation commits should not silently rewrite accepted governance.

## Evidence-before-closure rule

A gate may not be marked `CLOSED_PROVEN` until:
- required evidence exists,
- exact revision/environment identity is recorded where applicable,
- verifier requirements are satisfied,
- any configured human gate is satisfied.

## Contradiction rule

If authoritative new evidence conflicts with accepted state:
- stop further mutation,
- record the contradiction,
- move to HOLD/reopen according to policy,
- never preserve a prior closure merely for schedule convenience.
