"""Typed, sanitized provider observations used by Resource OS.

Only closed enums and bounded numeric metadata cross the provider boundary. Raw
headers, bodies, prompts, generated content and exception strings are never
stored in these objects.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, fields, replace
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional


class TaskClass(str, Enum):
    UNKNOWN = "unknown"
    SMALL_REASONING = "small_reasoning"
    STRUCTURED_PLANNING = "structured_planning"
    REPO_UI_PLANNING = "repo_ui_planning"
    LARGE_CONTEXT = "large_context"
    AGENTIC_EXECUTION = "agentic_execution"


class ObservationSource(str, Enum):
    PROVIDER_METADATA = "PROVIDER_METADATA"
    OBSERVED_RUNTIME = "OBSERVED_RUNTIME"
    PROVIDER_DOCUMENTATION = "PROVIDER_DOCUMENTATION"
    ADAPTIVE_ESTIMATE = "ADAPTIVE_ESTIMATE"


class FailureFamily(str, Enum):
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    SERVER_CAPACITY = "SERVER_CAPACITY"
    CONTRACT = "CONTRACT"
    CREDENTIAL = "CREDENTIAL"
    LOCAL_SERVICE = "LOCAL_SERVICE"
    UNKNOWN = "UNKNOWN"


class ContractFailureSubtype(str, Enum):
    BAD_REQUEST = "BAD_REQUEST"
    NO_CHOICES = "NO_CHOICES"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    REFUSAL = "REFUSAL"
    BAD_FINISH_REASON = "BAD_FINISH_REASON"
    PROVIDER_CONTRACT_ERROR = "PROVIDER_CONTRACT_ERROR"


_SOURCE_RANK = {
    ObservationSource.ADAPTIVE_ESTIMATE.value: 1,
    ObservationSource.PROVIDER_DOCUMENTATION.value: 2,
    ObservationSource.OBSERVED_RUNTIME.value: 3,
    ObservationSource.PROVIDER_METADATA.value: 4,
}
_NUMERIC_FIELDS = {
    "retry_after_seconds",
    "retry_at_epoch",
    "request_limit",
    "request_remaining",
    "request_reset_epoch",
    "token_limit",
    "token_remaining",
    "token_reset_epoch",
}
_INTEGER_FIELDS = {
    "http_status",
    "request_limit",
    "request_remaining",
    "token_limit",
    "token_remaining",
}
_MAX_DURATION_SECONDS = 366 * 24 * 60 * 60
_HEADER_TO_FIELD = {
    "x-ratelimit-limit-requests": "request_limit",
    "x-ratelimit-remaining-requests": "request_remaining",
    "x-ratelimit-reset-requests": "request_reset_epoch",
    "x-ratelimit-limit-tokens": "token_limit",
    "x-ratelimit-remaining-tokens": "token_remaining",
    "x-ratelimit-reset-tokens": "token_reset_epoch",
    "ratelimit-limit": "request_limit",
    "ratelimit-remaining": "request_remaining",
    "ratelimit-reset": "request_reset_epoch",
}


def _enum_value(value: Any, enum_type: type[Enum], default: Enum) -> str:
    if isinstance(value, enum_type):
        return str(value.value)
    try:
        return str(enum_type(value).value)
    except (TypeError, ValueError):
        return str(default.value)


def canonical_task_class(value: Any) -> str:
    return _enum_value(value, TaskClass, TaskClass.UNKNOWN)


def canonical_failure_family(value: Any) -> str:
    text = str(getattr(value, "value", value) or "").upper()
    if any(token in text for token in ("CREDIT", "QUOTA", "RATE_LIMIT", "429", "RESOURCE_EXHAUSTED")):
        return "QUOTA"
    if "TIMEOUT" in text:
        return FailureFamily.TIMEOUT.value
    if any(token in text for token in ("NETWORK", "CONNECTION", "DNS")):
        return FailureFamily.NETWORK.value
    if any(token in text for token in ("CAPACITY", "503", "502", "504", "SERVER", "INTERNAL")):
        return FailureFamily.SERVER_CAPACITY.value
    if any(token in text for token in ("CONTRACT", "SCHEMA", "BAD_REQUEST", "INVALID_JSON")):
        return FailureFamily.CONTRACT.value
    if any(token in text for token in ("CREDENTIAL", "AUTH", "PERMISSION", "401", "403")):
        return FailureFamily.CREDENTIAL.value
    if any(token in text for token in ("LOCAL_GATEWAY", "LOCAL_SERVICE", "OLLAMA")):
        return FailureFamily.LOCAL_SERVICE.value
    return FailureFamily.UNKNOWN.value


def _clean_number(value: Any, *, integer: bool = False) -> Optional[float | int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if integer:
        if not number.is_integer():
            return None
        return int(number)
    return number


def _iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class RateLimitObservation:
    provider_id: str
    model_id: Optional[str]
    task_class: str
    observed_at: str
    http_status: Optional[int] = None
    classification: str = "UNKNOWN"
    retry_after_seconds: Optional[float] = None
    retry_at_epoch: Optional[float] = None
    request_limit: Optional[int] = None
    request_remaining: Optional[int] = None
    request_reset_epoch: Optional[float] = None
    token_limit: Optional[int] = None
    token_remaining: Optional[int] = None
    token_reset_epoch: Optional[float] = None
    quota_scope: str = "UNKNOWN"
    evidence_source: str = ObservationSource.OBSERVED_RUNTIME.value
    field_sources: Optional[Dict[str, str]] = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not self.provider_id or len(self.provider_id) > 128:
            raise ValueError("provider_id must be a bounded non-empty identifier")
        if self.model_id is not None and len(self.model_id) > 256:
            raise ValueError("model_id is too long")
        task_class = canonical_task_class(self.task_class)
        if task_class == TaskClass.UNKNOWN.value and self.task_class != TaskClass.UNKNOWN.value:
            raise ValueError("unknown task_class")
        object.__setattr__(self, "task_class", task_class)
        if self.evidence_source not in _SOURCE_RANK:
            raise ValueError("unknown evidence_source")
        if self.classification not in {"UNKNOWN", "RATE_LIMITED", "QUOTA_EXHAUSTED", "AVAILABLE", "CONSTRAINED", "CREDIT_EXHAUSTED"}:
            raise ValueError("unknown rate classification")
        if self.quota_scope not in {"UNKNOWN", "PROVIDER", "ACCOUNT", "PROJECT", "MODEL", "REQUEST", "TOKEN"}:
            raise ValueError("unknown quota_scope")
        for name in _NUMERIC_FIELDS | {"http_status"}:
            value = getattr(self, name)
            cleaned = _clean_number(value, integer=name in _INTEGER_FIELDS)
            if value is not None and cleaned is None:
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, cleaned)
        safe_sources: Dict[str, str] = {}
        for key, value in (self.field_sources or {}).items():
            if key in _NUMERIC_FIELDS | {"http_status"} and value in _SOURCE_RANK:
                safe_sources[key] = value
        object.__setattr__(self, "field_sources", safe_sources)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "task_class": self.task_class,
            "observed_at": self.observed_at,
            "classification": self.classification,
            "quota_scope": self.quota_scope,
            "evidence_source": self.evidence_source,
            "field_sources": dict(self.field_sources or {}),
        }
        for name in _NUMERIC_FIELDS | {"http_status"}:
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RateLimitObservation":
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def parse_retry_after(value: Any, now_epoch: float) -> tuple[Optional[float], Optional[float]]:
    if value is None:
        return None, None
    text = str(value).strip()
    seconds = _clean_number(text)
    if seconds is not None:
        if seconds > _MAX_DURATION_SECONDS:
            return None, None
        return float(seconds), float(now_epoch + seconds)
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        epoch = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None, None
    delta = epoch - now_epoch
    if delta < 0 or delta > _MAX_DURATION_SECONDS:
        return None, None
    return float(delta), float(epoch)


def _headers_from_exception(exc: BaseException) -> Mapping[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        return headers
    headers = getattr(exc, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def extract_rate_limit_observation(
    *,
    provider_id: str,
    model_id: Optional[str],
    task_class: str,
    exc: Optional[BaseException] = None,
    headers: Optional[Mapping[str, Any]] = None,
    http_status: Optional[int] = None,
    now: Optional[Callable[[], float]] = None,
    classification: Optional[str] = None,
    quota_scope: str = "UNKNOWN",
) -> Optional[RateLimitObservation]:
    """Extract only allowlisted numeric rate metadata."""
    now_epoch = float((now or __import__("time").time)())
    status = http_status
    if status is None and exc is not None:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
    status = _clean_number(status, integer=True)
    source_headers = headers if headers is not None else (_headers_from_exception(exc) if exc is not None else {})
    normalized = {str(key).lower(): value for key, value in source_headers.items()}
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    retry_seconds, retry_epoch = parse_retry_after(normalized.get("retry-after"), now_epoch)
    if retry_seconds is not None:
        values["retry_after_seconds"] = retry_seconds
        values["retry_at_epoch"] = retry_epoch
        sources["retry_after_seconds"] = ObservationSource.PROVIDER_METADATA.value
        sources["retry_at_epoch"] = ObservationSource.PROVIDER_METADATA.value
    for header, field_name in _HEADER_TO_FIELD.items():
        raw = normalized.get(header)
        if raw is None:
            continue
        if field_name.endswith("reset_epoch"):
            numeric = _clean_number(raw)
            if numeric is not None and numeric < now_epoch:
                numeric = now_epoch + numeric
        else:
            numeric = _clean_number(raw, integer=True)
        if numeric is not None:
            values[field_name] = numeric
            sources[field_name] = ObservationSource.PROVIDER_METADATA.value
    if status is not None:
        sources["http_status"] = ObservationSource.OBSERVED_RUNTIME.value
    inferred = classification or ("RATE_LIMITED" if status == 429 else "UNKNOWN")
    if status is None and not values and inferred == "UNKNOWN":
        return None
    evidence_source = (
        ObservationSource.PROVIDER_METADATA.value
        if values else ObservationSource.OBSERVED_RUNTIME.value
    )
    return RateLimitObservation(
        provider_id=provider_id,
        model_id=model_id,
        task_class=task_class,
        observed_at=_iso_utc(now_epoch),
        http_status=status,
        classification=inferred,
        quota_scope=quota_scope,
        evidence_source=evidence_source,
        field_sources=sources,
        **values,
    )


def merge_rate_limit_observations(
    current: Optional[RateLimitObservation],
    incoming: RateLimitObservation,
    *,
    now_epoch: Optional[float] = None,
) -> RateLimitObservation:
    if current is None:
        return incoming
    if (current.provider_id, current.model_id, current.task_class, current.quota_scope) != (
        incoming.provider_id, incoming.model_id, incoming.task_class, incoming.quota_scope
    ):
        raise ValueError("observation keys do not match")
    values: Dict[str, Any] = {}
    sources = dict(current.field_sources or {})
    wall_now = now_epoch
    for name in _NUMERIC_FIELDS | {"http_status"}:
        old_value = getattr(current, name)
        new_value = getattr(incoming, name)
        old_source = sources.get(name, current.evidence_source)
        new_source = (incoming.field_sources or {}).get(name, incoming.evidence_source)
        choose_new = new_value is not None and (
            old_value is None or _SOURCE_RANK[new_source] > _SOURCE_RANK[old_source]
            or (_SOURCE_RANK[new_source] == _SOURCE_RANK[old_source] and incoming.observed_at >= current.observed_at)
        )
        if name in {"retry_at_epoch", "request_reset_epoch", "token_reset_epoch"} and old_value is not None and new_value is not None:
            active = wall_now is None or float(old_value) > wall_now
            if active and _SOURCE_RANK[new_source] < _SOURCE_RANK[old_source] and float(new_value) < float(old_value):
                choose_new = False
        values[name] = new_value if choose_new else old_value
        if choose_new:
            sources[name] = new_source
    newest = incoming if incoming.observed_at >= current.observed_at else current
    return replace(
        newest,
        field_sources=sources,
        **values,
    )


def safe_contract_detail(**detail: Any) -> Dict[str, Any]:
    """Return a bounded allowlisted contract diagnostic without response data."""
    allowed = {
        "exception_class",
        "http_status",
        "provider_code",
        "response_shape",
        "parser_class",
        "line",
        "column",
        "validator_keyword",
        "json_pointer",
        "refusal_code",
        "finish_reason",
        "choice_count",
    }
    result: Dict[str, Any] = {}
    for key, value in detail.items():
        if key not in allowed or value is None:
            continue
        if key in {"http_status", "line", "column", "choice_count"}:
            numeric = _clean_number(value, integer=True)
            if numeric is not None:
                result[key] = numeric
        else:
            text = str(value)
            if len(text) <= 128 and all(ch.isalnum() or ch in "._-/$" for ch in text):
                result[key] = text
    return result
