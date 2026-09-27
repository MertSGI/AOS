# AOS Maintenance Decisions

## Authority: AOS-BOUNDED-SELF-MAINTENANCE-CANARY-20260927-01

### Authorized scope

- Validate the prepared Human Action Center governance hardening based on
  5f0e722dd7e441005171d173b4c555b351d7be12.
- Run focused Action Center tests.
- Run canonical offline tests and validators.
- If all verification passes, perform a normal fast-forward push to
  feature/aos-resource-os-operations-20260924-01.
- Require exact-SHA hosted CI SUCCESS.
- After CI SUCCESS, materialize an immutable runtime candidate.
- Require provenance PROVEN.
- Stop at PROMOTION_READY.

### Explicitly not authorized

- Force push.
- History rewrite.
- Runtime activation or promotion from the maintenance worker.
- Production mutation (production remains NO_GO).
- Paid API activation.
- LARI workspace or command mutation.
- UI-V2 workspace or command mutation.
- Recreation or reset of protected lineages.
- Secret exposure.

## Standing constraints

- Isolated maintenance workspace only.
- LARI and UI-V2 protected commands/workspaces are immutable to this lane.
- Production remains NO_GO.
- Paid fallback remains disabled.
- Force push and history rewrite are forbidden.
- First canary stops at PROMOTION_READY.
- Runtime activation belongs to independent supervisor authority.