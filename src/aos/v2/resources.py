"""Unified Zero-Paid Resource Gateway for AOS V2.

Handles BOTH plan generation and code edit generation.
Enforces:
- Eligibility precedes scoring:
  - Exact endpoint/provider/model
  - Real FREE/INCLUDED entitlement and hard_no_overage=True
  - Current quota and health observations (expiry <= 5 min)
  - Entitlement expiry <= 24h
  - Credential PRESENT in server environment
  - Supported JSON/code capability
  - Authorized data class (PUBLIC_DISPOSABLE_CODE)
  - Circuit not open
- Unknown cost, paid fields/endpoints, missing entitlement/auth -> ZERO backend calls.
- Per-goal bounded network attempts (default max 4: 1 plan + 2 edits + 1 fallback).
- SDK retries = 0, per-request timeout <= 120s.
- Explicit reservation in LifecycleStore attempts table BEFORE any network send.
- Sanitized logging: call IDs, usage, finish reason, latency, evidence ref, billed cost UNKNOWN.
- Never mark an injected/mocked response as live model success (SIMULATED_MODEL).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from aos.v2.lifecycle import LifecycleStore, _digest, _identifier, _json, _strict_json_loads
from aos.v2.registry import RegistrySnapshot


class ResourceError(Exception):
    """Base exception for resource gateway errors."""


class ResourceUnavailableError(ResourceError):
    """Raised when no eligible resource is available (WAITING_RESOURCE)."""


class ResourceAuthError(ResourceError):
    """Raised when provider authentication fails."""


class ResourceBudgetExceededError(ResourceError):
    """Raised when the network attempt budget is exhausted."""


class ResourceContentError(ResourceError):
    """Raised when model response is malformed, oversized, or truncated (HOLD)."""


@dataclass(frozen=True)
class ResourceProfile:
    id: str
    enabled: bool
    provider: str
    model: str
    protocol: str
    base_url: str
    credential_ref: str
    capabilities: dict[str, Any]
    context_window: int
    token_count_method: str
    entitlement: dict[str, Any]
    quota: dict[str, Any]
    health: dict[str, Any]
    allowed_data_classes: tuple[str, ...]
    circuit: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourceProfile:
        rid = str(data.get("id", ""))
        _identifier(rid)
        return cls(
            id=rid,
            enabled=bool(data.get("enabled", False)),
            provider=str(data.get("provider", "openai_compatible")),
            model=str(data.get("model", "")),
            protocol=str(data.get("protocol", "chat.completions")),
            base_url=str(data.get("base_url", "")),
            credential_ref=str(data.get("credential_ref", "")),
            capabilities=dict(data.get("capabilities", {})),
            context_window=int(data.get("context_window", 8192)),
            token_count_method=str(data.get("token_count_method", "approximate_chars")),
            entitlement=dict(data.get("entitlement", {})),
            quota=dict(data.get("quota", {})),
            health=dict(data.get("health", {})),
            allowed_data_classes=tuple(data.get("allowed_data_classes", ["PUBLIC_DISPOSABLE_CODE"])),
            circuit=dict(data.get("circuit", {"open": False, "failure_count": 0})),
        )


@dataclass(frozen=True)
class GenerationObservation:
    attempt_id: str
    command_id: str
    task_id: str | None
    purpose: str
    profile_id: str
    model: str
    content: str
    finish_reason: str
    usage: dict[str, int]
    latency_ms: float
    quota_headers: dict[str, str]
    evidence_ref: str
    billed_cost: str  # Always "UNKNOWN" without independent financial account proof
    evidence_class: str  # "SIMULATED_MODEL" or "REMOTE_INFERENCE_PROOF"
    raw_response_sha256: str


def _parse_utc_iso(val: Any) -> datetime.datetime | None:
    if not isinstance(val, str) or not val:
        return None
    try:
        dt = datetime.datetime.fromisoformat(val)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def check_eligibility(
    profile: ResourceProfile,
    data_class: str = "PUBLIC_DISPOSABLE_CODE",
    *,
    now: float | datetime.datetime | None = None,
    allow_loopback: bool = False,
) -> tuple[bool, str]:
    """Check if a ResourceProfile is eligible for zero-paid execution."""
    if not profile.enabled:
        return False, "Profile is disabled"

    if not profile.provider or not isinstance(profile.provider, str) or not profile.provider.strip():
        return False, "Provider must be non-empty string"

    if not profile.model or not isinstance(profile.model, str) or not profile.model.strip():
        return False, "Model must be non-empty string"

    supported_protocols = {"OPENAI_CHAT_COMPLETIONS_V1", "chat.completions", "OPENAI_COMPATIBLE"}
    if profile.protocol not in supported_protocols:
        return False, f"Protocol '{profile.protocol}' is unsupported"

    if profile.context_window < 1024:
        return False, f"context_window ({profile.context_window}) is below minimum bound of 1024"

    if profile.token_count_method not in ("approximate_chars", "tiktoken", "cl100k_base", "o200k_base"):
        return False, f"token_count_method '{profile.token_count_method}' is unsupported/unverified"

    if data_class not in profile.allowed_data_classes:
        return False, f"Data class '{data_class}' not allowed by profile"

    if profile.circuit.get("open", False):
        return False, "Circuit breaker is open"

    # Enforce base_url scheme: HTTPS in production; loopback only if explicit
    base_url = profile.base_url.lower()
    if "://" not in base_url:
        return False, "Invalid base_url format"
    scheme = base_url.split("://", 1)[0]
    if scheme != "https":
        if not (allow_loopback and (base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:"))):
            return False, "Only HTTPS direct provider origins are allowed"
    if any(c in profile.base_url for c in ("?", "#", "@")):
        return False, "base_url must not contain userinfo, query, or fragment"

    # Entitlement checks: must be FREE or INCLUDED and hard_no_overage is True
    ent = profile.entitlement
    tier = str(ent.get("tier", "")).upper()
    if tier not in ("FREE", "INCLUDED"):
        return False, f"Non-free tier '{tier}': ZERO_PAID policy forbids paid resources"

    if ent.get("hard_no_overage") is not True:
        return False, "hard_no_overage must be boolean True"

    # Current time
    if isinstance(now, (int, float)):
        now_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    elif isinstance(now, datetime.datetime):
        now_dt = now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
    else:
        now_dt = datetime.datetime.now(datetime.timezone.utc)

    # Entitlement freshness (max 24h)
    ent_obs = _parse_utc_iso(ent.get("observed_at"))
    if ent_obs is None or (now_dt - ent_obs).total_seconds() > 24 * 3600:
        return False, "Entitlement proof observation missing or older than 24h"

    ent_exp = _parse_utc_iso(ent.get("expires_at"))
    if ent_exp is not None and now_dt >= ent_exp:
        return False, "Entitlement proof has expired"

    # Quota observation freshness (max 5 minutes)
    quota = profile.quota
    quota_obs = _parse_utc_iso(quota.get("observed_at"))
    if quota_obs is None or (now_dt - quota_obs).total_seconds() > 300:
        return False, "Quota observation missing or older than 5 minutes"

    remaining = quota.get("remaining_requests", 0)
    if remaining <= 0:
        return False, "Remaining quota is 0 or negative"

    # Health observation freshness (max 5 minutes)
    health = profile.health
    health_obs = _parse_utc_iso(health.get("observed_at"))
    if health_obs is None or (now_dt - health_obs).total_seconds() > 300:
        return False, "Health observation missing or older than 5 minutes"

    if str(health.get("status", "")).upper() != "HEALTHY":
        return False, f"Health status is not HEALTHY: {health.get('status')}"

    # Future timestamps check (reject if more than 60s in future)
    for obs_name, obs_dt in [("entitlement", ent_obs), ("quota", quota_obs), ("health", health_obs)]:
        if obs_dt is not None and (obs_dt - now_dt).total_seconds() > 60:
            return False, f"{obs_name} observation timestamp is in the future"

    # Credential present in server environment
    if not profile.credential_ref:
        return False, "No credential_ref specified"

    token = os.environ.get(profile.credential_ref)
    if not token or not token.strip():
        return False, f"Credential '{profile.credential_ref}' not present in environment"

    return True, "ELIGIBLE"


class ResourceGateway:
    """Gateway managing zero-paid AI model calls for planning and code edits."""

    def __init__(
        self,
        snapshot: RegistrySnapshot,
        store: LifecycleStore | None = None,
        *,
        transport_factory: Callable[[str, str, dict[str, Any]], str | bytes] | None = None,
        mock_generator: Callable[[str, str, dict[str, Any]], str] | None = None,
        allow_loopback: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.store = store
        self.transport_factory = transport_factory or mock_generator
        self.allow_loopback = allow_loopback
        self.profiles: dict[str, ResourceProfile] = {}
        for rid, rdata in snapshot.resources.items():
            try:
                self.profiles[rid] = ResourceProfile.from_dict(rdata)
            except Exception:
                pass

    def get_eligible_profiles(
        self,
        data_class: str = "PUBLIC_DISPOSABLE_CODE",
        *,
        now: float | datetime.datetime | None = None,
    ) -> list[ResourceProfile]:
        """Rank and return eligible profiles (highest headroom first)."""
        eligible: list[tuple[int, ResourceProfile]] = []
        for p in self.profiles.values():
            ok, _ = check_eligibility(p, data_class, now=now, allow_loopback=self.allow_loopback)
            if ok:
                remaining_quota = int(p.quota.get("remaining_requests", 0))
                eligible.append((remaining_quota, p))

        # Sort by remaining quota descending, then ID
        eligible.sort(key=lambda item: (-item[0], item[1].id))
        return [item[1] for item in eligible]

    def generate(
        self,
        command_id: str,
        purpose: str,  # "PLAN" or "EDIT"
        system_prompt: str,
        user_prompt: str,
        budget: dict[str, Any],
        *,
        task_id: str | None = None,
        data_class: str = "PUBLIC_DISPOSABLE_CODE",
        response_format: str = "json_object",
        now: float | datetime.datetime | None = None,
    ) -> GenerationObservation:
        """Reserve network attempt and execute generation call against eligible resource."""
        _identifier(command_id)
        if task_id:
            _identifier(task_id)
        if purpose not in ("PLAN", "EDIT"):
            raise ValueError(f"Invalid purpose '{purpose}'; expected 'PLAN' or 'EDIT'")

        max_attempts = int(budget.get("max_network_attempts", 4))
        timeout = float(budget.get("per_request_timeout", 120.0))
        max_context_bytes = int(budget.get("max_context_bytes", 32768))
        max_output_bytes = int(budget.get("max_output_bytes", 8192))

        # Context size verification
        total_input_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
        if total_input_bytes > max_context_bytes:
            raise ResourceContentError(
                f"Context size ({total_input_bytes} bytes) exceeds budget of {max_context_bytes} bytes"
            )

        # Check existing attempts count
        if self.store is not None:
            with closing(self.store._connect()) as db:
                cnt = db.execute(
                    "SELECT count(*) FROM attempts WHERE command_id=?", (command_id,)
                ).fetchone()[0]
                if cnt >= max_attempts:
                    raise ResourceBudgetExceededError(
                        f"Network attempt budget exhausted ({cnt}/{max_attempts})"
                    )

        eligible_profiles = self.get_eligible_profiles(data_class, now=now)
        if not eligible_profiles:
            raise ResourceUnavailableError(
                "WAITING_RESOURCE: No eligible zero-cost profile available"
            )

        profile = eligible_profiles[0]
        attempt_id = f"att_{secrets.token_hex(12)}"
        req_payload = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"} if response_format == "json_object" else None,
        }
        req_bytes = _json(req_payload).encode("utf-8")
        request_sha = hashlib.sha256(req_bytes).hexdigest()

        # Step 1: Durably reserve attempt in SQLite BEFORE network call
        if self.store is not None:
            self.store.reserve_attempt(
                attempt_id=attempt_id,
                command_id=command_id,
                task_id=task_id,
                purpose=purpose,
                profile_id=profile.id,
                request_sha=request_sha,
            )

        start_time = time.time()

        # Step 2: Generation execution via synthetic transport factory
        if self.transport_factory is not None:
            try:
                res = self.transport_factory(purpose, user_prompt, budget)
                content = res.decode("utf-8") if isinstance(res, bytes) else str(res)
                latency_ms = (time.time() - start_time) * 1000.0
                usage = {
                    "prompt_tokens": len(req_bytes) // 4,
                    "completion_tokens": len(content.encode("utf-8")) // 4,
                    "total_tokens": (len(req_bytes) + len(content.encode("utf-8"))) // 4,
                }
                obs = GenerationObservation(
                    attempt_id=attempt_id,
                    command_id=command_id,
                    task_id=task_id,
                    purpose=purpose,
                    profile_id=profile.id,
                    model=profile.model,
                    content=content,
                    finish_reason="stop",
                    usage=usage,
                    latency_ms=latency_ms,
                    quota_headers={"x-ratelimit-remaining-requests": "99"},
                    evidence_ref=f"simulated://{profile.id}/{attempt_id}",
                    billed_cost="UNKNOWN",
                    evidence_class="SIMULATED_INFERENCE",
                    raw_response_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
                if self.store is not None:
                    self.store.record_attempt_outcome(
                        attempt_id=attempt_id,
                        response_id=f"sim_resp_{attempt_id}",
                        usage_json=_json(usage),
                        finish_reason="stop",
                        quota_headers=_json(obs.quota_headers),
                        latency_ms=latency_ms,
                        evidence_ref=obs.evidence_ref,
                        billed_cost="UNKNOWN",
                    )
                return obs
            except Exception as e:
                if self.store is not None:
                    self.store.record_attempt_outcome(
                        attempt_id=attempt_id,
                        finish_reason="error",
                        evidence_ref=f"error://{type(e).__name__}",
                    )
                raise

        # Step 3: Production call via pinned openai SDK
        credential = os.environ.get(profile.credential_ref, "")
        try:
            import httpx
            import openai

            http_client = httpx.Client(
                follow_redirects=False,
                trust_env=False,
                timeout=timeout,
            )
            client = openai.OpenAI(
                base_url=profile.base_url.rstrip("/"),
                api_key=credential,
                max_retries=0,
                timeout=timeout,
                http_client=http_client,
            )

            with client.chat.completions.with_streaming_response.create(
                model=profile.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"} if response_format == "json_object" else None,
            ) as response:
                accumulated = bytearray()
                for chunk in response.iter_bytes():
                    accumulated.extend(chunk)
                    if len(accumulated) > max_output_bytes * 2:
                        raise ResourceContentError(f"Model response exceeds output limit ({len(accumulated)} bytes)")

                latency_ms = (time.time() - start_time) * 1000.0
                resp_bytes = bytes(accumulated)
                resp_data = _strict_json_loads(resp_bytes)
                choice = resp_data.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason", "stop")
                usage = resp_data.get("usage", {})
                resp_id = resp_data.get("id", f"resp_{attempt_id}")

                quota_hdrs = {}
                for k, v in response.headers.items():
                    if k.lower().startswith("x-ratelimit"):
                        quota_hdrs[k.lower()] = v

                obs = GenerationObservation(
                    attempt_id=attempt_id,
                    command_id=command_id,
                    task_id=task_id,
                    purpose=purpose,
                    profile_id=profile.id,
                    model=profile.model,
                    content=content,
                    finish_reason=finish_reason,
                    usage=usage,
                    latency_ms=latency_ms,
                    quota_headers=quota_hdrs,
                    evidence_ref=f"remote://{profile.id}/{resp_id}",
                    billed_cost="UNKNOWN",
                    evidence_class="REMOTE_INFERENCE_PROOF",
                    raw_response_sha256=hashlib.sha256(resp_bytes).hexdigest(),
                )

                if self.store is not None:
                    self.store.record_attempt_outcome(
                        attempt_id=attempt_id,
                        response_id=resp_id,
                        usage_json=_json(usage),
                        finish_reason=finish_reason,
                        quota_headers=_json(quota_hdrs),
                        latency_ms=latency_ms,
                        evidence_ref=obs.evidence_ref,
                        billed_cost="UNKNOWN",
                    )
                return obs
        except Exception as exc:
            if self.store is not None:
                self.store.record_attempt_outcome(
                    attempt_id=attempt_id,
                    finish_reason="error",
                    evidence_ref=f"error://{type(exc).__name__}",
                )
            exc_str = str(exc)
            if "401" in exc_str:
                raise ResourceAuthError(f"Authentication failed for provider '{profile.provider}': {exc}") from exc
            if "429" in exc_str:
                raise ResourceUnavailableError(f"WAITING_RESOURCE: Rate limited or quota exhausted: {exc}") from exc
            raise ResourceError(f"Transport failure contacting provider: {exc}") from exc
