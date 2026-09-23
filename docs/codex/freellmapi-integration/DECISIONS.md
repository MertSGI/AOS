# FreeLLMAPI integration decisions

## D-001 — separate pinned service

Accepted: FreeLLMAPI remains outside AOS at the exact audited commit. AOS stores
only integration metadata and lifecycle helpers. Automatic upstream tracking and
vendoring are rejected.

## D-002 — explicit loopback bind

Accepted: the service contract requires `HOST=127.0.0.1` and `PORT=3000`.
Relying on the upstream source default is rejected because its `::` listener is
not a loopback-only guarantee.

## D-003 — readiness before eligibility

Accepted: `/readyz` is the normal local availability contract. It is
unauthenticated, non-inference, and reports why a reachable gateway is not
serviceable. `/livez` is used to refine a local-internal failure. A real planner
request is not a health probe.

## D-004 — initial planner alias

Accepted for deterministic contract testing: `auto:reliable`. This is not an
activation claim. Phase 3 structured-output evidence may replace it only with a
recorded decision and deterministic evidence.

## D-005 — credentials remain separated

Accepted: AOS credentials are read from its secure local authority into memory;
FreeLLMAPI receives supported declarative configuration in the child process
environment and persists provider keys only through its encrypted store. No
plaintext bridge artifact is permitted.

## D-006 — no paid eligibility

Accepted: `freellmapi_local` is classified `FREE` even though it can aggregate
multiple upstreams. AOS does not provision paid upstream credentials into this
route, and the route cannot bypass AOS's paid-fallback gate.

## D-007 — explicit owned lifecycle only

Accepted: lifecycle actions validate the exact pinned checkout and operate one
foreground child owned by the caller. Import-time startup, detached daemons,
Windows Startup registration, service installation, and broad process-name
termination are rejected. The dedicated data directory is outside the source
checkout so failed starts and upstream source maintenance do not destroy AOS
project state.

## D-008 — in-memory free-credential bridge

Accepted: only the enumerated free-provider identities cross into
`FREEAPI_CONFIG_JSON`. Paid OpenAI and the unified local gateway credential are
never provisioned as upstream routes. The encryption key and declarative JSON
exist in the child environment only; lifecycle telemetry contains counts and
platform names, never values.
