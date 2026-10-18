"""Versioned trusted execution authorization for Antigravity agentic worker.

Provides explicit, per-project opt-in authority controls for Antigravity CLI execution,
subordinate to AOS resource orchestration, task-class restrictions, workspace boundaries,
and product lane isolation.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple


PROTECTED_PRODUCT_PROJECT_IDS = frozenset({"lari", "lari-ui-v2"})

DEFAULT_ALLOWED_TASK_CLASSES: Tuple[str, ...] = (
    "LONG_HORIZON_AGENTIC_WORK",
    "AGENTIC_MUTATION",
)

DISALLOWED_TASK_CLASSES: Tuple[str, ...] = (
    "DETERMINISTIC",
    "SIMPLE_REASONING",
    "NATIVE_PROCESS",
    "NATIVE_FILE",
    "CODE_REASONING_DIRECT",
)


@dataclass(frozen=True)
class AntigravityExecutionAuthority:
    """Explicit per-project opt-in authority record for Antigravity worker execution."""
    project_id: str
    enabled: bool = False
    allowed_task_classes: Tuple[str, ...] = DEFAULT_ALLOWED_TASK_CLASSES
    allowed_workspaces: Tuple[str, ...] = ()
    quota_limit_tokens: int = 100_000
    max_turns_per_invocation: int = 30
    require_proven_attestation: bool = True
    allow_product_lane: bool = False
    authority_version: str = "1.0.0"
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "enabled": self.enabled,
            "allowed_task_classes": list(self.allowed_task_classes),
            "allowed_workspaces": list(self.allowed_workspaces),
            "quota_limit_tokens": self.quota_limit_tokens,
            "max_turns_per_invocation": self.max_turns_per_invocation,
            "require_proven_attestation": self.require_proven_attestation,
            "allow_product_lane": self.allow_product_lane,
            "authority_version": self.authority_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> AntigravityExecutionAuthority:
        project_id = str(mapping.get("project_id", "")).strip()
        if not project_id:
            raise ValueError("project_id is required for AntigravityExecutionAuthority")
        raw_classes = mapping.get("allowed_task_classes", DEFAULT_ALLOWED_TASK_CLASSES)
        allowed_classes = tuple(str(c).strip().upper() for c in raw_classes if str(c).strip())
        raw_workspaces = mapping.get("allowed_workspaces", ())
        allowed_workspaces = tuple(
            str(Path(w).resolve()) for w in raw_workspaces if str(w).strip()
        )
        return cls(
            project_id=project_id,
            enabled=bool(mapping.get("enabled", False)),
            allowed_task_classes=allowed_classes,
            allowed_workspaces=allowed_workspaces,
            quota_limit_tokens=int(mapping.get("quota_limit_tokens", 100_000)),
            max_turns_per_invocation=int(mapping.get("max_turns_per_invocation", 30)),
            require_proven_attestation=bool(mapping.get("require_proven_attestation", True)),
            allow_product_lane=bool(mapping.get("allow_product_lane", False)),
            authority_version=str(mapping.get("authority_version", "1.0.0")),
            created_at=str(
                mapping.get("created_at")
                or datetime.datetime.now(datetime.timezone.utc).isoformat()
            ),
        )


def evaluate_antigravity_authority(
    authority: Optional[AntigravityExecutionAuthority],
    *,
    project_id: str,
    task_class: str,
    workspace_path: Optional[str | Path] = None,
    capability_status: str = "UNPROVEN",
) -> Tuple[bool, str]:
    """Evaluate whether an execution request satisfies explicit Antigravity authority gates.

    Returns:
        (authorized: bool, reason: str)
    """
    normalized_project = project_id.strip().lower()

    # Gate 1: Historical / Protected product lane isolation
    if normalized_project in PROTECTED_PRODUCT_PROJECT_IDS:
        return False, "PRODUCT_LANE_FORBIDDEN"

    if authority is None:
        return False, "NO_AUTHORITY_RECORD"

    if authority.project_id.strip().lower() != normalized_project:
        return False, "PROJECT_IDENTITY_MISMATCH"

    if not authority.enabled:
        return False, "AUTHORITY_DISABLED"

    if not authority.allow_product_lane and normalized_project in PROTECTED_PRODUCT_PROJECT_IDS:
        return False, "PRODUCT_LANE_FORBIDDEN"

    # Gate 2: Task-class restriction
    normalized_task_class = task_class.strip().upper()
    if normalized_task_class in DISALLOWED_TASK_CLASSES:
        return False, f"TASK_CLASS_DISALLOWED:{normalized_task_class}"

    if authority.allowed_task_classes and normalized_task_class not in authority.allowed_task_classes:
        return False, f"TASK_CLASS_INELIGIBLE:{normalized_task_class}"

    # Gate 3: Bound workspace / write scope
    if authority.allowed_workspaces and workspace_path is not None:
        resolved_ws = Path(workspace_path).resolve()
        in_bounds = any(
            resolved_ws == Path(allowed_ws).resolve()
            or resolved_ws.is_relative_to(Path(allowed_ws).resolve())
            for allowed_ws in authority.allowed_workspaces
        )
        if not in_bounds:
            return False, "WORKSPACE_OUT_OF_BOUNDS"

    # Gate 4: Verified capability attestation
    if authority.require_proven_attestation:
        if capability_status not in {"PROVEN", "TEST_DOUBLE"}:
            return False, f"ATTESTATION_UNPROVEN:{capability_status}"

    return True, "AUTHORIZED"
