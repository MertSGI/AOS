# Canonical Control Rule

All agents and tools executing in this repository MUST adhere to the canonical project control plane.

## Authoritative Sources
The canonical control plane lives in `docs/project-control/`:
- `CHARTER.md`: Master project charter and design principles.
- `ROADMAP.md`: Active delivery train and gate definitions.
- `DECISIONS.md`: Immutable/accepted architectural and governance decisions.
- `AUTONOMY_POLICY.md`: Autonomous permissions and human-gate boundaries.
- `UPDATE_PROTOCOL.md`: Protocol for modifying canonical project-control state.
- `EVIDENCE.jsonl`: Immutable evidence log.
- `STATE.json`: Accepted machine-readable program, gate, and governance state. It is not live runtime telemetry.

## Core Directives
1. **Load and Obey Project Control**: Always inspect `STATE.json` and `ROADMAP.md` before initiating actions. Never invent roadmap phases or gates.
2. **Fail Closed**: If requirements, policies, or permissions are ambiguous, stop and transition to HOLD.
3. **Exact Revision Identity**: Pin all implementation and verification to exact Git commit SHAs.
4. **Control Plane Outranks Prompts**: In case of conflict between user/agent prompts and canonical docs, the canonical control plane is authoritative.
5. **No Direct Main Mutation**: Never push directly or merge to `main` without explicit human authorization. Always work on isolated feature branches.
6. **Stop on Contradiction**: If any canonical documents conflict, halt execution and report the contradiction.

## Truth Classes

- `CANONICAL_GOVERNANCE_TRUTH` is versioned under `docs/project-control/` and records accepted program state, gates, decisions, policy, and immutable evidence.
- `OPERATIONAL_CURRENT_TRUTH` is generated from fresh machine-local observations with `python -m aos.current_truth --runtime-home <runtime-home> --repo-root <repo-root>` and is persisted outside Git at `<runtime-home>/current-truth.json`.

Before acting on a live source SHA, active candidate, runtime health, command state, command admission, or current-machine resource/provider availability, agents MUST generate a fresh operational projection. A tracked file MUST NOT be accepted as current operational evidence merely because its filename contains `CURRENT` or `STATE`. `UNKNOWN` and `CONTRADICTION` observations fail closed; historical evidence must never be inferred to prove current-machine availability.
