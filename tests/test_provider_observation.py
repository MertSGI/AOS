import datetime as dt

import pytest

from aos.provider_observation import (
    ContractFailureSubtype,
    ObservationSource,
    RateLimitObservation,
    extract_rate_limit_observation,
    merge_rate_limit_observations,
    safe_contract_detail,
)


class _Response:
    def __init__(self, headers, status_code=429):
        self.headers = headers
        self.status_code = status_code


class _SyntheticRateLimit(Exception):
    def __init__(self, headers):
        super().__init__("secret=sk-should-never-persist")
        self.status_code = 429
        self.response = _Response(headers)


def _observation(headers, now=1000.0):
    return extract_rate_limit_observation(
        provider_id="groq",
        model_id="model",
        task_class="repo_ui_planning",
        exc=_SyntheticRateLimit(headers),
        now=lambda: now,
    )


def test_retry_after_delta_and_sanitization():
    observation = _observation({
        "Retry-After": "120",
        "X-RateLimit-Remaining-Tokens": "0",
        "Authorization": "Bearer sk-forbidden",
        "Cookie": "forbidden",
        "X-Arbitrary": "https://example.test/?secret=1",
    })
    assert observation is not None
    assert observation.retry_at_epoch == 1120.0
    assert observation.retry_after_seconds == 120.0
    assert observation.token_remaining == 0
    serialized = str(observation.to_dict())
    assert "Authorization" not in serialized
    assert "Cookie" not in serialized
    assert "sk-forbidden" not in serialized
    assert "example.test" not in serialized


def test_retry_after_http_date_is_exact():
    target = dt.datetime.fromtimestamp(1090.0, tz=dt.timezone.utc)
    observation = _observation({"retry-after": target.strftime("%a, %d %b %Y %H:%M:%S GMT")})
    assert observation is not None
    assert observation.retry_at_epoch == 1090.0
    assert observation.retry_after_seconds == 90.0


def test_invalid_rate_values_are_dropped():
    observation = _observation({
        "retry-after": "-1",
        "x-ratelimit-limit-tokens": "NaN",
        "x-ratelimit-remaining-tokens": "not-a-number",
    })
    assert observation is not None
    assert observation.http_status == 429
    assert observation.retry_at_epoch is None
    assert observation.token_limit is None


def test_truth_order_is_per_field_and_weaker_deadline_cannot_shorten():
    estimate = RateLimitObservation(
        provider_id="groq",
        model_id="model",
        task_class="repo_ui_planning",
        observed_at="2026-09-23T00:00:00+00:00",
        classification="RATE_LIMITED",
        retry_at_epoch=1200.0,
        evidence_source=ObservationSource.ADAPTIVE_ESTIMATE.value,
        field_sources={"retry_at_epoch": ObservationSource.ADAPTIVE_ESTIMATE.value},
    )
    provider = RateLimitObservation(
        provider_id="groq",
        model_id="model",
        task_class="repo_ui_planning",
        observed_at="2026-09-23T00:01:00+00:00",
        classification="RATE_LIMITED",
        retry_at_epoch=1300.0,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
        field_sources={"retry_at_epoch": ObservationSource.PROVIDER_METADATA.value},
    )
    merged = merge_rate_limit_observations(estimate, provider, now_epoch=1000.0)
    assert merged.retry_at_epoch == 1300.0
    weaker = RateLimitObservation(
        provider_id="groq",
        model_id="model",
        task_class="repo_ui_planning",
        observed_at="2026-09-23T00:02:00+00:00",
        classification="RATE_LIMITED",
        retry_at_epoch=1100.0,
        evidence_source=ObservationSource.ADAPTIVE_ESTIMATE.value,
        field_sources={"retry_at_epoch": ObservationSource.ADAPTIVE_ESTIMATE.value},
    )
    assert merge_rate_limit_observations(merged, weaker, now_epoch=1000.0).retry_at_epoch == 1300.0


def test_contract_detail_allowlist_drops_raw_values():
    detail = safe_contract_detail(
        parser_class="JSONDecodeError",
        line=4,
        column=8,
        raw_content="sk-secret",
        provider_code="bad_request",
    )
    assert detail == {
        "parser_class": "JSONDecodeError",
        "line": 4,
        "column": 8,
        "provider_code": "bad_request",
    }
    assert ContractFailureSubtype.INVALID_JSON.value == "INVALID_JSON"


def test_unknown_task_class_is_rejected():
    with pytest.raises(ValueError, match="unknown task_class"):
        RateLimitObservation(
            provider_id="groq",
            model_id=None,
            task_class="invented",
            observed_at="2026-09-23T00:00:00+00:00",
        )
