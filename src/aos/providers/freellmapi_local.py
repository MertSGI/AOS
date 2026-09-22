"""Loopback-only FreeLLMAPI planner adapter.

The gateway is a local transport boundary over potentially remote free
providers. AOS therefore keeps canonical schema validation and external-proof
semantics while exposing only bounded routing metadata from the gateway.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.providers.openai_compatible import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    default_max_output_tokens,
    project_openai_compatible_schema,
    strict_schema_compatible,
)
from aos.providers.schema_utils import sanitize_planner_output


DEFAULT_BASE_URL = "http://127.0.0.1:3000/v1"
DEFAULT_READINESS_TIMEOUT_SECONDS = 1.5
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
READINESS_CACHE_SECONDS = 2.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_READINESS_BYTES = 4096

_ROUTED_VIA_RE = re.compile(
    r"^(?P<provider>[A-Za-z0-9_.:-]{1,48})/(?P<model>[A-Za-z0-9_./:@+\-]{1,160})$"
)
_TRAIL_ENTRY_RE = re.compile(
    r"^[A-Za-z0-9_.:-]{1,48}/[A-Za-z0-9_./:@+\-]{1,160} key[0-9]{1,3}=[a-z_]{1,48}$"
)
_TRAIL_REMAINDER_RE = re.compile(r"^\+[0-9]{1,3} more$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+\-/]{1,160}$")


@dataclass(frozen=True)
class FreeLLMAPIReadiness:
    """Sanitized, non-secret local readiness observation."""

    service_available: bool
    eligible: bool
    reason: str
    status_code: Optional[int]
    ready_upstreams: Optional[int] = None

    def to_telemetry(self) -> Dict[str, Any]:
        return {
            "service_available": self.service_available,
            "eligible": self.eligible,
            "reason": self.reason,
            "status_code": self.status_code,
            "ready_upstreams": self.ready_upstreams,
        }


def _is_loopback_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_base_url(base_url: str) -> str:
    value = str(base_url).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or not _is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("freellmapi_local base URL must be an uncredentialed HTTP loopback URL")
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError("freellmapi_local base URL must end with /v1")
    return value


def sanitize_routing_headers(headers: Mapping[str, str]) -> Dict[str, Any]:
    """Return only recognized, grammar-checked, bounded gateway metadata."""
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    metadata: Dict[str, Any] = {}

    routed = normalized.get("x-routed-via", "").strip()
    routed_match = _ROUTED_VIA_RE.fullmatch(routed)
    if routed_match:
        metadata["routed_via"] = routed
        metadata["routed_provider_id"] = routed_match.group("provider")
        metadata["routed_model_id"] = routed_match.group("model")

    attempts_raw = normalized.get("x-fallback-attempts", "").strip()
    if attempts_raw.isdigit():
        attempts = int(attempts_raw)
        if 0 <= attempts <= 100:
            metadata["fallback_attempts"] = attempts

    trail_raw = normalized.get("x-fallback-trail", "").strip()
    if trail_raw and len(trail_raw) <= 2048:
        accepted = []
        for segment in trail_raw.split(";")[:11]:
            item = segment.strip()
            if _TRAIL_ENTRY_RE.fullmatch(item) or _TRAIL_REMAINDER_RE.fullmatch(item):
                accepted.append(item)
            else:
                accepted = []
                break
        if accepted:
            metadata["fallback_trail"] = "; ".join(accepted)[:768]

    return metadata


def _bounded_json_body(response: Any, limit: int) -> Any:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise PlannerContractError("freellmapi_local response exceeded the bounded body limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlannerContractError("freellmapi_local returned malformed JSON") from exc


def _safe_response_id(value: Any) -> Optional[str]:
    candidate = str(value or "").strip()
    return candidate if _SAFE_ID_RE.fullmatch(candidate) else None


class FreeLLMAPILocalPlannerProvider:
    """OpenAI-compatible planner provider backed by a pinned loopback gateway."""

    provider_id = "freellmapi_local"
    execution_provenance = "LIVE_EXTERNAL"

    def __init__(
        self,
        model: str = "auto:reliable",
        base_url: str = DEFAULT_BASE_URL,
        credential_env_var: str = "FREELLMAPI_LOCAL_API_KEY",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        opener: Optional[Any] = None,
    ) -> None:
        self.model = str(model).strip() or "auto:reliable"
        self.base_url = _validated_base_url(base_url)
        self.credential_env_var = credential_env_var
        self.max_output_tokens = int(max_output_tokens)
        self.readiness_timeout_seconds = max(0.1, float(readiness_timeout_seconds))
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self._opener = opener or urllib.request.urlopen
        self.last_readiness: Optional[FreeLLMAPIReadiness] = None
        self.last_routing_metadata: Dict[str, Any] = {}
        self._readiness_checked_at = 0.0

    @property
    def service_root(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def check_readiness(self, *, force: bool = False) -> FreeLLMAPIReadiness:
        now = time.monotonic()
        if (
            not force
            and self.last_readiness is not None
            and now - self._readiness_checked_at <= READINESS_CACHE_SECONDS
        ):
            return self.last_readiness

        request = urllib.request.Request(
            f"{self.service_root}/readyz",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "AOS/freellmapi-local-readiness"},
        )
        try:
            with self._opener(request, timeout=self.readiness_timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                body = _bounded_json_body(response, MAX_READINESS_BYTES)
            status_value = body.get("status") if isinstance(body, dict) else None
            ready_count = body.get("ready_upstreams") if isinstance(body, dict) else None
            result = FreeLLMAPIReadiness(
                service_available=True,
                eligible=status == 200 and status_value == "ok",
                reason="ready" if status == 200 and status_value == "ok" else "invalid_readiness_response",
                status_code=status,
                ready_upstreams=(ready_count if isinstance(ready_count, int) and ready_count >= 0 else None),
            )
        except urllib.error.HTTPError as exc:
            reason = "readiness_http_error"
            try:
                body = _bounded_json_body(exc, MAX_READINESS_BYTES)
                if isinstance(body, dict) and isinstance(body.get("reason"), str):
                    candidate = body["reason"]
                    if candidate in {
                        "db_unreachable",
                        "no_upstreams_configured",
                        "all_upstreams_rate_limited",
                        "all_upstreams_unhealthy",
                    }:
                        reason = candidate
            except PlannerContractError:
                pass
            result = FreeLLMAPIReadiness(True, False, reason, int(exc.code))
        except (urllib.error.URLError, TimeoutError, OSError):
            result = FreeLLMAPIReadiness(False, False, "local_gateway_unavailable", None)
        except PlannerContractError:
            result = FreeLLMAPIReadiness(True, False, "invalid_readiness_response", 200)

        self.last_readiness = result
        self._readiness_checked_at = now
        return result

    def _require_ready(self) -> None:
        readiness = self.check_readiness()
        if readiness.eligible:
            return
        if not readiness.service_available:
            raise PlannerTransientError("freellmapi_local LOCAL_GATEWAY_UNAVAILABLE")
        if readiness.reason == "all_upstreams_rate_limited":
            raise PlannerTransientError("freellmapi_local QUOTA_EXHAUSTED")
        raise PlannerTransientError(
            f"freellmapi_local LOCAL_GATEWAY_NO_UPSTREAM_ROUTE ({readiness.reason})"
        )

    def _raise_http_error(self, status: int, body: Any) -> None:
        error = body.get("error") if isinstance(body, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        descriptor = "/".join(
            item for item in (str(error_type or ""), str(error_code or ""))
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)
        )
        suffix = f" ({descriptor})" if descriptor else ""

        if status in (401, 403):
            raise PlannerCredentialError(f"freellmapi_local GATEWAY_CREDENTIAL_UNAVAILABLE{suffix}")
        if status == 429:
            raise PlannerTransientError(f"freellmapi_local RATE_LIMITED{suffix}")
        if status == 502:
            raise PlannerTransientError(f"freellmapi_local UPSTREAM_ROUTE_UNAVAILABLE{suffix}")
        if status in (503, 504):
            raise PlannerTransientError(f"freellmapi_local SERVER_CAPACITY{suffix}")
        if status == 413:
            raise PlannerContractError(f"freellmapi_local request exceeded upstream context capacity{suffix}")
        if status in (400, 404, 422):
            raise PlannerContractError(f"freellmapi_local rejected the planner request contract{suffix}")
        if status >= 500:
            raise PlannerTransientError(f"freellmapi_local UPSTREAM_ROUTE_UNAVAILABLE HTTP {status}")
        raise PlannerContractError(f"freellmapi_local unexpected HTTP status {status}")

    def generate_plan(
        self,
        prompt: str,
        schema: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
        self.last_routing_metadata = {}
        self._require_ready()

        api_key = os.environ.get(self.credential_env_var, "").strip()
        if not api_key:
            raise PlannerCredentialError(
                f"{self.credential_env_var} environment variable is missing for freellmapi_local"
            )

        provider_schema = project_openai_compatible_schema(schema)
        instructions = (
            "You are the AOS Shadow Planner. Return only one bounded JSON object matching the "
            "provided canonical schema. Select the canonical milestone and next_action exactly "
            "as provided. mutation_intent must be NONE and risk_class must be R0."
        )
        strict_compatible = strict_schema_compatible(provider_schema)
        if not strict_compatible:
            instructions += (
                " AOS will validate this open-ended schema locally: "
                + json.dumps(provider_schema, ensure_ascii=False, sort_keys=True)
            )
        response_format: Dict[str, Any]
        if strict_compatible:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "planner_decision",
                    "strict": True,
                    "schema": provider_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "response_format": response_format,
            "max_tokens": default_max_output_tokens(schema, self.max_output_tokens),
            "temperature": 0.0,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AOS/freellmapi-local",
            },
        )

        try:
            with self._opener(request, timeout=self.request_timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                self.last_routing_metadata = sanitize_routing_headers(response.headers)
                body = _bounded_json_body(response, MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            self.last_routing_metadata = sanitize_routing_headers(exc.headers or {})
            try:
                body = _bounded_json_body(exc, MAX_RESPONSE_BYTES)
            except PlannerContractError:
                body = None
            self._raise_http_error(int(exc.code), body)
            raise AssertionError("unreachable")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlannerTransientError("freellmapi_local LOCAL_GATEWAY_UNAVAILABLE") from exc

        if status != 200:
            self._raise_http_error(status, body)
        if not isinstance(body, dict):
            raise PlannerContractError("freellmapi_local success response is not an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise PlannerContractError("freellmapi_local returned no choices")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise PlannerTransientError(
                "freellmapi_local response reached configured output capacity before completing JSON"
            )
        if finish_reason not in (None, "stop"):
            raise PlannerContractError(
                "freellmapi_local response finished with an unacceptable reason"
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise PlannerContractError("freellmapi_local returned empty content")
        try:
            parsed_decision = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerContractError("freellmapi_local output is not valid JSON") from exc
        if not isinstance(parsed_decision, dict):
            raise PlannerContractError("freellmapi_local output is not a structured object")

        parsed_decision = sanitize_planner_output(parsed_decision, schema)
        errors = list(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parsed_decision)
        )
        if errors:
            raise PlannerContractError(
                "freellmapi_local output failed canonical JSON schema validation: "
                + errors[0].message
            )

        usage_raw = body.get("usage")
        usage = None
        if isinstance(usage_raw, dict):
            usage = {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage_raw.get(key)
                usage[key] = value if isinstance(value, int) and value >= 0 else 0

        return parsed_decision, _safe_response_id(body.get("id")), usage

