# AOS Maintenance Decisions

- Isolated maintenance workspace only.
- LARI and UI-V2 protected commands/workspaces are immutable to this lane.
- Production remains NO_GO.
- Paid fallback remains disabled.
- Force push and history rewrite are forbidden.
- First canary stops at PROMOTION_READY.
- Runtime activation belongs to independent supervisor authority.
