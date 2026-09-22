# Provider Reliability and Controller Relay Provenance Audit

Audit base: `6f044bf0cd37fdc09ad5b0b19bb66e5f32170b68`

Audit date: 2026-09-21

Safety posture: `Production=NO_GO`; no credentials, live Runtime V1 state, Startup authority, or managed product workspace was changed.

Integration target: `9e792bad060de7ba84924b6a19f1b5d419de3e6d` (2026-09-22). The integration preserves target-side source-transport recovery, protected-lineage handling, active-candidate rebinding, detailed telemetry, durable lane fallback, provider eligibility, three-state immutable provenance, and current provider model policy.

## Confirmed root causes and corrections

| Area | Before | After |
|---|---|---|
| Probe amplification | Every successful/failed inference incremented `probe_count`; one activation probe copied to N command registries was summed N times. | Only HALF_OPEN/synthetic probes count. Synthetic probes carry durable IDs and aggregation counts each ID once. |
| Probe scheduling | Activation probed every enabled provider without consulting CLOSED state or future OPEN backoff. | Activation probes only UNKNOWN or due OPEN/HALF_OPEN providers. A CLOSED provider is not re-probed. |
| NOT_ATTEMPTED | Missing credentials, disabled paid fallback, or unavailable local service was recorded as a provider failure and opened the circuit. | Availability metadata is recorded without fabricating a failure or state transition. |
| HALF_OPEN | Every caller was admitted while HALF_OPEN. | The first caller receives a durable 120-second probe lease; later callers fail over until resolution or lease expiry. |
| Health aggregation | ISO timestamps were selected lexicographically, so offsets could make an older success override a newer failure; a recovered CLOSED aggregate retained stale failure counts. | Timestamps are compared as instants, and a newer success resets the aggregate consecutive failure count. |
| Fallback policy | `allow_provider_fallback=false` changed telemetry labels but still selected the next provider. | Post-invocation selection stops when fallback is disabled. |
| Council admission | Circuit file paths were passed where registry objects were required; the exception was swallowed and spare-capacity council calls could remain admitted. | Paths are loaded into registries before aggregation. |
| Required nullable fields | Provider normalization deleted every explicit `null`, including required nullable properties such as `target_base_sha`. | Shared schema-aware normalization preserves required or schema-allowed nulls and only prunes invalid optional nulls. |
| Controller Relay | An accepted immutable runtime was revalidated against a mutable checkout HEAD. When that checkout advanced, the relay emitted `PROVENANCE_STATUS=FAIL` even though manifest, build record, runtime SHA, and asset tree still matched. | Materialized runtimes are validated against their immutable manifest attestation, build record, running SHA, and runtime asset-tree digest. Mutable HEAD remains the basis only for unmaterialized development execution. The relay exposes `PROVENANCE_BASIS` and bounded error details. |

## Provider contract findings

