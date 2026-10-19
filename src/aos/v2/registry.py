"""Server-owned, read-only project and resource registry for AOS V2.

Enforces:
- Registry file must exist outside all worker/source write roots.
- Owner ACL / mode validation (not world-writable).
- Schema version 1 format validation.
- Rejection of unknown fields, Product classifications ('lari', 'lari-ui-v2'),
  protected roots (runtime-v1, backups, checkpoints), overlapping roots,
  and shared Git worktrees.
- Rejection of nonportable Windows device names, ADS, trailing dots/spaces,
  symlinks, junctions, reparse points, and hardlinks.
- Expiration and revocation checks.
- Zero-paid / NO_GO enforcement.
- Immutable AuthorizedProject binding.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from aos.v2.lifecycle import (
    _digest,
    _identifier,
    _json,
    _no_links,
    _relative,
    _strict_json_loads,
    validate_private_file,
)


class RegistryError(ValueError):
    """Base error for registry validation failures."""


class RegistryAuthorizationError(RegistryError):
    """Raised when authorization is denied or grant is invalid/expired."""


FORBIDDEN_PRODUCT_NAMES = frozenset({"lari", "lari-ui-v2"})
PROTECTED_PATH_PATTERNS = [
    re.compile(r"(?i)(?:runtime-v1|_ag_checkpoints|_p0-recovery-backups|runtime-config\.json)"),
]
FORBIDDEN_DEVICE_STEMS = frozenset({
    "con", "prn", "aux", "nul", "conin$", "conout$",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


def _validate_safe_path_component(part: str) -> None:
    """Check part against Windows device names, trailing dots/spaces, ADS, and Git metadata."""
    if not part or part == "." or part == "..":
        return
    if part.casefold() == ".git":
        raise RegistryAuthorizationError("Git metadata directory cannot be in write scope")
    if part.rstrip(" .") != part:
        raise RegistryAuthorizationError(f"Path part '{part}' has trailing dot or space")
    if ":" in part:
        raise RegistryAuthorizationError(f"Path part '{part}' contains NTFS alternate data stream syntax")
    if re.search(r'[<>"|?*\x00-\x1f]', part):
        raise RegistryAuthorizationError(f"Path part '{part}' contains invalid characters")
    stem = part.split(".", 1)[0].casefold()
    if stem in FORBIDDEN_DEVICE_STEMS:
        raise RegistryAuthorizationError(f"Path part '{part}' aliases Windows reserved device name")


def validate_scope_path(value: str) -> str:
    """Validate and normalize a relative file scope path."""
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or str(p) != value:
        raise RegistryAuthorizationError(f"Write file path must be normalized relative: '{value}'")
    for part in p.parts:
        _validate_safe_path_component(part)
    return value


def _check_registry_file_permissions(path: Path) -> None:
    """Verify registry file is regular file and not world-writable."""
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise RegistryAuthorizationError(f"Registry path '{path}' is not a regular file")
    # On POSIX, check not world-writable or group-writable
    if os.name != "nt":
        if st.st_mode & 0o022:
            raise RegistryAuthorizationError(f"Registry file '{path}' has unsafe write permissions (0o{st.st_mode:o})")


@dataclass(frozen=True)
class ProjectCriterion:
    id: str
    kind: str  # e.g. "FUNCTION_ORACLE_V1" or "TEST_COMMAND"
    module: str = ""
    symbol: str = ""
    oracle_ref: str = ""
    oracle_sha256: str = ""
    test_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.module:
            res["module"] = self.module
        if self.symbol:
            res["symbol"] = self.symbol
        if self.oracle_ref:
            res["oracle_ref"] = self.oracle_ref
        if self.oracle_sha256:
            res["oracle_sha256"] = self.oracle_sha256
        if self.test_id:
            res["test_id"] = self.test_id
        return res


@dataclass(frozen=True)
class AuthorizedProject:
    project_id: str
    authority_id: str
    classification: str
    workspace: str
    base_sha: str
    write_files: tuple[str, ...]
    criteria: tuple[ProjectCriterion, ...]
    resources: tuple[str, ...]
    cost_mode: str
    production: str
    expires_at: str | None
    budget: dict[str, Any]
    capability_digest: str

    def is_expired(self, now: float | datetime.datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        if isinstance(now, (int, float)):
            now_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        elif isinstance(now, datetime.datetime):
            now_dt = now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
        else:
            now_dt = datetime.datetime.now(datetime.timezone.utc)
        try:
            exp_dt = datetime.datetime.fromisoformat(self.expires_at)
            if not exp_dt.tzinfo:
                exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
            return now_dt >= exp_dt
        except Exception:
            return True

    def binding(self) -> dict[str, Any]:
        """Produce the canonical binding dictionary required for LifecycleStore."""
        return {
            "project_id": self.project_id,
            "authority_id": self.authority_id,
            "workspace": self.workspace,
            "base_sha": self.base_sha,
            "write_files": sorted(self.write_files),
            "cost_mode": self.cost_mode,
            "production": self.production,
            "criteria": [c.to_dict() for c in sorted(self.criteria, key=lambda x: x.id)],
            "resources": sorted(self.resources),
            "budget": self.budget,
            "capability_digest": self.capability_digest,
        }

    def binding_digest(self) -> str:
        return hashlib.sha256(_json(self.binding()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistrySnapshot:
    path: str
    raw_hash: str
    schema_version: int
    projects: dict[str, dict[str, Any]]
    resources: dict[str, dict[str, Any]]


def load_registry(path: Path | str) -> RegistrySnapshot:
    """Load and parse server registry configuration file."""
    raw_str = str(path)
    if not Path(raw_str).is_absolute():
        raise RegistryAuthorizationError(f"Registry path must be absolute: '{raw_str}'")
    reg_path = Path(path).resolve()
    _no_links(reg_path)
    if not reg_path.is_file():
        raise RegistryAuthorizationError(f"Registry file not found: {reg_path}")

    _check_registry_file_permissions(reg_path)

    raw_bytes = reg_path.read_bytes()
    if len(raw_bytes) > 256 * 1024:
        raise RegistryAuthorizationError("Registry file exceeds maximum size of 256KiB")

    try:
        data = _strict_json_loads(raw_bytes, max_depth=8)
    except Exception as exc:
        raise RegistryAuthorizationError(f"Malformed registry JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RegistryAuthorizationError("Registry JSON must be an object")

    if data.get("schema_version") != 1:
        raise RegistryAuthorizationError("Unsupported registry schema_version; expected 1")

    raw_projects = data.get("projects")
    if not isinstance(raw_projects, list):
        raise RegistryAuthorizationError("Registry 'projects' must be a list")

    raw_resources = data.get("resources", [])
    if not isinstance(raw_resources, list):
        raise RegistryAuthorizationError("Registry 'resources' must be a list")

    projects: dict[str, dict[str, Any]] = {}
    for p in raw_projects:
        if not isinstance(p, dict):
            raise RegistryAuthorizationError("Project entries must be objects")
        pid = p.get("project_id")
        if not isinstance(pid, str) or not pid:
            raise RegistryAuthorizationError("Project entry missing valid 'project_id'")
        _identifier(pid)
        if pid.casefold() in FORBIDDEN_PRODUCT_NAMES:
            raise RegistryAuthorizationError(f"Protected Product project '{pid}' is forbidden")
        if pid in projects:
            raise RegistryAuthorizationError(f"Duplicate project_id '{pid}' in registry")
        projects[pid] = p

    resources: dict[str, dict[str, Any]] = {}
    for r in raw_resources:
        if not isinstance(r, dict):
            raise RegistryAuthorizationError("Resource entries must be objects")
        rid = r.get("id")
        if not isinstance(rid, str) or not rid:
            raise RegistryAuthorizationError("Resource entry missing valid 'id'")
        _identifier(rid)
        if rid in resources:
            raise RegistryAuthorizationError(f"Duplicate resource id '{rid}' in registry")
        resources[rid] = r

    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    return RegistrySnapshot(
        path=str(reg_path),
        raw_hash=raw_hash,
        schema_version=1,
        projects=projects,
        resources=resources,
    )


def authorize(
    snapshot: RegistrySnapshot,
    project_id: str,
    *,
    now: float | datetime.datetime | None = None,
) -> AuthorizedProject:
    """Authorize a project against the registry snapshot."""
    _identifier(project_id)
    if project_id.casefold() in FORBIDDEN_PRODUCT_NAMES:
        raise RegistryAuthorizationError(f"Project '{project_id}' is a protected Product lane")

    proj = snapshot.projects.get(project_id)
    if proj is None:
        raise RegistryAuthorizationError(f"Unknown project_id: '{project_id}'")

    if not proj.get("enabled", False):
        raise RegistryAuthorizationError(f"Project '{project_id}' is disabled in registry")

    classification = proj.get("classification")
    if classification != "DISPOSABLE_SOURCE":
        raise RegistryAuthorizationError(
            f"Invalid project classification '{classification}'; only DISPOSABLE_SOURCE is permitted"
        )

    authority_id = proj.get("authority_id")
    if not isinstance(authority_id, str) or not authority_id:
        raise RegistryAuthorizationError("Project missing valid authority_id")
    _identifier(authority_id)

    raw_ws = proj.get("workspace")
    if not isinstance(raw_ws, str) or not raw_ws:
        raise RegistryAuthorizationError("Project missing workspace path")
    ws_path = Path(raw_ws)
    if not ws_path.is_absolute():
        raise RegistryAuthorizationError(f"Workspace path must be absolute: '{raw_ws}'")
    root = _no_links(ws_path)
    if not root.is_dir():
        raise RegistryAuthorizationError(f"Workspace directory not found: '{root}'")

    # Reject protected roots
    root_str = str(root)
    for pat in PROTECTED_PATH_PATTERNS:
        if pat.search(root_str):
            raise RegistryAuthorizationError(f"Workspace path '{root_str}' aliases a protected root")

    # Workspace must contain .git directory
    git_dir = root / ".git"
    if not git_dir.is_dir():
        raise RegistryAuthorizationError(f"Workspace '{root}' must be a Git repository with a .git directory")

    # Registry must not be inside workspace
    reg_path = Path(snapshot.path)
    if reg_path.is_relative_to(root) or root.is_relative_to(reg_path):
        raise RegistryAuthorizationError("Registry file cannot reside inside worker workspace")

    base_sha = proj.get("base_sha")
    if not isinstance(base_sha, str):
        raise RegistryAuthorizationError("Project missing base_sha")
    _digest(base_sha, 40)

    raw_write_files = proj.get("write_files")
    if not isinstance(raw_write_files, (list, tuple)) or not raw_write_files:
        raise RegistryAuthorizationError("Project must have non-empty unique write_files")
    if len(set(raw_write_files)) != len(raw_write_files):
        raise RegistryAuthorizationError("write_files must not contain duplicates")

    write_files: list[str] = []
    for f in raw_write_files:
        if not isinstance(f, str):
            raise RegistryAuthorizationError("write_files elements must be strings")
        validated = validate_scope_path(f)
        target = root / validated
        _no_links(target)
        if not target.resolve().is_relative_to(root):
            raise RegistryAuthorizationError(f"Scope escape detected in '{f}'")
        if target.exists():
            if not target.is_file():
                raise RegistryAuthorizationError(f"Write scope item '{f}' is not a regular file")
            if target.stat().st_nlink != 1:
                raise RegistryAuthorizationError(f"Write scope item '{f}' has hard links (nlink != 1)")
        write_files.append(validated)

    # Cost mode and production
    cost_mode = proj.get("cost_mode")
    if cost_mode != "ZERO_PAID":
        raise RegistryAuthorizationError(f"cost_mode must be 'ZERO_PAID', got '{cost_mode}'")

    production = proj.get("production")
    if production != "NO_GO":
        raise RegistryAuthorizationError(f"production must be 'NO_GO', got '{production}'")

    # Criteria
    raw_criteria = proj.get("criteria", [])
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise RegistryAuthorizationError("Project requires non-empty criteria list")

    criteria: list[ProjectCriterion] = []
    seen_crit_ids: set[str] = set()
    for c in raw_criteria:
        if not isinstance(c, dict):
            raise RegistryAuthorizationError("Criterion must be an object")
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            raise RegistryAuthorizationError("Criterion missing valid id")
        _identifier(cid)
        if cid in seen_crit_ids:
            raise RegistryAuthorizationError(f"Duplicate criterion id: '{cid}'")
        seen_crit_ids.add(cid)

        kind = c.get("kind", "FUNCTION_ORACLE_V1")
        module = c.get("module", "")
        symbol = c.get("symbol", "")
        oracle_ref = c.get("oracle_ref", "")
        oracle_sha256 = c.get("oracle_sha256", "")
        test_id = c.get("test_id", "")

        if oracle_sha256:
            _digest(oracle_sha256, 64)

        criteria.append(ProjectCriterion(
            id=cid,
            kind=kind,
            module=module,
            symbol=symbol,
            oracle_ref=oracle_ref,
            oracle_sha256=oracle_sha256,
            test_id=test_id,
        ))

    # Resources
    resource_ids = tuple(str(r) for r in proj.get("resources", []))

    # Expiry
    expires_at = proj.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise RegistryAuthorizationError("expires_at must be an ISO UTC string or null")

    # Budgets
    raw_budget = proj.get("budget", {})
    if not isinstance(raw_budget, dict):
        raw_budget = {}
    budget = {
        "max_network_attempts": int(raw_budget.get("max_network_attempts", 4)),
        "max_active_seconds": int(raw_budget.get("max_active_seconds", 1800)),
        "max_context_bytes": int(raw_budget.get("max_context_bytes", 32768)),
        "max_output_bytes": int(raw_budget.get("max_output_bytes", 8192)),
        "per_request_timeout": float(raw_budget.get("per_request_timeout", 120.0)),
        "test_timeout": float(raw_budget.get("test_timeout", 30.0)),
    }
    if budget["max_network_attempts"] > 8:
        raise RegistryAuthorizationError("max_network_attempts cannot exceed contract bound of 8")

    # Capability digest
    cap_dict = {
        "project_id": project_id,
        "authority_id": authority_id,
        "workspace": str(root),
        "base_sha": base_sha,
        "write_files": sorted(write_files),
        "criteria": [c.to_dict() for c in sorted(criteria, key=lambda x: x.id)],
        "resources": sorted(resource_ids),
        "budget": budget,
        "cost_mode": cost_mode,
        "production": production,
    }
    capability_digest = hashlib.sha256(_json(cap_dict).encode("utf-8")).hexdigest()

    auth_project = AuthorizedProject(
        project_id=project_id,
        authority_id=authority_id,
        classification=classification,
        workspace=str(root),
        base_sha=base_sha,
        write_files=tuple(write_files),
        criteria=tuple(criteria),
        resources=resource_ids,
        cost_mode=cost_mode,
        production=production,
        expires_at=expires_at,
        budget=budget,
        capability_digest=capability_digest,
    )

    if auth_project.is_expired(now):
        raise RegistryAuthorizationError(f"Project '{project_id}' grant has expired at {expires_at}")

    return auth_project


def revalidate(
    binding: Mapping[str, Any],
    registry_path: Path | str,
    *,
    now: float | datetime.datetime | None = None,
) -> AuthorizedProject:
    """Revalidate an existing binding against the server registry."""
    project_id = binding.get("project_id")
    if not isinstance(project_id, str):
        raise RegistryAuthorizationError("Invalid binding: missing project_id")

    snapshot = load_registry(registry_path)
    fresh = authorize(snapshot, project_id, now=now)

    # Verify exact match on immutable properties
    if fresh.authority_id != binding.get("authority_id"):
        raise RegistryAuthorizationError("Authority ID changed upon revalidation")
    if fresh.workspace != binding.get("workspace"):
        raise RegistryAuthorizationError("Workspace root changed upon revalidation")
    if fresh.base_sha != binding.get("base_sha"):
        raise RegistryAuthorizationError("Base SHA changed upon revalidation")
    if sorted(fresh.write_files) != sorted(binding.get("write_files", [])):
        raise RegistryAuthorizationError("Write files changed upon revalidation")
    if fresh.cost_mode != binding.get("cost_mode", "ZERO_PAID"):
        raise RegistryAuthorizationError("Cost mode changed upon revalidation")
    if fresh.production != binding.get("production", "NO_GO"):
        raise RegistryAuthorizationError("Production mode changed upon revalidation")

    # Check criteria drift (including oracle_sha256, module, symbol, etc.)
    fresh_crit = [c.to_dict() for c in sorted(fresh.criteria, key=lambda x: x.id)]
    if "criteria" in binding:
        bound_crit = [c if isinstance(c, dict) else c.to_dict() for c in binding["criteria"]]
        bound_crit_sorted = sorted(bound_crit, key=lambda x: x.get("id", ""))
        if fresh_crit != bound_crit_sorted:
            raise RegistryAuthorizationError("Criteria or oracle hash changed upon revalidation")

    # Check resources drift
    if "resources" in binding:
        if sorted(fresh.resources) != sorted(binding["resources"]):
            raise RegistryAuthorizationError("Authorized resources changed upon revalidation")

    # Check budget drift
    if "budget" in binding:
        if fresh.budget != binding["budget"]:
            raise RegistryAuthorizationError("Budget changed upon revalidation")

    # Check capability_digest
    if "capability_digest" in binding:
        if fresh.capability_digest != binding["capability_digest"]:
            raise RegistryAuthorizationError("Capability digest changed upon revalidation")

    return fresh
