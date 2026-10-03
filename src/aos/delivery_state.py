"""Durable delivery state tracking for AOS autonomous delivery closure."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from enum import Enum

class DeliveryStage(str, Enum):
    PREPARING = "PREPARING"
    STAGED = "STAGED"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    WAITING_FOR_CI = "WAITING_FOR_CI"
    CI_COMPLETE = "CI_COMPLETE"
    ACCEPTED = "ACCEPTED"

_STAGE_ORDER = [
    DeliveryStage.PREPARING.value,
    DeliveryStage.STAGED.value,
    DeliveryStage.COMMITTED.value,
    DeliveryStage.PUSHED.value,
    DeliveryStage.WAITING_FOR_CI.value,
    DeliveryStage.CI_COMPLETE.value,
    DeliveryStage.ACCEPTED.value,
]

_VALID_STAGES = frozenset(_STAGE_ORDER)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(data), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclasses.dataclass
class DeliveryState:
    delivery_id: str
    objective_id: str
    execution_base_sha: str
    candidate_sha: Optional[str] = None
    stage: str = DeliveryStage.PREPARING.value
    ci_workflow_identity: Optional[str] = None
    ci_run_id: Optional[int] = None
    ci_conclusion: Optional[str] = None
    ci_attempt_count: int = 0
    push_target_branch: str = "main"
    created_at: str = dataclasses.field(default_factory=_utc_now)
    updated_at: str = dataclasses.field(default_factory=_utc_now)
    production: str = "NO_GO"

    def __post_init__(self) -> None:
        if isinstance(self.stage, DeliveryStage):
            self.stage = self.stage.value
        if self.stage not in _VALID_STAGES:
            raise ValueError(f"Invalid delivery stage: {self.stage}")
        if self.production != "NO_GO":
            raise ValueError("production must always be NO_GO")

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeliveryState:
        allowed = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


def read_delivery_state(runtime_dir: Path) -> Optional[DeliveryState]:
    path = runtime_dir / "delivery-state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "delivery_id" in data:
            return DeliveryState.from_dict(data)
    except Exception:
        pass
    return None


def write_delivery_state(runtime_dir: Path, state: DeliveryState) -> None:
    state.updated_at = _utc_now()
    path = runtime_dir / "delivery-state.json"
    _atomic_json(path, state.to_dict())


def advance_delivery_stage(runtime_dir: Path, new_stage: DeliveryStage | str, **updates: Any) -> DeliveryState:
    current = read_delivery_state(runtime_dir)
    if not current:
        raise ValueError(f"No existing delivery-state.json found in {runtime_dir}")
    stage_val = new_stage.value if isinstance(new_stage, DeliveryStage) else str(new_stage)
    if stage_val not in _VALID_STAGES:
        raise ValueError(f"Invalid delivery stage: {stage_val}")

    current_idx = _STAGE_ORDER.index(current.stage)
    new_idx = _STAGE_ORDER.index(stage_val)
    if new_idx < current_idx:
        raise ValueError(f"Cannot transition backwards from {current.stage} to {stage_val}")

    current_dict = current.to_dict()
    current_dict["stage"] = stage_val
    current_dict.update(updates)
    updated = DeliveryState.from_dict(current_dict)
    write_delivery_state(runtime_dir, updated)
    return updated

