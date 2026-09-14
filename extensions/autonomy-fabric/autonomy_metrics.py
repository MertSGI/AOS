"""AOS Autonomy Completeness Metrics (V2).

Tracks explicit dimensional readiness states rather than relying solely on a single global percentage:
- AOS_ORCHESTRATION
- EXECUTOR_INDEPENDENCE
- PERSISTENT_COORDINATOR
- MODEL_FABRIC
- DESIGN_INTELLIGENCE
- CI_AUTOMATION
- CONTROLLER_RELAY
- PRODUCTION_READINESS

Valid state values:
NOT_STARTED, PARTIAL, SOURCE_PROVEN, RUNTIME_PROVEN, STABLE_ACCEPTED
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Dict, Any, Optional
import datetime


class MetricState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PARTIAL = "PARTIAL"
    SOURCE_PROVEN = "SOURCE_PROVEN"
    RUNTIME_PROVEN = "RUNTIME_PROVEN"
    STABLE_ACCEPTED = "STABLE_ACCEPTED"


@dataclass
class AutonomyCompletenessProfile:
    aos_orchestration: MetricState = MetricState.RUNTIME_PROVEN
    executor_independence: MetricState = MetricState.RUNTIME_PROVEN
    persistent_coordinator: MetricState = MetricState.RUNTIME_PROVEN
    model_fabric: MetricState = MetricState.RUNTIME_PROVEN
    design_intelligence: MetricState = MetricState.RUNTIME_PROVEN
    ci_automation: MetricState = MetricState.RUNTIME_PROVEN
    controller_relay: MetricState = MetricState.PARTIAL
    production_readiness: MetricState = MetricState.PARTIAL
    ag_optional: bool = True
    ag_default_required: bool = False
    legacy_progress_percentage: float = 88.0
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, MetricState):
                d[k] = v.value
        return d

    def summary(self) -> str:
        lines = [
            "============================================================",
            "AOS AUTONOMY COMPLETENESS PROFILE (V2)",
            "============================================================",
            f"AOS_ORCHESTRATION:         {self.aos_orchestration.value}",
            f"EXECUTOR_INDEPENDENCE:     {self.executor_independence.value}",
            f"PERSISTENT_COORDINATOR:    {self.persistent_coordinator.value}",
            f"MODEL_FABRIC:              {self.model_fabric.value}",
            f"DESIGN_INTELLIGENCE:       {self.design_intelligence.value}",
            f"CI_AUTOMATION:             {self.ci_automation.value}",
            f"CONTROLLER_RELAY:          {self.controller_relay.value}",
            f"PRODUCTION_READINESS:      {self.production_readiness.value}",
            f"AG_OPTIONAL:               {'TRUE' if self.ag_optional else 'FALSE'}",
            f"AG_DEFAULT_REQUIRED:       {'TRUE' if self.ag_default_required else 'FALSE'}",
            f"LEGACY_PROGRESS:           {self.legacy_progress_percentage}%",
            "============================================================",
        ]
        return "\n".join(lines)
