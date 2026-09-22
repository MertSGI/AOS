# `freellmapi_local` architecture

## Boundary

FreeLLMAPI remains a separately installed local service. AOS does not vendor it,
does not update it, and does not own its process automatically. The AOS provider
ID is `freellmapi_local`; the provider is a free/local meta-route, never a paid
route. Existing direct AOS providers remain separate registry entries and their
health stays independently observable.

The source-only candidate uses:

- upstream repository: `tashfeenahmed/freellmapi`
- pinned upstream commit: `15c30081d2ce832bea16d804d9edac4ed87c7bc3`
- audited checkout: `C:\Projects\freellmapi-aos-pin-15c30081`
- upstream server package version: `0.2.1`
- loopback API root: `http://127.0.0.1:3000/v1`
- explicit bind: `HOST=127.0.0.1` (the upstream source default is `::`)
- explicit port: `PORT=3000` (the upstream source default is `3001`)
- dedicated database: `%LOCALAPPDATA%\AOS\freellmapi-local\data\freeapi.db`
- planned build command: `npm ci && npm run build:server`
- planned foreground start command: `npm run start -w server`

No build, daemon, Windows Startup registration, service installation, live
secret provisioning, inference call, or deployment is part of Phase 1.

## Availability contract

Routine probes must not perform inference:

1. `GET http://127.0.0.1:3000/livez` is a cheap unauthenticated liveness check.
   Connection refusal/timeout means `LOCAL_GATEWAY_UNAVAILABLE`. HTTP 503 means
   the process answered but its DB/encryption layer is not live.
2. `GET http://127.0.0.1:3000/readyz` is the preferred routine eligibility
   check. HTTP 200 with `status=ok` makes the local meta-route eligible. HTTP
   503 is reachable-but-unavailable and preserves the bounded reason:
   `db_unreachable`, `no_upstreams_configured`,
   `all_upstreams_rate_limited`, or `all_upstreams_unhealthy`.
3. Authenticated `GET /v1/models?execution_status=ready` is optional diagnostic
   evidence, not the routine scheduler probe. It must use the unified gateway
   key and must not be followed by a second readiness probe in the same cycle.
4. A minimal structured planner inference is reserved for an explicit bounded
   activation proof. Source implementation and routine health checks use mocks
   or the non-inference readiness endpoints.

This avoids quota-consuming double probes while separating local service loss
from reachable gateway exhaustion.

## Planner request and response contract

The adapter calls `POST /v1/chat/completions` using the `auto:reliable` alias and
the existing AOS canonical JSON schema. The gateway's structured-output support
is advisory: AOS still parses JSON, applies its existing nullable-field
sanitization, and validates with the canonical Draft 2020-12 schema. Invalid or
malformed output is `CONTRACT_FAILURE`.

Gateway HTTP/error semantics map as follows:

| Observation | AOS classification |
|---|---|
| connection refused/timeout before HTTP | `LOCAL_GATEWAY_UNAVAILABLE` |
| `/readyz` 503 with a bounded reason | `LOCAL_GATEWAY_NO_UPSTREAM_ROUTE` or the corresponding quota/health subtype |
| inference 429 / `rate_limit_error` | `QUOTA_EXHAUSTED` or `RATE_LIMITED`, never `CONTRACT_FAILURE` |
| inference 502/503 provider/capacity response | `SERVER_CAPACITY` or `UPSTREAM_ROUTE_UNAVAILABLE` |
| syntactically invalid or schema-invalid success payload | `CONTRACT_FAILURE` |
| valid canonical planner object | success |

Successful attempt telemetry may include only bounded, sanitized routing
metadata: `X-Routed-Via`, `X-Fallback-Attempts`, and `X-Fallback-Trail`. It must
not persist response bodies, authorization headers, provider keys, the unified
gateway key, or arbitrary upstream errors. The routed-via value supplies the
actual upstream provider/model where present.

## Routing policy

For R0 planner work, the intended order is:

1. healthy direct free provider routes;
2. `freellmapi_local` as a broad free/meta fallback;
3. other eligible free/local recovery routes;
4. no paid route.

`freellmapi_local` has `billing_class=FREE`, `cloud_local=LOCAL`, and a distinct
`credential_env_var=FREELLMAPI_LOCAL_API_KEY`. Locality does not waive gateway
authentication. Its eligibility requires a successful cheap readiness result;
an UNKNOWN circuit alone is not sufficient.

## Credentials and process data

AOS remains the authority for provider credentials in its secure local store.
The bridge will read approved credentials into memory and construct
FreeLLMAPI's supported `FREEAPI_CONFIG_JSON` only in the child process
environment. FreeLLMAPI encrypts imported keys with AES-256-GCM in its dedicated
SQLite database. No generated JSON or environment file containing secrets is
written by AOS, logged, committed, or returned in telemetry.

The FreeLLMAPI encryption key and unified gateway key are separate secrets. They
must be supplied from the secure local store/process environment and must never
appear in state/evidence files. The lifecycle implementation must scrub the
temporary child-environment mapping after process creation and report only
presence/count booleans.

Startup remains an explicit operator action in this track. No unbounded daemon
or Startup authority is created, and live AOS is not stopped or restarted.

## Audited upstream evidence

- `server/src/lib/config.ts`: source defaults (`PORT=3001`, `HOST=::`) and
  `FREEAPI_DB_PATH`.
- `server/src/routes/status.ts`: `/livez`, `/readyz`, and bounded readiness
  reasons.
- `server/src/routes/proxy.ts` and `server/src/routes/responses.ts`: OpenAI
  routes, unified-key authentication, and routed-via response headers.
- `server/src/lib/fallback-loop.ts`: bounded fallback headers and distinct
  429/502/503 exhaustion semantics.
- `server/src/services/declarative-config.ts`: idempotent environment-backed
  configuration and encrypted key insertion.
- `server/src/db/index.ts`: dedicated SQLite path support and filesystem
  permission hardening.

