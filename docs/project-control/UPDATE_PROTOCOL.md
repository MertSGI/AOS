# AOS Project-Control Update Protocol

**Version:** 0.1.0
**Default posture:** canonical state changes require explicit versioned updates.

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
