# Jev access and cost audit

Verified: `2026-09-23` from current official provider/model documentation. No account dashboard state was assumed.

## Route comparison

| Route | Current model/API | Context exposed | Current public price | Audit disposition |
|---|---|---:|---:|---|
| Vercel AI Gateway | `typesafe-ai/jev`; `POST https://ai-gateway.vercel.sh/v1/evaluate`, AI SDK `experimental_evaluate`, or TypeSafe client at `/typesafe` | 32k | **Free promotional price through 2026-09-25** | `BEST_ACCESS_ROUTE` during the promotion. Temporary, credential required, and must fail closed when the promotion expires. |
| TypeSafe direct | `jev-1.13.0`/`jev-latest`; `POST https://api.typesafe.ai/v1/systemone`; official Python/JS SDKs | 64k aggregate and 32k state + longest question | `$0.042/M` input; output free | Canonical stable fallback after explicit paid authorization. Not callable by this track. |
| OpenRouter | `typesafe/jev-1.13` or `~typesafe/jev-latest`; experimental Decisions API | 32k | `$0.042/M` input; `$0/M` output | Available, but metered and its Jev API is marked alpha/experimental. Disabled without paid authorization. |
| Cloudflare Workers AI | `typesafe/jev`; Workers binding or `/accounts/{id}/ai/run` | 32k | Public catalog sends users to an authenticated dashboard for price | Officially available tertiary route, but public cost could not be verified. Treat as `UNKNOWN`/cost-blocked, not as zero-cost. |

## Direct TypeSafe facts

TypeSafe's current model page lists `jev-1.13.0`, a direct price of `$42/Btok` or `$0.042/Mtok` for input, free output, 250,000 tokens/second, and 1,200 requests/minute. Limits are described as dynamic. `jev-latest` is the SDK default and currently aliases `jev-1.13.0`; AOS evaluation and production policy should pin the versioned ID after calibration.

The official Python package is `typesafe-sdk`, using `TYPESAFE_API_KEY`. Direct errors include 401, 422, 429, and 529; the SDK retries 429/529 with backoff by default. AOS must avoid wrapping it in an uncontrolled second retry loop and must carry bounded Retry-After evidence into Resource OS.

## Vercel AI Gateway facts

Vercel's current Jev model page shows input and output as `Free`, a 32k context, and states that promotional pricing ends September 25, 2026. The September 21 changelog documents both `/v1/evaluate` and a TypeSafe-compatible base URL. The AI SDK adapter calls the boolean-style primitive `boolean`; the TypeSafe-compatible route retains `noul`.

The promotion makes an actually zero-cost route available on the audit date. It is not a durable price guarantee. Vercel's general AI Gateway policy otherwise charges provider list rates without token markup. Its free tier includes `$5/month` only for free-tier-eligible models; purchasing credits moves an account to the paid tier and removes that monthly free credit. AOS must never infer that a team/account is free merely from the public catalog.

No `AI_GATEWAY_API_KEY` or `VERCEL_OIDC_TOKEN` was present in the audit process. Therefore the permitted disposable proof was not run; no access was provisioned and no call was made.

## OpenRouter facts

OpenRouter's official Jev page lists `typesafe/jev-1.13`, 32k context, `$0.042/M` input and `$0/M` output. Jev uses OpenRouter's Decisions API rather than chat completions. The API path is documented under an alpha namespace, so it is a compatibility risk and should not be the primary integration route.

No `OPENROUTER_API_KEY` was present. OpenRouter is metered rather than currently zero-price, so a call would have violated this track's no-paid-call constraint even if a key had existed.

## Cloudflare facts

Cloudflare's official model catalog exposes `typesafe/jev`, a 32k context, and native Noul/Choice/Score payloads. Its public model page links pricing to the authenticated Cloudflare dashboard instead of publishing a numeric Jev rate. An account identifier was present locally, but no API token was present and cost eligibility could not be established. This route is `COST_UNKNOWN`, not free.

## AOS cost policy

`DecisionCostObservation` is evaluated before transport selection. For this preflight and any initial implementation:

- configured paid budget is `$0.00`;
- unknown pricing is ineligible;
- metered routes are ineligible without separate explicit authorization;
- promotional-free eligibility includes a hard expiry timestamp and a fresh public/account observation;
- free-credit availability is quota, not health, and must not be fabricated;
- auto top-up, payment creation, and paid fallback are forbidden;
- a route changing from free to paid becomes `COST_BLOCKED`, and AOS proceeds without Jev.

The initial route order is:

1. Vercel promotional-free Jev, only through `2026-09-25T23:59:59Z` or an earlier provider withdrawal, and only when a credential and current zero-price observation exist.
2. No Jev call.

After a separate paid authorization, the recommended stable order is TypeSafe direct, then OpenRouter, then Cloudflare only after its account-specific price is verified. This recommendation does not authorize any of them.

## Sources

- TypeSafe: [models, pricing, context and limits](https://docs.typesafe.ai/models), [HTTP API](https://docs.typesafe.ai/api), [Python SDK](https://docs.typesafe.ai/sdk/python).
- Vercel: [current Jev model card and promotion](https://vercel.com/ai-gateway/models/jev), [Jev HTTP/TypeSafe access](https://vercel.com/changelog/ai-gateway-now-supports-typesafe-clients-and-http-api-for-jev), [AI Gateway pricing](https://vercel.com/docs/ai-gateway/pricing).
- OpenRouter: [Jev 1.13 model/API page](https://openrouter.ai/typesafe/jev-1.13/api).
- Cloudflare: [Jev model page](https://developers.cloudflare.com/ai/models/typesafe/jev/).
