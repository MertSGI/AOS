"""Backend-neutral compatibility gate for external agent session resume."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from extensions.autonomy_fabric.execution_backend import AgenticSessionIdentity


class ResumeCompatibility(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    MISSING_SESSION_ID = "MISSING_SESSION_ID"
    BACKEND_MISMATCH = "BACKEND_MISMATCH"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    WORKSPACE_CHANGED = "WORKSPACE_CHANGED"
    ADAPTER_CHANGED = "ADAPTER_CHANGED"
    EXECUTABLE_CHANGED = "EXECUTABLE_CHANGED"
    AUTH_MODE_CHANGED = "AUTH_MODE_CHANGED"
    OBJECTIVE_TERMINAL = "OBJECTIVE_TERMINAL"


@dataclass(frozen=True)
class ResumeCompatibilityResult:
    compatible: bool
    reason: ResumeCompatibility


def evaluate_agentic_resume(
    identity: AgenticSessionIdentity,
    *,
    backend_id: str,
    resource_id: str,
    source_sha: str,
    workspace_fingerprint: str,
    adapter_contract_version: str,
    executable_sha256: Optional[str],
    auth_mode: str,
    objective_terminal: bool = False,
) -> ResumeCompatibilityResult:
    checks = (
        (objective_terminal, ResumeCompatibility.OBJECTIVE_TERMINAL),
        (not identity.session_or_thread_id, ResumeCompatibility.MISSING_SESSION_ID),
        (identity.backend_id != backend_id, ResumeCompatibility.BACKEND_MISMATCH),
        (identity.resource_id != resource_id, ResumeCompatibility.RESOURCE_MISMATCH),
        (identity.source_sha != source_sha, ResumeCompatibility.SOURCE_CHANGED),
        (
            identity.workspace_fingerprint != workspace_fingerprint,
            ResumeCompatibility.WORKSPACE_CHANGED,
        ),
        (
            identity.adapter_contract_version != adapter_contract_version,
            ResumeCompatibility.ADAPTER_CHANGED,
        ),
        (
            bool(identity.executable_sha256)
            and identity.executable_sha256 != executable_sha256,
            ResumeCompatibility.EXECUTABLE_CHANGED,
        ),
        (
            bool(identity.auth_mode) and identity.auth_mode != auth_mode,
            ResumeCompatibility.AUTH_MODE_CHANGED,
        ),
    )
    for failed, reason in checks:
        if failed:
            return ResumeCompatibilityResult(False, reason)
    return ResumeCompatibilityResult(True, ResumeCompatibility.COMPATIBLE)