| Provider | Finding | Resolution |
|---|---|---|
| Nemotron | Blanket null pruning could corrupt canonical required-nullable output. | Uses shared schema-aware normalization; existing strict canonical validation remains authoritative. |
| Gemini | Parsed JSON was returned without canonical JSON Schema validation. | Adds local Draft 2020-12 validation after schema-aware normalization. |
| Groq | Blanket null pruning could corrupt otherwise valid canonical output. | Uses shared schema-aware normalization; existing local validation remains authoritative. |
| Cloudflare Workers AI | The generic adapter sent OpenAI's named/strict wrapper and the OpenAI-specific `store` field, while Workers AI documents the schema directly under `json_schema`. | Uses Cloudflare's documented response-format shape and omits `store` from generic Chat Completions. |
| OpenRouter Free | Routing did not require downstream support for requested structured-output parameters. | Sends `provider.require_parameters=true`; local canonical validation still fails closed. |
| Cerebras | The audit base selected deprecated `llama-3.3-70b`. | The integration target already uses `gpt-oss-120b`; that newer policy is preserved without modification. Generic strict/local validation remains fail-closed. |
| Hugging Face Router | Generic Chat Completions carried the unnecessary OpenAI `store` field; dynamic `:fastest` routing can vary downstream capability. | Omits `store` and retains strict request plus local canonical validation. Dynamic-route variance remains documented below. |
| Ollama | Health probing selected the first installed model, used `/api/generate` with a different toy contract, and the execution adapter did not validate the canonical schema or incomplete responses. | Requires the configured installed model, probes through the real `/api/chat` adapter and planner schema, validates locally, and classifies length/incomplete responses. |
| OpenAI paid safety | Probe eligibility checked only `allow_paid_fallback`, not the enable flag and positive budgets; Responses API capacity/server outcomes could be classified as contract failures. | All paid gates must be true before probing/routing. Capacity/server outcomes are transient. The paid provider remains disabled by policy. |
| Hugging Face account credit | An authenticated HTTP 402/payment-required response fell through to `CONTRACT_FAILURE`, incorrectly describing account availability as an adapter defect. | HTTP 402 and equivalent exhausted-credit messages become failover-eligible `CREDIT_EXHAUSTED`, retain that class through probing/circuit telemetry, and receive a bounded 24-hour cooldown. Genuine HTTP 400/schema failures remain `CONTRACT_FAILURE`. |

First-party contract references used during the audit:

- Cloudflare OpenAI compatibility and JSON mode: https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/ and https://developers.cloudflare.com/workers-ai/features/json-mode/
- OpenRouter structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- Cerebras supported models, deprecations, and structured outputs: https://inference-docs.cerebras.ai/models/overview, https://inference-docs.cerebras.ai/support/deprecation, and https://inference-docs.cerebras.ai/capabilities/structured-outputs
- Hugging Face structured outputs: https://huggingface.co/docs/inference-providers/guides/structured-output

## Deterministic regression coverage

`tests/test_provider_reliability_audit.py` covers:

- HALF_OPEN lease exclusivity and expiry;
- ordinary inference versus probe telemetry;
- disabled failover enforcement;
- offset-aware aggregation and recovered failure counts;
- shared activation-probe ID deduplication;
- NOT_ATTEMPTED state semantics;
- council capacity aggregation from persisted registry paths;
- Gemini and Ollama canonical schema rejection;
- configured Ollama model enforcement;
- Cloudflare/OpenRouter request profiles;
- OpenAI Responses capacity classification;
- complete paid-probe gating;
- due-bounded activation probing across multiple command registries; and
- immutable-slot relay provenance when a checkout has advanced, plus asset-tree mismatch failure.
- Hugging Face-style HTTP 402 credit exhaustion, long cooldown/reset, healthy-alternate failover, and true HTTP 400 contract failure separation.

Existing provider, circuit, hotfix, relay, provenance, and runtime tests were updated only where prior expectations encoded the confirmed defective behavior.

## Remaining risks

- Circuit persistence is command-local. The HALF_OPEN lease prevents duplicate attempts within one lineage/registry, but two already-running independent processes can still race before runtime-level observations converge.
- Probe ID history is retained for exact deduplication; very long-lived command registries can grow slowly with probe history.
- Hugging Face `:fastest` and OpenRouter free routing can change downstream providers. `require_parameters` is enforced for OpenRouter, while Hugging Face still relies on requested structured output plus local fail-closed validation.
- Provider contract assertions are deterministic mocked tests. No credentialed live probe was run by this audit.
- Provider availability evidence is intentionally heterogeneous: a healthy proven alternate prevents one provider's `QUOTA_EXHAUSTED`, `RATE_LIMITED`, `SERVER_CAPACITY`, `CREDIT_EXHAUSTED`, or local-model absence from becoming a simultaneous-provider release requirement.
- Immutable runtime provenance trusts the materializer's persisted `provenance=PROVEN` attestation and then detects runtime drift through the separate build record and asset-tree digest. It does not re-query historical CI on every heartbeat.

Production remains `NO_GO`.
