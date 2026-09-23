"""Durable quota state, intentionally separate from provider health."""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from aos.provider_observation import (
    ObservationSource,
    RateLimitObservation,
    canonical_task_class,
    merge_rate_limit_observations,
)
from aos.runtime_store import atomic_json, exclusive_file_lock


class QuotaState(str, Enum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    CONSTRAINED = "CONSTRAINED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class QuotaDecision:
    state: str
    eligible: bool
    retry_at_epoch: Optional[float]
    reason: str
    evidence_source: str
    key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "eligible": self.eligible,
            "retry_at_epoch": self.retry_at_epoch,
            "reason": self.reason,
            "evidence_source": self.evidence_source,
            "key": self.key,
        }


def _quota_key(observation: RateLimitObservation) -> str:
    return "|".join((
        observation.provider_id,
        observation.model_id or "*",
        observation.quota_scope,
        observation.task_class,
    ))


def _parse_observed_at(value: str) -> float:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


class QuotaGovernor:
    schema_version = "1.0.0"

    def __init__(
        self,
        persistence_path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.time,
        scarcity_ratio: float = 0.10,
    ) -> None:
        self.persistence_path = persistence_path
        self.clock = clock
        self.scarcity_ratio = max(0.0, min(float(scarcity_ratio), 1.0))
        self._observations: Dict[str, RateLimitObservation] = {}
        self._corrupt = False
        self.load()

    def load(self) -> None:
        if self.persistence_path is None or not self.persistence_path.exists():
            return
        try:
            data = json.loads(self.persistence_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != self.schema_version:
                raise ValueError("unsupported quota snapshot")
            records = data.get("records", {})
            if not isinstance(records, dict):
                raise ValueError("quota records must be an object")
            loaded: Dict[str, RateLimitObservation] = {}
            for key, raw in records.items():
                if not isinstance(raw, dict):
                    raise ValueError("quota record must be an object")
                observation = RateLimitObservation.from_dict(raw)
                if _quota_key(observation) != key:
                    raise ValueError("quota record key mismatch")
                loaded[key] = observation
            self._observations = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._observations = {}
            self._corrupt = True

    def save(self) -> None:
        if self.persistence_path is None:
            return
        lock_path = self.persistence_path.with_suffix(self.persistence_path.suffix + ".lock")
        with exclusive_file_lock(lock_path):
            atomic_json(self.persistence_path, {
                "schema_version": self.schema_version,
                "updated_at_epoch": self.clock(),
                "records": {
                    key: observation.to_dict()
                    for key, observation in sorted(self._observations.items())
                },
            })

    def record(self, observation: RateLimitObservation) -> QuotaDecision:
        if not isinstance(observation, RateLimitObservation):
            raise TypeError("QuotaGovernor accepts only RateLimitObservation")
        key = _quota_key(observation)
        current = self._observations.get(key)
        self._observations[key] = merge_rate_limit_observations(
            current, observation, now_epoch=self.clock()
        )
        self._corrupt = False
        self.save()
        return self.decision(
            observation.provider_id,
            observation.model_id,
            observation.task_class,
            quota_scope=observation.quota_scope,
        )

    @staticmethod
    def _deadline(observation: RateLimitObservation) -> Optional[float]:
        values = [
            value for value in (
                observation.retry_at_epoch,
                observation.request_reset_epoch,
                observation.token_reset_epoch,
            ) if value is not None
        ]
        return max(values) if values else None

    def _decision_for(self, key: str, observation: RateLimitObservation) -> QuotaDecision:
        now = self.clock()
        deadline = self._deadline(observation)
        remaining_values = [
            value for value in (observation.request_remaining, observation.token_remaining)
            if value is not None
        ]
        exhausted_marker = observation.classification in {
            "RATE_LIMITED", "QUOTA_EXHAUSTED", "CREDIT_EXHAUSTED"
        } or any(value == 0 for value in remaining_values)
        if exhausted_marker and (deadline is None or deadline > now):
            return QuotaDecision(
                QuotaState.EXHAUSTED.value,
                False,
                deadline,
                observation.classification,
                observation.evidence_source,
                key,
            )
        if exhausted_marker and deadline is not None and deadline <= now:
            return QuotaDecision(
                QuotaState.AVAILABLE.value,
                True,
                None,
                "WINDOW_EXPIRED",
                observation.evidence_source,
                key,
            )
        ratios = []
        if observation.request_limit and observation.request_remaining is not None:
            ratios.append(observation.request_remaining / observation.request_limit)
        if observation.token_limit and observation.token_remaining is not None:
            ratios.append(observation.token_remaining / observation.token_limit)
        if ratios and min(ratios) <= self.scarcity_ratio:
            return QuotaDecision(
                QuotaState.CONSTRAINED.value,
                True,
                deadline,
                "LOW_REMAINING",
                observation.evidence_source,
                key,
            )
        if remaining_values or observation.classification == "AVAILABLE":
            return QuotaDecision(
                QuotaState.AVAILABLE.value,
                True,
                None,
                "OBSERVED_AVAILABLE",
                observation.evidence_source,
                key,
            )
        return QuotaDecision(
            QuotaState.UNKNOWN.value,
            True,
            None,
            "NO_BLOCKING_QUOTA_EVIDENCE",
            observation.evidence_source,
            key,
        )

    def decision(
        self,
        provider_id: str,
        model_id: Optional[str],
        task_class: str,
        *,
        quota_scope: Optional[str] = None,
    ) -> QuotaDecision:
        if self._corrupt:
            return QuotaDecision(
                QuotaState.UNKNOWN.value,
                False,
                None,
                "CORRUPT_QUOTA_STATE_FAIL_CLOSED",
                ObservationSource.OBSERVED_RUNTIME.value,
            )
        canonical_task = canonical_task_class(task_class)
        matches = [
            (key, observation)
            for key, observation in self._observations.items()
            if observation.provider_id == provider_id
            and observation.model_id in (None, model_id)
            and observation.task_class == canonical_task
            and (quota_scope is None or observation.quota_scope == quota_scope)
        ]
        if not matches:
            return QuotaDecision(
                QuotaState.UNKNOWN.value,
                True,
                None,
                "NO_QUOTA_OBSERVATION",
                ObservationSource.OBSERVED_RUNTIME.value,
            )
        decisions = [self._decision_for(key, observation) for key, observation in matches]
        blocked = [decision for decision in decisions if not decision.eligible]
        if blocked:
            return max(blocked, key=lambda item: item.retry_at_epoch or float("inf"))
        constrained = [decision for decision in decisions if decision.state == QuotaState.CONSTRAINED.value]
        if constrained:
            return max(constrained, key=lambda item: item.retry_at_epoch or 0.0)
        return max(
            decisions,
            key=lambda item: _parse_observed_at(
                self._observations[item.key].observed_at if item.key else ""
            ),
        )

    def earliest_retry(
        self,
        provider_ids: Optional[Iterable[str]] = None,
        *,
        task_class: Optional[str] = None,
    ) -> Optional[float]:
        allowed = set(provider_ids) if provider_ids is not None else None
        deadlines = []
        for observation in self._observations.values():
            if allowed is not None and observation.provider_id not in allowed:
                continue
            if task_class is not None and observation.task_class != canonical_task_class(task_class):
                continue
            decision = self._decision_for(_quota_key(observation), observation)
            if not decision.eligible and decision.retry_at_epoch is not None:
                deadlines.append(decision.retry_at_epoch)
        return min(deadlines) if deadlines else None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corrupt": self._corrupt,
            "records": {
                key: observation.to_dict()
                for key, observation in sorted(self._observations.items())
            },
        }
