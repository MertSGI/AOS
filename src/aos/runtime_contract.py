"""AOS Runtime Contract V1.

This module defines the single versioned IPC contract used by the detached AOS
runtime, AOS Direct, and the administrative CLI.  The contract intentionally
contains no secret-bearing fields and keeps production disabled.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

CONTRACT_VERSION = "1.0.0"
PRODUCTION_STATE = "NO_GO"
MAX_GOAL_CHARS = 8000
MAX_LIST_ITEMS = 64
MAX_ITEM_CHARS = 2000
COMMAND_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,96}$")
TERMINAL_STATES = frozenset({"PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"})
NONTERMINAL_STATES = frozenset({
    "QUEUED", "RUNNING", "WAITING_FOR_REASONING_PROVIDER", "RECOVERING",
})

_PROHIBITED_KEYS = frozenset({
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "refresh_token", "private_key", "client_secret", "authorization",
    "bearer_token", "credential", "credentials",
})


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _contains_prohibited_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _PROHIBITED_KEYS:
                return True
            if _contains_prohibited_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_prohibited_key(item) for item in value)
    return False


def _bounded_text(value: Any, limit: int, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{field_name} is required")
    if len(text) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return text


def _string_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field_name} has too many items")
    result = []
    for item in value:
        result.append(_bounded_text(item, MAX_ITEM_CHARS, field_name))
    return tuple(result)


def new_command_id(prefix: str = "run") -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    descriptor_path: str
    workspace: str
    routing_policy_path: str
    standing_authority: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProjectProfile":
        if _contains_prohibited_key(value):
            raise ValueError("Project profile contains a prohibited secret-bearing key")
        return cls(
            project_id=_bounded_text(value.get("project_id"), 120, "project_id"),
            descriptor_path=_bounded_text(value.get("descriptor_path"), 4096, "descriptor_path"),
            workspace=_bounded_text(value.get("workspace"), 4096, "workspace"),
            routing_policy_path=_bounded_text(value.get("routing_policy_path"), 4096, "routing_policy_path"),
            standing_authority=bool(value.get("standing_authority", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "descriptor_path": self.descriptor_path,
            "workspace": self.workspace,
            "routing_policy_path": self.routing_policy_path,
            "standing_authority": self.standing_authority,
        }


@dataclass(frozen=True)
class ContinueProjectCommand:
    command_id: str
    project: ProjectProfile
    goal: str
    constraints: Tuple[str, ...] = ()
    red_lines: Tuple[str, ...] = ()
    max_batches_per_cycle: int = 1
    max_iterations_per_batch: int = 30
    continuous: bool = True
    created_at: str = field(default_factory=utc_now)
    contract_version: str = CONTRACT_VERSION
    command_type: str = "continue_project"
    production: str = PRODUCTION_STATE
    ag_backend_enabled: bool = False

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        project: Optional[ProjectProfile] = None,
    ) -> "ContinueProjectCommand":
        if _contains_prohibited_key(value):
            raise ValueError("Runtime command contains a prohibited secret-bearing key")
        version = str(value.get("contract_version", CONTRACT_VERSION))
        if version != CONTRACT_VERSION:
            raise ValueError(f"Unsupported Runtime Contract version: {version}")
        if value.get("production", PRODUCTION_STATE) != PRODUCTION_STATE:
            raise ValueError("Runtime Contract V1 requires production=NO_GO")
        if bool(value.get("ag_backend_enabled", False)):
            raise ValueError("Runtime Contract V1 requires ag_backend_enabled=false")
        command_id = value.get("command_id") or new_command_id("continue")
        command_id = _bounded_text(command_id, 96, "command_id")
        if not COMMAND_ID_RE.fullmatch(command_id):
            raise ValueError("Invalid command_id")
        command_type = str(value.get("command_type", "continue_project"))
        if command_type != "continue_project":
            raise ValueError(f"Unsupported command_type: {command_type}")
        if project is None:
            raw_project = value.get("project")
            if not isinstance(raw_project, Mapping):
                raise ValueError("Runtime command requires a project profile")
            project = ProjectProfile.from_mapping(raw_project)
        goal = _bounded_text(value.get("goal"), MAX_GOAL_CHARS, "goal")
        max_batches = int(value.get("max_batches_per_cycle", 1))
        max_iterations = int(value.get("max_iterations_per_batch", 30))
        if not 1 <= max_batches <= 50:
            raise ValueError("max_batches_per_cycle must be between 1 and 50")
        if not 1 <= max_iterations <= 500:
            raise ValueError("max_iterations_per_batch must be between 1 and 500")
        return cls(
            command_id=command_id,
            project=project,
            goal=goal,
            constraints=_string_tuple(value.get("constraints", []), "constraints"),
            red_lines=_string_tuple(value.get("red_lines", []), "red_lines"),
            max_batches_per_cycle=max_batches,
            max_iterations_per_batch=max_iterations,
            continuous=bool(value.get("continuous", True)),
            created_at=str(value.get("created_at") or utc_now()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "command_type": self.command_type,
            "command_id": self.command_id,
            "created_at": self.created_at,
            "project": self.project.to_dict(),
            "goal": self.goal,
            "constraints": list(self.constraints),
            "red_lines": list(self.red_lines),
            "max_batches_per_cycle": self.max_batches_per_cycle,
            "max_iterations_per_batch": self.max_iterations_per_batch,
            "continuous": self.continuous,
            "production": self.production,
            "ag_backend_enabled": self.ag_backend_enabled,
        }


@dataclass(frozen=True)
class RuntimeEvent:
    command_id: str
    event_type: str
    seq: int
    payload: Dict[str, Any]
    timestamp: str = field(default_factory=utc_now)
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        if _contains_prohibited_key(self.payload):
            raise ValueError("Runtime event payload contains a prohibited secret-bearing key")
        return {
            "contract_version": self.contract_version,
            "command_id": self.command_id,
            "event_type": self.event_type,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class RuntimeResult:
    command_id: str
    state: str
    disposition: str
    completed_batch_count: int
    canonical_source_sha: Optional[str]
    canonical_execution_base_sha: Optional[str]
    receipt: Dict[str, Any]
    finished_at: str = field(default_factory=utc_now)
    contract_version: str = CONTRACT_VERSION
    production: str = PRODUCTION_STATE
    ag_invocation_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        if _contains_prohibited_key(self.receipt):
            raise ValueError("Runtime result contains a prohibited secret-bearing key")
        return {
            "contract_version": self.contract_version,
            "command_id": self.command_id,
            "state": self.state,
            "disposition": self.disposition,
            "completed_batch_count": int(self.completed_batch_count),
            "canonical_source_sha": self.canonical_source_sha,
            "canonical_execution_base_sha": self.canonical_execution_base_sha,
            "receipt": self.receipt,
            "finished_at": self.finished_at,
            "production": self.production,
            "ag_invocation_count": int(self.ag_invocation_count),
        }


def validate_runtime_config(value: Mapping[str, Any]) -> Dict[str, Any]:
    if _contains_prohibited_key(value):
        raise ValueError("Runtime config contains a prohibited secret-bearing key")
    if value.get("contract_version", CONTRACT_VERSION) != CONTRACT_VERSION:
        raise ValueError("Runtime config contract_version must be 1.0.0")
    if value.get("production") != PRODUCTION_STATE:
        raise ValueError("Runtime config production must remain NO_GO")
    if value.get("ag_backend_enabled") is not False:
        raise ValueError("Runtime config requires ag_backend_enabled=false")
    bind_host = str(value.get("bind_host", "127.0.0.1"))
    if bind_host not in ("127.0.0.1", "localhost"):
        raise ValueError("Runtime API must bind to loopback")
    port = int(value.get("port", 8770))
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid Runtime API port")
    runtime_root = Path(_bounded_text(value.get("runtime_root"), 4096, "runtime_root")).expanduser().resolve()
    roots = value.get("authorized_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("Runtime config requires authorized_roots")
    authorized_roots = [str(Path(str(item)).expanduser().resolve()) for item in roots]
    projects_raw = value.get("projects")
    if not isinstance(projects_raw, Mapping) or not projects_raw:
        raise ValueError("Runtime config requires projects mapping")
    projects = {str(key): ProjectProfile.from_mapping(project).to_dict() for key, project in projects_raw.items()}
    default_project = str(value.get("default_project", ""))
    if default_project not in projects:
        raise ValueError("Runtime config default_project must exist in projects")

    # Verify workspace disjointness across all project profiles to guarantee
    # safe concurrent multi-lane execution without state pollution or git collisions.
    normalized_workspaces: Dict[str, Path] = {}
    for proj_id, proj_dict in projects.items():
        w_path = Path(proj_dict["workspace"]).expanduser().resolve()
        for other_id, other_path in normalized_workspaces.items():
            if w_path == other_path:
                raise ValueError(
                    f"Workspace collision detected: project '{proj_id}' and '{other_id}' share identical workspace '{w_path}'"
                )
            try:
                w_path.relative_to(other_path)
                raise ValueError(
                    f"Workspace nesting collision: project '{proj_id}' workspace '{w_path}' is inside '{other_id}' workspace '{other_path}'"
                )
            except ValueError as e:
                if "Workspace nesting collision" in str(e):
                    raise
            try:
                other_path.relative_to(w_path)
                raise ValueError(
                    f"Workspace nesting collision: project '{other_id}' workspace '{other_path}' is inside '{proj_id}' workspace '{w_path}'"
                )
            except ValueError as e:
                if "Workspace nesting collision" in str(e):
                    raise
        normalized_workspaces[proj_id] = w_path

    return {
        **dict(value),
        "contract_version": CONTRACT_VERSION,
        "bind_host": "127.0.0.1",
        "port": port,
        "runtime_root": str(runtime_root),
        "authorized_roots": authorized_roots,
        "projects": projects,
        "default_project": default_project,
        "production": PRODUCTION_STATE,
        "ag_backend_enabled": False,
    }


def resolve_project(config: Mapping[str, Any], project_id: Optional[str]) -> ProjectProfile:
    normalized = validate_runtime_config(config)
    selected = project_id or normalized["default_project"]
    projects = normalized["projects"]
    if selected not in projects:
        raise ValueError(f"Unknown project_id: {selected}")
    return ProjectProfile.from_mapping(projects[selected])


def resolve_under_authorized_roots(path_value: str, roots: Sequence[str]) -> Path:
    candidate = Path(path_value).expanduser().resolve()
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise ValueError(f"Path is outside authorized roots: {candidate}")
