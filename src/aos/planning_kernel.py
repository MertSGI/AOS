"""Autonomous project planning/replanning kernel for AOS.

Normal mode starts from a project descriptor + fresh canonical state + user goal.
A human-authored run plan remains only as a debug/replay/manual bounded override.

This module deliberately separates planning authority from execution authority:
provider output is advisory until schema, canonical revision, scope and standing
canonical authority validation all pass.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from jsonschema import Draft202012Validator

from aos.process_utils import run_headless
from aos.provider_registry import ProviderRouter, load_routing_policy
from aos.provider_observation import TaskClass, canonical_task_class
from aos.read_identity import (
    build_read_identity,
    build_workspace_source_generation,
    normalize_read_path,
)
from aos.providers.council import (
    COUNCIL_MIN_REAL_QUORUM,
    COUNCIL_TARGET_MEMBER_COUNT,
    DeliberationCouncilV1,
    assess_council_trigger,
)
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_file


def _ensure_repo_extensions_importable() -> None:
    import sys
    candidates = []
    aos_home = os.environ.get("AOS_HOME")
    if aos_home:
        candidates.append(Path(aos_home))
    candidates.extend([
        Path(__file__).resolve().parents[1],
        Path(__file__).resolve().parents[2],
        Path.cwd(),
    ])
    for candidate in candidates:
        if (candidate / "extensions" / "__init__.py").exists():
            value = str(candidate.resolve())
            if value not in sys.path:
                sys.path.insert(0, value)
            return


_ensure_repo_extensions_importable()

from extensions.autonomy_fabric.execution_backend import (  # noqa: E402
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.native_workers import (  # noqa: E402
    NativeFileWorker,
    NativeGitWorker,
    NativeProcessWorker,
    redact_secrets,
)


SCHEMA_VERSION = "1.0.0"
PLANNER_CANONICAL_EXCERPT_MAX_CHARS = 2500
PLANNER_COMPLETED_SIGNATURES_MAX_CHARS = 1800
PLANNER_COMPLETED_READ_MAX_FILES = 1
PLANNER_COMPLETED_READ_MAX_CHARS_PER_FILE = 500
OBJECTIVE_CANONICAL_EXCERPT_MAX_CHARS = 3000
COMPLETION_CANONICAL_EXCERPT_MAX_CHARS = 2500
DEFAULT_GOAL = "Continue this project to completion under standing authority."
DEFAULT_RED_LINES = (
    "production activation",
    "force push or history rewrite",
    "destructive irreversible operation",
    "secret ownership/storage expansion",
    "payments or commercial activation",
    "real external-provider activation when separately gated",
    "legal/compliance decision",
    "material trust/security change",
    "stable/global runtime replacement without explicit approval",
    "material scope outside standing authority",
)

_MUTATING_RUN_TYPES = {"FILE", "PROCESS", "GIT", "BUILD"}
_ALLOWED_RUN_TYPES = {"FILE", "PROCESS", "GIT", "TEST", "BUILD", "CI", "BROWSER", "MODEL_REASONING"}
_ALLOWED_GIT_ACTIONS = {
    "status", "diff", "log", "show", "rev-parse", "branch", "remote", "ls-files",
    "fetch", "add", "commit", "push", "checkout", "switch", "tag", "merge",
    "cherry-pick", "restore", "worktree",
}
_DANGEROUS_PATTERNS = (
    r"\bforce[- ]?push\b",
    r"\bpush\b[^\n]*\s--force(?:-with-lease)?\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+rebase\b",
    r"\bfilter-branch\b",
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force",
    r"\bproduction\s*=\s*(?:yes|true|go)\b",
    r"\bdeploy(?:ment)?\b[^\n]*(?:prod|production)\b",
    r"\bsecret\b[^\n]*(?:write|rotate|delete|create)\b",
    r"\bpayment\b|\bcharge\b|\bbilling activation\b",
)
_SECRET_KEYS = {
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "refresh_token", "private_key", "client_secret", "authorization", "bearer_token",
}
_SYNTHETIC_BOOKKEEPING_STEMS = (
    "next_action",
    "next-action",
    "completion_marker",
    "completion-marker",
    "status_marker",
    "status-marker",
    "progress",
    "progress_report",
    "progress-report",
)
_PLANNER_READ_CONTEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml",
}
_SENSITIVE_READ_CONTEXT_PARTS = {
    ".env", ".npmrc", ".pypirc", "credentials", "credential", "private-key", "private_key",
}


class PlanningKernelError(RuntimeError):
    pass


class HumanRequired(PlanningKernelError):
    def __init__(self, reason: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


class WaitingForReasoningProvider(PlanningKernelError):
    def __init__(
        self,
        message: str,
        task_class: str = TaskClass.STRUCTURED_PLANNING.value,
        *,
        retry_after_epoch: Optional[float] = None,
        quota_key: Optional[str] = None,
    ):
        super().__init__(message)
        self.task_class = canonical_task_class(task_class)
        self.retry_after_epoch = retry_after_epoch
        self.quota_key = quota_key


class PlannerValidationExhausted(PlanningKernelError):
    pass


class CanonicalDrift(PlanningKernelError):
    pass


class AuthorityDenied(PlanningKernelError):
    pass


@dataclasses.dataclass(frozen=True)
class AuthorityRecord:
    authority_id: str
    source_path: str
    text: str
    superseded: bool
    production_allowed: bool

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ProjectSituation:
    schema_version: str
    project_id: str
    repository: str
    control_ref: str
    control_sha: str
    repository_head: str
    execution_base_sha: Optional[str]
    current_status: Optional[str]
    current_milestone: Optional[str]
    canonical_next_action: Optional[str]
    canonical_hashes: Dict[str, str]
    canonical_excerpt: str
    working_tree_state: str
    ci_state: List[Dict[str, Any]]
    accepted_gates: List[str]
    blocked_gates: List[str]
    authority_records: Dict[str, AuthorityRecord]
    goal: str
    constraints: Tuple[str, ...]
    red_lines: Tuple[str, ...]
    completion_criteria: Tuple[str, ...]
    ambiguity_reasons: Tuple[str, ...]
    captured_at: str

    def identity(self) -> str:
        payload = {
            "project_id": self.project_id,
            "repository": self.repository,
            "control_sha": self.control_sha,
            "repository_head": self.repository_head,
            "execution_base_sha": self.execution_base_sha,
            "goal": self.goal,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def to_dict(self, include_authority_text: bool = False) -> Dict[str, Any]:
        value = dataclasses.asdict(self)
        if not include_authority_text:
            value["authority_records"] = {
                key: {
                    "authority_id": record.authority_id,
                    "source_path": record.source_path,
                    "superseded": record.superseded,
                    "production_allowed": record.production_allowed,
                }
                for key, record in self.authority_records.items()
            }
        value["situation_id"] = self.identity()
        return value


@dataclasses.dataclass(frozen=True)
class Objective:
    objective_id: str
    title: str
    description: str
    authority_id: str
    risk_class: str
    rationale: str
    scope_tags: Tuple[str, ...]
    completion_criteria: Tuple[str, ...]
    parallel_candidates: Tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Objective":
        return cls(
            objective_id=_bounded_text(value.get("objective_id"), 120, "objective_id"),
            title=_bounded_text(value.get("title"), 240, "title"),
            description=_bounded_text(value.get("description"), 3000, "description"),
            authority_id=_bounded_text(value.get("authority_id"), 160, "authority_id"),
            risk_class=_bounded_text(value.get("risk_class", "R0"), 16, "risk_class"),
            rationale=_bounded_text(value.get("rationale"), 3000, "rationale"),
            scope_tags=tuple(_string_list(value.get("scope_tags"), "scope_tags", 32, 120)),
            completion_criteria=tuple(_string_list(value.get("completion_criteria"), "completion_criteria", 32, 500)),
            parallel_candidates=tuple(_string_list(value.get("parallel_candidates", []), "parallel_candidates", 16, 240)),
        )


def _objective_task_class(objective: "Objective") -> str:
    text = " ".join((objective.title, objective.description, *objective.scope_tags)).lower()
    markers = ("ui", "frontend", "browser", "react", "tsx", "css", "visual")
    return (
        TaskClass.REPO_UI_PLANNING.value
        if any(marker in text for marker in markers)
        else TaskClass.STRUCTURED_PLANNING.value
    )


@dataclasses.dataclass(frozen=True)
class DurableBatchHistory:
    total_executed_batches: int
    successful_batches: int
    failed_batches: int
    highest_batch_number: int
    completed_task_ids: Tuple[str, ...]
    completed_task_signatures: Tuple[str, ...]
    completed_read_paths: Tuple[str, ...]
    completed_read_observations: Tuple[Dict[str, Any], ...]
    recent_completed_batches: Tuple[Dict[str, Any], ...]
    durable_batches: Tuple[Dict[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_executed_batches": self.total_executed_batches,
            "successful_batches": self.successful_batches,
            "failed_batches": self.failed_batches,
            "highest_batch_number": self.highest_batch_number,
            "completed_task_ids": list(self.completed_task_ids),
            "completed_task_signatures": list(self.completed_task_signatures),
            "completed_read_paths": list(self.completed_read_paths),
            "completed_read_observations": [dict(item) for item in self.completed_read_observations],
            "recent_completed_batches": list(self.recent_completed_batches),
            "durable_batches": list(self.durable_batches),
        }


OBJECTIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "objective_id", "title", "description", "authority_id", "risk_class",
        "rationale", "scope_tags", "completion_criteria", "parallel_candidates",
    ],
    "properties": {
        "objective_id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "authority_id": {"type": "string"},
        "risk_class": {"type": "string", "enum": ["R0", "R1", "R2", "R3"]},
        "rationale": {"type": "string"},
        "scope_tags": {"type": "array", "items": {"type": "string"}},
        "completion_criteria": {"type": "array", "items": {"type": "string"}},
        "parallel_candidates": {"type": "array", "items": {"type": "string"}},
    },
}

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "objective_id", "tasks", "parallel_safe_groups", "rollback_strategy"],
    "properties": {
        "schema_version": {"type": "string", "enum": ["1.0.0"]},
        "objective_id": {"type": "string"},
        "tasks": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "required": [
                    "node_id", "run_type", "authority_id", "risk_class", "mutating",
                    "dependencies", "scope_tags", "write_scope", "payload", "expected_artifacts",
                    "tests", "evidence_requirements", "completion_criteria",
                ],
                "properties": {
                    "node_id": {"type": "string"},
                    "run_type": {"type": "string", "enum": sorted(_ALLOWED_RUN_TYPES)},
                    "authority_id": {"type": "string"},
                    "risk_class": {"type": "string"},
                    "mutating": {"type": "boolean"},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "scope_tags": {"type": "array", "items": {"type": "string"}},
                    "write_scope": {"type": "array", "items": {"type": "string"}},
                    "payload": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": False,
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": sorted(
                                    _ALLOWED_GIT_ACTIONS
                                    | {"read_file", "write_file", "apply_patch", "observe_run"}
                                ),
                            },
                            "args": {"type": "array", "items": {"type": "string"}},
                            "cmd": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                            "env": {"type": "object", "additionalProperties": {"type": "string"}},
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "precondition_sha": {"type": "string"},
                            "patch": {"type": "string"},
                            "precondition_shas": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "allow_deletion": {"type": "boolean", "enum": [False]},
                            "sha": {"type": "string"},
                            "repo": {"type": "string"},
                            "url": {"type": "string"},
                            "run_id": {"type": "string"},
                            "prompt": {"type": "string"},
                            "schema": {"type": "object"},
                            "risk_class": {"type": "string"},
                            "ignore_credentials": {"type": "boolean"},
                        },
                        "anyOf": [
                            {"required": ["action"]},
                            {"required": ["cmd"]},
                            {"required": ["sha"]},
                            {"required": ["url"]},
                            {"required": ["prompt", "schema"]},
                        ],
                    },
                    "expected_artifacts": {"type": "array", "items": {"type": "string"}},
                    "tests": {"type": "array", "items": {"type": "string"}},
                    "evidence_requirements": {"type": "array", "items": {"type": "string"}},
                    "completion_criteria": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "parallel_safe_groups": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "rollback_strategy": {"type": "string"},
    },
}

COMPLETION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["disposition", "rationale", "satisfied_criteria", "unsatisfied_criteria"],
    "properties": {
        "disposition": {"type": "string", "enum": ["PROJECT_COMPLETE", "REPLAN", "HUMAN_REQUIRED"]},
        "rationale": {"type": "string"},
        "satisfied_criteria": {"type": "array", "items": {"type": "string"}},
        "unsatisfied_criteria": {"type": "array", "items": {"type": "string"}},
    },
}


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningKernelError(f"{label} must be non-empty text")
    return value.strip()[:limit]


def _string_list(value: Any, label: str, max_items: int, max_len: int) -> List[str]:
    if not isinstance(value, list):
        raise PlanningKernelError(f"{label} must be an array")
    result: List[str] = []
    for item in value[:max_items]:
        if not isinstance(item, str) or not item.strip():
            raise PlanningKernelError(f"{label} contains non-text item")
        result.append(item.strip()[:max_len])
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            # Windows scanners and readers can briefly retain a handle to the
            # destination. Preserve atomic replacement while tolerating that
            # bounded, transient sharing violation.
            time.sleep(min(0.05 * (2 ** attempt), 0.5))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _run_readonly(cmd: Sequence[str], cwd: Path, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        proc = run_headless(
            list(cmd), cwd=str(cwd), timeout=timeout
        )
        return proc.returncode, redact_secrets(proc.stdout or "")[:20000], redact_secrets(proc.stderr or "")[:10000]
    except FileNotFoundError as exc:
        # Optional read-only discovery helpers (for example `gh`) must never
        # terminate autonomous project synthesis. Callers already handle a
        # non-zero return code by recording unavailable/empty discovery state.
        # Mutating workers have their own strict executable/authority gates.
        return 127, "", redact_secrets(str(exc))[:10000]
    except OSError as exc:
        # Preserve fail-closed mutation semantics while making read-only
        # environmental discovery degradable instead of process-fatal.
        return 126, "", redact_secrets(str(exc))[:10000]


def _repo_head(workspace: Path) -> str:
    code, out, _ = _run_readonly(["git", "rev-parse", "HEAD"], workspace)
    return out.strip() if code == 0 else "UNAVAILABLE"


def _working_tree_state(workspace: Path) -> str:
    code, out, err = _run_readonly(["git", "status", "--porcelain=v1", "--untracked-files=normal"], workspace)
    if code != 0:
        return f"UNAVAILABLE:{err[:300]}"
    return "CLEAN" if not out.strip() else out[:12000]


def _ci_state(repository: str, repository_head: str, workspace: Path) -> List[Dict[str, Any]]:
    if repository_head == "UNAVAILABLE":
        return []
    code, out, _ = _run_readonly(
        [
            "gh", "run", "list", "--repo", repository, "--commit", repository_head,
            "--limit", "10", "--json", "databaseId,status,conclusion,headSha,workflowName,event",
        ],
        workspace,
        timeout=30,
    )
    if code != 0:
        return []
    try:
        value = json.loads(out)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _canonical_excerpt(contents: Mapping[str, Any], max_chars: int = 90000) -> str:
    chunks: List[str] = []
    remaining = max_chars

    def priority_rank(path: str) -> Tuple[int, str]:
        """Keep current state/roadmap ahead of large historical journals."""
        upper = path.upper()
        for rank, token in enumerate(("STATE", "ROADMAP", "RESUME", "CURRENT", "DECISION", "EVIDENCE")):
            if token in upper:
                return rank, path
        return 6, path

    priority = sorted(
        contents,
        key=priority_rank,
    )
    for path in priority:
        raw = contents[path]
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            text = raw
        else:
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        text = redact_secrets(text)
        piece = f"\n--- {path} ---\n{text}\n"
        if len(piece) > remaining:
            piece = piece[:remaining]
        chunks.append(piece)
        remaining -= len(piece)
        if remaining <= 0:
            break
    return "".join(chunks)


def _extract_gates(text: str) -> Tuple[List[str], List[str]]:
    accepted: List[str] = []
    blocked: List[str] = []
    for line in text.splitlines():
        u = line.upper()
        if any(token in u for token in ("=PASS", "=ACCEPTED", "=STABLE_ACCEPTED", "CONCLUSION=SUCCESS")):
            accepted.append(line.strip()[:300])
        if any(token in u for token in ("=HOLD", "=BLOCK", "=NO_GO", "HUMAN_REQUIRED", "NOT_YET_PROVEN")):
            blocked.append(line.strip()[:300])
    return accepted[-80:], blocked[-80:]


def _extract_authorities(contents: Mapping[str, Any]) -> Dict[str, AuthorityRecord]:
    result: Dict[str, AuthorityRecord] = {}
    authority_re = re.compile(r"\b(?:DECISION|AUTHORITY)[-_][A-Z0-9._-]+\b|\bDECISION-\d+\b", re.I)
    for path, raw in contents.items():
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            text = raw
        else:
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2)
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            ids = authority_re.findall(line)
            if not ids:
                continue
            window = "\n".join(lines[max(0, idx - 12): min(len(lines), idx + 80)])
            upper = window.upper()
            superseded = any(
                marker in upper
                for marker in (
                    "STATUS=SUPERSEDED", "SUPERSEDED=YES", "AUTHORITY=CONSUMED", "AUTHORITY_STATE=REVOKED",
                )
            )
            production_allowed = any(
                marker in upper for marker in ("PRODUCTION=GO", "PRODUCTION_ALLOWED=YES", "PRODUCTION_MUTATION=AUTHORIZED")
            )
            def _source_priority(src: str) -> int:
                u = src.lower()
                if "decision" in u:
                    return 3
                if "state" in u or "roadmap" in u:
                    return 2
                return 1

            for authority_id in ids:
                key = authority_id.upper()
                current = result.get(key)
                candidate = AuthorityRecord(
                    authority_id=authority_id,
                    source_path=str(path),
                    text=window[:14000],
                    superseded=superseded,
                    production_allowed=production_allowed,
                )
                if current is None:
                    result[key] = candidate
                else:
                    cand_prio = _source_priority(candidate.source_path)
                    curr_prio = _source_priority(current.source_path)
                    if cand_prio > curr_prio or (cand_prio == curr_prio and len(candidate.text) > len(current.text)):
                        result[key] = candidate
    return result


def _completion_criteria_from_descriptor(descriptor: Mapping[str, Any], goal: str) -> Tuple[str, ...]:
    for key in ("completion_criteria", "acceptance_criteria", "project_completion_criteria"):
        value = descriptor.get(key)
        if isinstance(value, list) and value:
            return tuple(str(item)[:500] for item in value if str(item).strip())
    return (goal[:500], "Canonical roadmap has no unfinished authorized work and all applicable acceptance evidence is satisfied.")


def synthesize_project_situation(
    descriptor_path: Path,
    workspace: Path,
    goal: str = DEFAULT_GOAL,
    constraints: Sequence[str] = (),
    red_lines: Sequence[str] = DEFAULT_RED_LINES,
    *,
    adapter_override: Optional[Any] = None,
) -> ProjectSituation:
    validation, _ = validate_file("project_descriptor", str(descriptor_path))
    if not validation.is_valid:
        raise PlanningKernelError(f"Invalid project descriptor: {[str(e) for e in validation.errors]}")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    adapter = adapter_override or ProjectSourceAdapter(descriptor["repository"], descriptor["control_ref"])
    control_sha = adapter.resolve_ref_to_sha()
    contents, hashes = adapter.fetch_canonical_context(control_sha, descriptor["control"])
    snapshot = adapter.build_normalized_snapshot(
        descriptor["project_id"], control_sha, contents, hashes, descriptor.get("projection")
    )
    ambiguity = tuple(str(item) for item in snapshot.get("ambiguity_reasons", []) if str(item).strip())
    if snapshot.get("has_ambiguity") and not ambiguity:
        ambiguity = ("Canonical source reports ambiguity",)

    execution_base_sha = snapshot.get("next_action_execution_base_sha") or snapshot.get("execution_base_sha")
    if execution_base_sha:
        adapter.resolve_exact_revision(execution_base_sha)

    excerpt = _canonical_excerpt(contents)
    accepted, blocked = _extract_gates(excerpt)
    authorities = _extract_authorities(contents)
    return ProjectSituation(
        schema_version=SCHEMA_VERSION,
        project_id=descriptor["project_id"],
        repository=descriptor["repository"],
        control_ref=descriptor["control_ref"],
        control_sha=control_sha,
        repository_head=_repo_head(workspace),
        execution_base_sha=execution_base_sha,
        current_status=snapshot.get("current_status"),
        current_milestone=snapshot.get("current_milestone"),
        canonical_next_action=snapshot.get("canonical_next_action"),
        canonical_hashes={str(k): str(v) for k, v in hashes.items()},
        canonical_excerpt=excerpt,
        working_tree_state=_working_tree_state(workspace),
        ci_state=_ci_state(descriptor["repository"], _repo_head(workspace), workspace),
        accepted_gates=accepted,
        blocked_gates=blocked,
        authority_records=authorities,
        goal=goal.strip() or DEFAULT_GOAL,
        constraints=tuple(str(item)[:500] for item in constraints),
        red_lines=tuple(str(item)[:500] for item in red_lines),
        completion_criteria=_completion_criteria_from_descriptor(descriptor, goal),
        ambiguity_reasons=ambiguity,
        captured_at=_utc_now(),
    )


def _worker_contract_summary() -> str:
    """Return the planner-visible worker payload contract without source bloat."""
    return json.dumps(
        {
            "NativeFileWorker": {
                "run_type": "FILE",
                "actions": {
                    "read_file": {"path": "workspace-relative string"},
                    "write_file": {
                        "path": "workspace-relative string",
                        "content": "string",
                        "precondition_sha": "optional sha256",
                    },
                    "apply_patch": {
                        "patch": "unified diff string",
                        "precondition_shas": "optional path-to-sha object",
                        "allow_deletion": False,
                    },
                },
                "safety": "workspace confinement and declared write_scope are enforced",
            },
            "NativeProcessWorker": {
                "run_type": "PROCESS",
                "payload": {"cmd": "non-empty argv array", "env": "non-secret string map"},
                "allowed_binaries": sorted(NativeProcessWorker.ALLOWED_BINARIES),
                "safety": "non-mutating verification only; shell=False; Python -c and -m are forbidden; invoke an available binary directly or pass Python an existing workspace-relative script path; bounded timeout; clean environment",
            },
            "NativeGitWorker": {
                "run_type": "GIT",
                "payload": {"action": {"enum": sorted(_ALLOWED_GIT_ACTIONS)}, "args": "argv array"},
                "prohibited_subcommands": sorted(NativeGitWorker.PROHIBITED_SUBCOMMANDS),
                "safety": "force push and prohibited subcommands are denied",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_workspace_file_manifest(
    workspace: Optional[Path],
    objective: Objective,
    *,
    max_chars: int = 800,
) -> Dict[str, Any]:
    """Return a compact path-only view of the tracked workspace for the planner.

    This is advisory context, not a security boundary. Exact confinement and
    existence/dependency checks still run locally after provider output.
    """
    if workspace is None:
        return {"status": "UNAVAILABLE", "reason": "workspace_not_bound"}
    try:
        completed = run_headless(
            ["git", "ls-files"],
            cwd=workspace.resolve(),
            check=True,
            timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "UNAVAILABLE", "reason": "tracked_file_inventory_failed"}

    paths = sorted({line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()})
    if not paths:
        return {"status": "AVAILABLE", "tracked_count": 0, "representative_existing_paths": []}

    objective_text = " ".join(
        [objective.title, objective.description, *objective.scope_tags, *objective.completion_criteria]
    ).lower()
    stop_words = {
        "continue", "within", "under", "standing", "authority", "program", "phase",
        "development", "completion", "current", "project", "bounded", "non", "production",
    }
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", objective_text)
        if len(token) >= 4 and token not in stop_words
    }

    def priority(path: str) -> tuple[int, int, str]:
        lowered = path.lower()
        root_rank = 0 if "/" not in path else 1
        match_count = sum(1 for token in tokens if token in lowered)
        return (root_rank, -match_count, lowered)

    ordered = sorted(paths, key=priority)
    manifest: Dict[str, Any] = {
        "status": "AVAILABLE",
        "tracked_count": len(paths),
        "path_set_sha256": hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest(),
        "representative_existing_paths": [],
    }
    for path in ordered:
        manifest["representative_existing_paths"].append(path)
        if len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))) > max_chars:
            manifest["representative_existing_paths"].pop()
            break
    return manifest


def _bounded_completed_read_context(
    runtime_dir: Path,
    workspace: Path,
    completed_batches: Sequence[Mapping[str, Any]],
    *,
    workspace_source_generation: Optional[str] = None,
    max_files: int = PLANNER_COMPLETED_READ_MAX_FILES,
    max_chars_per_file: int = PLANNER_COMPLETED_READ_MAX_CHARS_PER_FILE,
) -> Dict[str, Any]:
    """Reuse only worker-observed reads whose content and generation still match."""
    workspace_root = workspace.resolve()
    selected: List[Tuple[int, Dict[str, Any], Path]] = []
    seen_paths: set[str] = set()
    observed_paths: set[str] = set()
    legacy_unbound_paths: set[str] = set()

    for completed in reversed(list(completed_batches)):
        if not isinstance(completed, Mapping):
            continue
        try:
            batch_number = int(completed.get("batch_number"))
        except (TypeError, ValueError):
            continue
        receipt = completed.get("receipt", {})
        if not isinstance(receipt, Mapping):
            continue
        observations = receipt.get("completed_read_observations", [])
        if isinstance(observations, list):
            for raw_observation in reversed(observations):
                if not isinstance(raw_observation, Mapping):
                    continue
                try:
                    observation = dict(raw_observation)
                    normalized_path = normalize_read_path(str(observation.get("normalized_path", "")))
                    observed_paths.add(normalized_path)
                    if normalized_path in seen_paths:
                        continue
                    generation = str(observation.get("workspace_source_generation", "")).lower()
                    content_hash = str(observation.get("content_sha256", "")).lower()
                    identity = build_read_identity(
                        normalized_path=normalized_path,
                        content_sha256=content_hash,
                        workspace_source_generation=generation,
                    )
                    if observation.get("read_identity") not in (None, identity):
                        continue
                    if workspace_source_generation is not None and generation != workspace_source_generation:
                        continue
                except (TypeError, ValueError):
                    continue
                parts = {part.casefold() for part in Path(normalized_path).parts}
                if parts & _SENSITIVE_READ_CONTEXT_PARTS:
                    continue
                target = _resolve_normalized_workspace_path(workspace_root, normalized_path)
                if target is None:
                    continue
                if target.suffix.casefold() not in _PLANNER_READ_CONTEXT_SUFFIXES or not target.is_file():
                    continue
                try:
                    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                except OSError:
                    continue
                if current_hash != content_hash:
                    continue
                observation["read_identity"] = identity
                observation["normalized_path"] = normalized_path
                selected.append((batch_number, observation, target))
                seen_paths.add(normalized_path)
                if len(selected) >= max_files:
                    break
        completed_ids = {
            str(task_id) for task_id in receipt.get("completed_task_ids", [])
            if str(task_id).strip()
        }
        plan = _read_json(runtime_dir / "batches" / f"batch-{batch_number:04d}" / "generated-run-plan.json")
        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, Mapping) or str(task.get("node_id")) not in completed_ids:
                continue
            payload = task.get("payload", {})
            if task.get("run_type") != "FILE" or not isinstance(payload, Mapping):
                continue
            if payload.get("action") != "read_file":
                continue
            raw_path = str(payload.get("path", "")).strip()
            normalized_path = raw_path.replace("\\", "/")
            try:
                path_key = normalize_read_path(normalized_path)
            except ValueError:
                continue
            if path_key not in observed_paths:
                legacy_unbound_paths.add(path_key)
        if len(selected) >= max_files:
            break

    files: List[Dict[str, Any]] = []
    for batch_number, observation, _target in selected:
        excerpt = redact_secrets(str(observation.get("redacted_excerpt", "")))
        files.append({
            "batch_number": batch_number,
            "path": observation["normalized_path"],
            "read_identity": observation["read_identity"],
            "content_chars": int(observation.get("character_count", 0) or 0),
            "content_sha256": observation["content_sha256"],
            "workspace_source_generation": observation["workspace_source_generation"],
            "redacted_excerpt": _bounded_prompt_excerpt(excerpt, max_chars=max_chars_per_file),
        })
    return {
        "status": "AVAILABLE" if files else "NONE",
        "fresh_read": False,
        "hash_bound": True,
        "files": files,
        "completed_read_paths": [entry["path"] for entry in files],
        "completed_read_identities": [entry["read_identity"] for entry in files],
        "legacy_unbound_paths": sorted(legacy_unbound_paths),
    }


def _resolve_normalized_workspace_path(workspace_root: Path, normalized_path: str) -> Optional[Path]:
    """Resolve a case-folded read identity path without escaping the workspace.

    Read identities are intentionally case-insensitive and therefore do not
    preserve the spelling of a path such as ``ROADMAP.md``. Walk each component
    and require exactly one case-insensitive match so those identities remain
    portable to case-sensitive filesystems without accepting ambiguous paths.
    """
    try:
        current = workspace_root.resolve()
        for part in PurePosixPath(normalized_path).parts:
            matches = [child for child in current.iterdir() if child.name.casefold() == part.casefold()]
            if len(matches) != 1:
                return None
            current = matches[0]
        target = current.resolve()
    except OSError:
        return None
    if target != workspace_root and workspace_root not in target.parents:
        return None
    return target


def _task_signature(task: Mapping[str, Any]) -> str:
    payload = task.get("payload", {})
    normalized_payload = payload if isinstance(payload, Mapping) else {}
    return f"{str(task.get('run_type', '')).upper()}:" + json.dumps(
        dict(normalized_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _completed_task_signatures(
    runtime_dir: Path,
    completed_batches: Sequence[Mapping[str, Any]],
) -> List[str]:
    signatures: set[str] = set()
    for completed in completed_batches:
        if not isinstance(completed, Mapping):
            continue
        try:
            batch_number = int(completed.get("batch_number"))
        except (TypeError, ValueError):
            continue
        receipt = completed.get("receipt", {})
        if not isinstance(receipt, Mapping):
            continue
        completed_ids = {
            str(task_id) for task_id in receipt.get("completed_task_ids", [])
            if str(task_id).strip()
        }
        plan = _read_json(runtime_dir / "batches" / f"batch-{batch_number:04d}" / "generated-run-plan.json")
        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if isinstance(task, Mapping) and str(task.get("node_id")) in completed_ids:
                payload = task.get("payload", {})
                if (
                    task.get("run_type") == "FILE"
                    and isinstance(payload, Mapping)
                    and payload.get("action") == "read_file"
                ):
                    continue
                signatures.add(_task_signature(task))
    return sorted(signatures)


def _current_read_identity(
    workspace: Optional[Path],
    raw_path: str,
    workspace_source_generation: Optional[str],
) -> Optional[str]:
    if workspace is None or workspace_source_generation is None:
        return None
    try:
        normalized_path = normalize_read_path(raw_path)
        workspace_root = workspace.resolve()
        target = (workspace_root / raw_path).resolve()
        if target != workspace_root and workspace_root not in target.parents:
            return None
        if not target.is_file():
            return None
        content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        return build_read_identity(
            normalized_path=normalized_path,
            content_sha256=content_hash,
            workspace_source_generation=workspace_source_generation,
        )
    except (OSError, TypeError, ValueError):
        return None


def reconstruct_batch_history(
    runtime_dir: Path,
    workspace: Optional[Path] = None,
) -> DurableBatchHistory:
    """Deterministically reconstruct durable batch execution history from disk artifacts.

    Scans runtime_dir / 'batches' / 'batch-*' for host-receipt.json and generated-run-plan.json.
    Computes executed, successful, and failed batches, highest executed batch index,
    and cumulative completed task IDs, task signatures, and file read paths.
    """
    batches_dir = runtime_dir / "batches"
    executed_batches: List[Dict[str, Any]] = []
    completed_task_ids: set[str] = set()
    completed_signatures: set[str] = set()
    completed_read_paths: set[str] = set()
    completed_read_observations: Dict[str, Dict[str, Any]] = {}
    successful_count = 0
    failed_count = 0
    highest_batch_number = -1

    if batches_dir.is_dir():
        batch_dirs = []
        for p in batches_dir.iterdir():
            if p.is_dir():
                m = re.match(r"^batch-(\d+)$", p.name)
                if m:
                    batch_dirs.append((int(m.group(1)), p))
        batch_dirs.sort(key=lambda x: x[0])

        workspace_root = workspace.resolve() if workspace is not None else None

        for batch_num, b_dir in batch_dirs:
            receipt_path = b_dir / "host-receipt.json"
            if not receipt_path.is_file():
                continue
            receipt = _read_json(receipt_path)
            if not isinstance(receipt, dict):
                continue
            # Must be a valid receipt with progress or completed_task_ids or failed_task_ids
            if "progress" not in receipt and "completed_task_ids" not in receipt and "failed_task_ids" not in receipt:
                continue

            highest_batch_number = max(highest_batch_number, batch_num)
            raw_prog = receipt.get("progress", 0.0)
            try:
                prog = float(raw_prog) if raw_prog is not None else 0.0
            except (ValueError, TypeError):
                prog = 0.0
            failed_ids = receipt.get("failed_task_ids", [])
            is_success = bool(not failed_ids and prog >= 100.0)
            if is_success:
                successful_count += 1
            else:
                failed_count += 1

            batch_completed_ids = {
                str(t_id).strip()
                for t_id in receipt.get("completed_task_ids", [])
                if str(t_id).strip()
            }
            completed_task_ids.update(batch_completed_ids)

            raw_observations = receipt.get("completed_read_observations", [])
            if isinstance(raw_observations, list):
                for raw_observation in raw_observations:
                    if not isinstance(raw_observation, Mapping):
                        continue
                    try:
                        observation = dict(raw_observation)
                        normalized_path = normalize_read_path(str(observation.get("normalized_path", "")))
                        content_hash = str(observation.get("content_sha256", "")).lower()
                        generation = str(observation.get("workspace_source_generation", "")).lower()
                        identity = build_read_identity(
                            normalized_path=normalized_path,
                            content_sha256=content_hash,
                            workspace_source_generation=generation,
                        )
                    except (TypeError, ValueError):
                        continue
                    observation["normalized_path"] = normalized_path
                    observation["content_sha256"] = content_hash
                    observation["workspace_source_generation"] = generation
                    observation["read_identity"] = identity
                    completed_read_observations[identity] = observation
                    completed_read_paths.add(normalized_path)

            plan_path = b_dir / "generated-run-plan.json"
            objective_id = receipt.get("objective_id") or ""
            plan = _read_json(plan_path) if plan_path.is_file() else {}
            if isinstance(plan, dict):
                if not objective_id:
                    objective_id = str(plan.get("objective_id") or "")
                tasks = plan.get("tasks", [])
                if isinstance(tasks, list):
                    for task in tasks:
                        if not isinstance(task, Mapping):
                            continue
                        node_id = str(task.get("node_id") or "").strip()
                        if node_id in batch_completed_ids:
                            payload = task.get("payload", {})
                            is_read = (
                                task.get("run_type") == "FILE"
                                and isinstance(payload, Mapping)
                                and payload.get("action") == "read_file"
                            )
                            if not is_read:
                                completed_signatures.add(_task_signature(task))
                            if is_read:
                                raw_path = str(payload.get("path", "")).strip()
                                normalized_path = raw_path.replace("\\", "/")
                                path_key = normalized_path.casefold()
                                parts = {part.casefold() for part in Path(normalized_path).parts}
                                if raw_path and not (parts & _SENSITIVE_READ_CONTEXT_PARTS):
                                    if workspace_root is not None:
                                        target = (workspace_root / raw_path).resolve()
                                        if (
                                            (target == workspace_root or workspace_root in target.parents)
                                            and target.suffix.casefold() in _PLANNER_READ_CONTEXT_SUFFIXES
                                            and target.is_file()
                                        ):
                                            completed_read_paths.add(normalized_path)
                                    else:
                                        completed_read_paths.add(normalized_path)

            executed_batches.append({
                "batch_number": batch_num,
                "objective_id": objective_id,
                "canonical_source_sha": receipt.get("canonical_source_sha", ""),
                "receipt": dict(receipt),
                "resumed": False,
            })

    total_executed = len(executed_batches)
    recent_batches = executed_batches[-30:]

    return DurableBatchHistory(
        total_executed_batches=total_executed,
        successful_batches=successful_count,
        failed_batches=failed_count,
        highest_batch_number=highest_batch_number,
        completed_task_ids=tuple(sorted(completed_task_ids)),
        completed_task_signatures=tuple(sorted(completed_signatures)),
        completed_read_paths=tuple(sorted(completed_read_paths)),
        completed_read_observations=tuple(
            completed_read_observations[key]
            for key in sorted(completed_read_observations)
        ),
        recent_completed_batches=tuple(recent_batches),
        durable_batches=tuple(executed_batches),
    )


def _bounded_task_signatures_for_prompt(
    signatures: Sequence[str],
    *,
    max_chars: int = PLANNER_COMPLETED_SIGNATURES_MAX_CHARS,
    max_signature_chars: int = 240,
) -> Dict[str, Any]:
    normalized = sorted({str(item) for item in signatures if str(item).strip()})
    digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    result: Dict[str, Any] = {
        "total_count": len(normalized),
        "signature_set_sha256": digest,
        "representative_signatures": [],
    }
    for signature in normalized:
        rendered = signature
        if len(rendered) > max_signature_chars:
            item_digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            suffix = f"...<sha256={item_digest}>"
            rendered = rendered[:max_signature_chars - len(suffix)] + suffix
        result["representative_signatures"].append(rendered)
        if len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) > max_chars:
            result["representative_signatures"].pop()
            break
    return result


def _is_generic_readiness_task(task: Mapping[str, Any]) -> bool:
    run_type = str(task.get("run_type", "")).upper()
    payload = task.get("payload", {})
    if not isinstance(payload, Mapping):
        return False
    if run_type == "GIT":
        return str(payload.get("action", "")).lower() in {"status", "rev-parse", "branch", "remote"}
    if run_type in {"PROCESS", "TEST", "BUILD"}:
        cmd = payload.get("cmd", [])
        return (
            isinstance(cmd, list)
            and len(cmd) <= 2
            and any(str(arg).lower() in {"--version", "-v", "version"} for arg in cmd)
        )
    return False


def _available_process_binaries() -> List[str]:
    """Return the allowlisted process binaries executable in this runtime environment."""
    return sorted(binary for binary in NativeProcessWorker.ALLOWED_BINARIES if shutil.which(binary))


def _bounded_prompt_excerpt(
    excerpt: str,
    max_chars: int = PLANNER_CANONICAL_EXCERPT_MAX_CHARS,
) -> str:
    """Project a large canonical excerpt into a deterministic bounded prompt.

    The full canonical artifact remains durable and all authority checks continue
    to use the unabridged ProjectSituation. The model receives both ends plus a
    hash/length binding so omitted middle text cannot be mistaken for absence.
    """
    if len(excerpt) <= max_chars:
        return excerpt
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    marker = (
        f"\n...[BOUNDED_CANONICAL_EXCERPT original_chars={len(excerpt)} "
        f"sha256={digest}]...\n"
    )
    available = max(0, max_chars - len(marker))
    head_len = available // 2
    tail_len = available - head_len
    return excerpt[:head_len] + marker + excerpt[-tail_len:]


def _hydrate_credentials() -> None:
    try:
        from aos.secure_store import hydrate_environment
        hydrate_environment(overwrite=True)
    except Exception:
        return


def build_planning_resource_requirements(
    task_class: str,
    prompt: str,
    schema: Dict[str, Any],
    situation: Optional[ProjectSituation] = None,
) -> Dict[str, Any]:
    """Derive truthful planning capability envelope from task class, prompt, and schema complexity."""
    c_task_class = canonical_task_class(task_class)
    estimated_tokens = max(100, (len(prompt) + len(json.dumps(schema or {}))) // 4)

    # Architectural / long context / high complexity markers
    prompt_lower = prompt.lower()
    is_high_complexity = (
        c_task_class in (TaskClass.REPO_UI_PLANNING.value, TaskClass.LARGE_CONTEXT.value, TaskClass.AGENTIC_EXECUTION.value)
        or "architecture" in prompt_lower
        or "refactor" in prompt_lower
        or (c_task_class != TaskClass.STRUCTURED_PLANNING.value and "design intelligence" in prompt_lower)
        or (c_task_class != TaskClass.STRUCTURED_PLANNING.value and estimated_tokens > 4000)
    )
    is_small_reasoning = (
        c_task_class == TaskClass.SMALL_REASONING.value
        or (estimated_tokens <= 1500 and not is_high_complexity)
    )

    if is_small_reasoning:
        return {
            "task_class": c_task_class,
            "context_tokens": estimated_tokens,
            "minimum_quality": 1,
            "complexity_class": "LOW",
            "local_qwen_allowed": True,
            "agentic_planning_allowed": True,
            "maximum_latency_ms": 10000,
            "scarcity_policy": "ALLOW_SCARCE",
        }
    elif is_high_complexity:
        return {
            "task_class": c_task_class,
            "context_tokens": estimated_tokens,
            "minimum_quality": 3,
            "complexity_class": "HIGH",
            "local_qwen_allowed": False,
            "agentic_planning_allowed": True,
            "maximum_latency_ms": 30000,
            "scarcity_policy": "ALLOW_SCARCE",
        }
    else:  # GENERAL / MEDIUM
        return {
            "task_class": c_task_class,
            "context_tokens": estimated_tokens,
            "minimum_quality": 2,
            "complexity_class": "MEDIUM",
            "local_qwen_allowed": estimated_tokens <= 3000,
            "agentic_planning_allowed": True,
            "maximum_latency_ms": 15000,
            "scarcity_policy": "ALLOW_SCARCE",
        }


def _reason(
    situation: ProjectSituation,
    routing_policy_path: Path,
    runtime_dir: Path,
    task_id: str,
    prompt: str,
    schema: Dict[str, Any],
    authority_id: str,
    *,
    backend_override: Optional[Any] = None,
    task_class: str = TaskClass.STRUCTURED_PLANNING.value,
) -> Dict[str, Any]:
    _hydrate_credentials()
    if backend_override is None:
        from aos.autonomous_host import build_execution_router
        router = build_execution_router(routing_policy_path, runtime_dir)
        backend = router
    else:
        backend = backend_override
    resource_reqs = build_planning_resource_requirements(task_class, prompt, schema, situation)
    request_identity = hashlib.sha256(json.dumps({
        "project_id": situation.project_id,
        "control_sha": situation.control_sha,
        "execution_base_sha": situation.execution_base_sha,
        "task_id": task_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "schema": schema,
        "task_class": canonical_task_class(task_class),
        "resource_requirements": resource_reqs,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    planning_workspace = str(situation.repository) if situation and situation.repository and Path(situation.repository).is_dir() else str(runtime_dir)
    request = ExecutionRequest(
        task_id=task_id,
        project_id=situation.project_id,
        workspace=planning_workspace,
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id=authority_id,
        request_id=f"reason-{request_identity}",
        payload={
            "prompt": prompt,
            "schema": schema,
            "risk_class": "R0",
            "task_class": canonical_task_class(task_class),
            "resource_requirements": resource_reqs,
            "source_sha": situation.control_sha,
        },
    )
    if hasattr(backend, "execute_with_failover"):
        result = backend.execute_with_failover(request)
    else:
        result = backend.execute(request)
    if result.status != "SUCCESS":
        failure = str(result.evidence_payload.get("failure_class", result.status))
        if result.status in ("DEGRADED", "WAITING_FOR_REASONING_PROVIDER") or "UNAVAILABLE" in failure.upper():
            decisions = result.evidence_payload.get("quota_decisions", [])
            blocking = [
                item for item in decisions
                if isinstance(item, Mapping) and item.get("eligible") is False
            ] if isinstance(decisions, list) else []
            retry = result.evidence_payload.get("quota_retry_after_epoch")
            quota_key = blocking[0].get("key") if blocking else None
            raise WaitingForReasoningProvider(
                failure,
                task_class=task_class,
                retry_after_epoch=(float(retry) if retry is not None else None),
                quota_key=(str(quota_key) if quota_key else None),
            )
        raise PlanningKernelError(f"Reasoning failed: {failure}")
    proposal = result.evidence_payload.get("proposal")
    if not isinstance(proposal, dict):
        proposal = getattr(result, "transient_structured_output", None)
    if not isinstance(proposal, dict):
        raise PlanningKernelError("Reasoning backend returned no structured proposal")
    return proposal


def _shadow_deliberate(
    runtime_dir: Path,
    situation: ProjectSituation,
    decision_type: str,
    prompt: str,
    primary_proposal: Mapping[str, Any],
    *,
    command_id: Optional[str] = None,
    routing_policy_path: Optional[Path] = None,
    schema: Optional[Dict[str, Any]] = None,
    alternate_proposals: Sequence[Tuple[str, Mapping[str, Any]]] = (),
) -> None:
    """Non-blocking shadow deliberation integration for real planning decisions.

    INVARIANTS:
    - Never blocks or delays primary execution
    - Never mutates the primary decision or execution plan
    - Only consumes spare capacity (skips/defers if provider constrained)
    - Target 3 council members, min real quorum >= 2
    - Records durable sanitized decision ledger in runtime_dir / 'deliberation'
    - Durable metrics aggregate across runtime restarts
    """
    try:
        ledger_dir = runtime_dir / "deliberation"
        council = DeliberationCouncilV1(
            mode="SHADOW_ONLY",
            min_quorum=COUNCIL_MIN_REAL_QUORUM,
            target_members=COUNCIL_TARGET_MEMBER_COUNT,
            ledger_dir=ledger_dir,
        )
        assessment = assess_council_trigger(decision_type, prompt, primary_proposal)
        if not assessment.council_required:
            return

        # Section 11 Admission Rules:
        # If HEALTHY_REASONING_PROVIDER_COUNT <= 1: Council model calls = 0
        # If any primary product lane is WAITING_FOR_REASONING_PROVIDER: Council model calls = 0
        # If provider quota/capacity is degraded materially: Council model calls = 0
        spare_capacity = True

        # Global aggregate primary-lane capacity view across known store roots
        store_roots = []
        commands_root = runtime_dir.parent.parent
        if (commands_root / "commands").is_dir():
            store_roots.append(commands_root / "commands")
        elif commands_root.name == "commands" and commands_root.is_dir():
            store_roots.append(commands_root)
        local_app_cmds = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "runtime-v1" / "state" / "commands"
        if local_app_cmds.is_dir() and local_app_cmds not in store_roots:
            store_roots.append(local_app_cmds)

        # 1. Global check: If ANY active primary product lane is WAITING_FOR_REASONING_PROVIDER
        for cmd_root in store_roots:
            try:
                for state_file in cmd_root.glob("*/state.json"):
                    s_data = _read_json(state_file)
                    st = str(s_data.get("state", "")).upper()
                    disp = str(s_data.get("disposition", "")).upper()
                    fail_cls = str(s_data.get("failure_class", "")).upper()
                    if (
                        st == "WAITING_FOR_REASONING_PROVIDER"
                        or disp == "WAITING_FOR_REASONING_PROVIDER"
                        or fail_cls == "WAITING_FOR_REASONING_PROVIDER"
                    ):
                        spare_capacity = False
                        break
                if not spare_capacity:
                    break
                for chk_file in cmd_root.glob("*/project-runtime/planning-kernel-checkpoint.json"):
                    c_data = _read_json(chk_file)
                    ph = str(c_data.get("phase", "")).upper()
                    if "WAITING_FOR_REASONING_PROVIDER" in ph or ph == "WAITING_FOR_REASONING_PROVIDER":
                        spare_capacity = False
                        break
            except Exception:
                pass
            if not spare_capacity:
                break

        # Check local runtime_dir checkpoint as well
        if spare_capacity:
            checkpoint_file = _kernel_checkpoint_path(runtime_dir)
            if checkpoint_file.exists():
                try:
                    cp_data = _read_json(checkpoint_file)
                    if "WAITING" in str(cp_data.get("phase", "")).upper():
                        spare_capacity = False
                except Exception:
                    pass

        # 2. Global check: Total healthy reasoning providers <= 1
        circuit_files = []
        cf = runtime_dir / "provider-circuits.json"
        if cf.exists():
            circuit_files.append(cf)
        for cmd_root in store_roots:
            circuit_files.extend(list(cmd_root.glob("*/project-runtime/provider-circuits.json")))

        if circuit_files:
            try:
                from aos.provider_circuit import ProviderCircuitBreakerRegistry
                agg_reg = ProviderCircuitBreakerRegistry.aggregate_registries(
                    [ProviderCircuitBreakerRegistry(path) for path in circuit_files]
                )
                sm = agg_reg.summarize()
                if sm.get("healthy_reasoning_provider_count", 0) <= 1:
                    spare_capacity = False
            except Exception:
                pass

        if spare_capacity:
            attempts_file = runtime_dir / "provider-attempts.jsonl"
            if attempts_file.exists():
                try:
                    content = attempts_file.read_text(encoding="utf-8", errors="replace")[-2000:].upper()
                    unhealthy_signals = (
                        "QUOTA_EXHAUSTED", "UNAVAILABLE", "RATE_LIMIT",
                        "500", "502", "503", "504", "TIMEOUT", "CONNECTION",
                    )
                    if any(signal in content for signal in unhealthy_signals):
                        spare_capacity = False
                except Exception:
                    pass

        # Attempt to gather independent alternate proposals if spare capacity exists and alternates not provided

        collected_alternates: List[Tuple[str, Mapping[str, Any]]] = list(alternate_proposals)
        active_reviewers: List[Tuple[str, Any]] = []
        if spare_capacity and routing_policy_path and routing_policy_path.is_file() and schema:
            try:
                from aos.provider_registry import ProviderRouter, load_routing_policy
                from aos.autonomous_host import _PROVIDER_FACTORIES
                router = ProviderRouter(load_routing_policy(str(routing_policy_path)))
                available_providers = router.registry.list_providers()
                # Target up to 2 alternate policy-approved providers to reach 3 members total
                for entry in available_providers:
                    p_id = entry.provider_id
                    if entry.cloud_local == "CLOUD" and entry.credential_env_var and not os.environ.get(entry.credential_env_var):
                        continue
                    factory = _PROVIDER_FACTORIES.get(p_id)
                    if factory is None:
                        continue
                    try:
                        provider_inst = factory(entry.model_id)
                        if len(collected_alternates) < (COUNCIL_TARGET_MEMBER_COUNT - 1):
                            plan_data, _, _ = provider_inst.generate_plan(prompt, schema)
                            if isinstance(plan_data, dict):
                                collected_alternates.append((p_id, plan_data))
                        active_reviewers.append((p_id, provider_inst))
                    except Exception:
                        pass
            except Exception:
                pass

        auth_records = {k: v.to_dict() for k, v in situation.authority_records.items()}
        council.evaluate_decision(
            decision_type=decision_type,
            prompt=prompt,
            primary_proposal=primary_proposal,
            alternate_proposals=collected_alternates,
            authority_records=auth_records,
            project_id=situation.project_id,
            command_id=command_id or situation.identity(),
            is_spare_capacity_available=spare_capacity,
            is_real_execution=True,
            reviewers=active_reviewers,
        )
    except Exception:
        # Deliberation Council in SHADOW_ONLY mode must never crash primary execution path
        pass


def _situation_prompt_payload(
    situation: ProjectSituation,
    *,
    canonical_excerpt_max_chars: int = PLANNER_CANONICAL_EXCERPT_MAX_CHARS,
) -> Dict[str, Any]:
    return {
        "schema_version": situation.schema_version,
        "project_id": situation.project_id,
        "repository": situation.repository,
        "control_ref": situation.control_ref,
        "control_sha": situation.control_sha,
        "repository_head": situation.repository_head,
        "execution_base_sha": situation.execution_base_sha,
        "current_status": situation.current_status,
        "current_milestone": situation.current_milestone,
        "canonical_next_action": situation.canonical_next_action,
        "working_tree_state": situation.working_tree_state,
        "ci_state": situation.ci_state,
        "accepted_gates": situation.accepted_gates,
        "blocked_gates": situation.blocked_gates,
        "authority_ids": sorted(record.authority_id for record in situation.authority_records.values()),
        "goal": situation.goal,
        "constraints": list(situation.constraints),
        "red_lines": list(situation.red_lines),
        "completion_criteria": list(situation.completion_criteria),
        "ambiguity_reasons": list(situation.ambiguity_reasons),
        "canonical_excerpt": _bounded_prompt_excerpt(
            situation.canonical_excerpt, max_chars=canonical_excerpt_max_chars,
        ),
        "canonical_excerpt_chars": len(situation.canonical_excerpt),
        "canonical_excerpt_sha256": hashlib.sha256(
            situation.canonical_excerpt.encode("utf-8")
        ).hexdigest(),
    }


def select_objective(
    situation: ProjectSituation,
    routing_policy_path: Path,
    runtime_dir: Path,
    *,
    backend_override: Optional[Any] = None,
    replan_reason: Optional[str] = None,
) -> Objective:
    if situation.ambiguity_reasons:
        raise HumanRequired("CANONICAL_CONTRADICTION", {"ambiguity_reasons": list(situation.ambiguity_reasons)})
    authority_hint = next(iter(sorted(situation.authority_records)), "NONE")
    prompt = (
        "You are the AOS autonomous objective selector. Use ONLY the fresh canonical situation below. "
        "Choose the highest-value ready objective inside standing authority. Never ask the user what to do next. "
        "If multiple independent lanes are ready, identify them in parallel_candidates. Do not select production, "
        "destructive, secret-management, payment, legal/compliance or other red-line work. Return exactly the requested JSON.\n\n"
        f"REPLAN_REASON={replan_reason or 'INITIAL'}\n"
        + json.dumps(
            _situation_prompt_payload(
                situation, canonical_excerpt_max_chars=OBJECTIVE_CANONICAL_EXCERPT_MAX_CHARS,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    proposal = _reason(
        situation, routing_policy_path, runtime_dir, "objective-selection", prompt, OBJECTIVE_SCHEMA,
        authority_hint, backend_override=backend_override,
    )
    _shadow_deliberate(
        runtime_dir,
        situation,
        "FRONTIER_SELECTION",
        prompt,
        proposal,
        routing_policy_path=routing_policy_path,
        schema=OBJECTIVE_SCHEMA,
    )
    objective = Objective.from_dict(proposal)
    if objective.risk_class not in ("R0", "R1"):
        raise HumanRequired("OBJECTIVE_RISK_OUTSIDE_ROUTINE_STANDING_AUTHORITY", {"risk_class": objective.risk_class})
    return objective


class CanonicalAuthorityResolver:
    """Fail-closed resolver binding mutating tasks to fresh canonical decisions."""

    def __init__(self, situation: ProjectSituation):
        self.situation = situation

    @staticmethod
    def _dangerous(task: Mapping[str, Any]) -> Optional[str]:
        raw_payload = task.get("payload", {})
        payload = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True)
        lower_keys = {str(k).lower().replace("-", "_") for k in _walk_keys(raw_payload)}
        if lower_keys & _SECRET_KEYS:
            return "SECRET_BEARING_PAYLOAD"

        # Git red lines must be validated structurally, not only against JSON text.
        # json.dumps(["git", "push", "--force"]) produces punctuation between argv
        # tokens, so a whitespace-oriented regex can miss a real force push.
        if isinstance(raw_payload, Mapping):
            raw_cmd = raw_payload.get("cmd")
            if isinstance(raw_cmd, (list, tuple)):
                argv = [str(item).strip().lower() for item in raw_cmd if str(item).strip()]
                if argv and Path(argv[0]).name in ("git", "git.exe"):
                    args = argv[1:]
                    if "push" in args:
                        force_flags = {
                            "--force",
                            "-f",
                            "--force-with-lease",
                            "--force-if-includes",
                        }
                        if any(
                            arg in force_flags
                            or arg.startswith("--force=")
                            or arg.startswith("--force-with-lease=")
                            for arg in args
                        ):
                            return "RED_LINE_GIT_FORCE_PUSH"
                    if "reset" in args and "--hard" in args:
                        return "RED_LINE_GIT_RESET_HARD"
                    if "clean" in args:
                        return "RED_LINE_GIT_CLEAN"
                    if "rebase" in args:
                        return "RED_LINE_GIT_REBASE"
                    if "filter-branch" in args:
                        return "RED_LINE_GIT_FILTER_BRANCH"

        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, payload, re.I):
                return f"RED_LINE_PATTERN:{pattern}"
        return None

    def validate_task(self, task: Mapping[str, Any]) -> None:
        authority_id = str(task.get("authority_id", "")).strip()
        record = self.situation.authority_records.get(authority_id.upper())
        if record is None:
            raise AuthorityDenied(f"Authority {authority_id!r} is not present in fresh canonical decisions")
        if record.superseded:
            raise AuthorityDenied(f"Authority {authority_id} is superseded/revoked/consumed")
        risk = str(task.get("risk_class", "R0"))
        if risk not in ("R0", "R1"):
            raise AuthorityDenied(f"Risk class {risk} is outside routine standing authority")
        dangerous = self._dangerous(task)
        if dangerous:
            raise AuthorityDenied(dangerous)
        if record.production_allowed:
            # A production-capable decision is not enough to authorize this host's production use.
            pass
        text = record.text.upper()
        repo_tokens = {
            self.situation.repository.upper(),
            self.situation.repository.split("/")[-1].upper(),
            self.situation.project_id.upper(),
            self.situation.project_id.split("-")[0].upper(),
        }
        broad_markers = ("STANDING AUTHORITY", "PROGRAM V2", "ROUTINE NON-PROD", "NON-PRODUCTION", "NON_PRODUCTION")
        if not any(token and token in text for token in repo_tokens) and not any(marker in text for marker in broad_markers):
            raise AuthorityDenied(f"Authority {authority_id} does not prove repository/program scope")
        for tag in task.get("scope_tags", []) or []:
            token = str(tag).strip().upper().replace("-", " ")
            if token in ("LARI", "AOS", "NON PRODUCTION", "PROGRAM V2"):
                continue
            # Scope tags are advisory unless the authority explicitly contradicts them.
        for path in task.get("write_scope", []) or []:
            if not isinstance(path, str) or not path.strip():
                raise AuthorityDenied("Invalid write_scope path")
            candidate = (Path(self.situation.repository_head) if False else path)  # type guard placeholder
            if ".." in Path(path).parts:
                raise AuthorityDenied("write_scope escapes workspace")


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)



def _repairable_schema_version(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return float(value) == 1.0
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not text:
        return True
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if not all(part.isdigit() for part in parts):
        return False
    if len(parts) == 1:
        parts += ["0", "0"]
    elif len(parts) == 2:
        parts += ["0"]
    if len(parts) != 3:
        return False
    return tuple(int(part) for part in parts) == (1, 0, 0)


def _assert_plan_schema_envelope_only_defect(plan: Mapping[str, Any]) -> None:
    expected_top = set(PLAN_SCHEMA["properties"])
    actual_top = set(str(key) for key in plan)
    missing_top = (expected_top - {"schema_version"}) - actual_top
    extra_top = actual_top - expected_top
    if missing_top or extra_top:
        raise PlanningKernelError(
            f"Execution plan has non-envelope top-level contract defects: "
            f"missing={sorted(missing_top)} extra={sorted(extra_top)}"
        )

    raw_tasks = plan.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanningKernelError("Execution plan must contain at least one task")

    task_schema = PLAN_SCHEMA["properties"]["tasks"]["items"]
    expected_task = set(task_schema["properties"])
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise PlanningKernelError(f"Execution plan task {index} must be an object")
        actual_task = set(str(key) for key in raw)
        missing_task = expected_task - actual_task
        extra_task = actual_task - expected_task
        if missing_task or extra_task:
            raise PlanningKernelError(
                f"Execution plan task {index} has non-envelope contract defects: "
                f"missing={sorted(missing_task)} extra={sorted(extra_task)}"
            )

    groups = plan.get("parallel_safe_groups")
    if not isinstance(groups, list):
        raise PlanningKernelError("parallel_safe_groups must be an array")
    for group in groups:
        if not isinstance(group, list) or any(not isinstance(item, str) or not item.strip() for item in group):
            raise PlanningKernelError("parallel_safe_groups must contain arrays of non-empty node ids")

    rollback = plan.get("rollback_strategy")
    if not isinstance(rollback, str) or not rollback.strip():
        raise PlanningKernelError("rollback_strategy must be non-empty text")


def _normalize_plan_schema_envelope(
    plan: Mapping[str, Any],
    objective: Objective,
    situation: ProjectSituation,
    runtime_dir: Path,
) -> Dict[str, Any]:
    raw_version = plan.get("schema_version")
    if raw_version == SCHEMA_VERSION:
        return _validate_plan_shape(plan, objective, situation)

    if not _repairable_schema_version(raw_version):
        raise PlanningKernelError(
            f"Execution plan schema_version is not repairable as v1 envelope: {redact_secrets(str(raw_version))[:120]}"
        )

    # Fail closed unless schema_version is the sole envelope-level defect.
    _assert_plan_schema_envelope_only_defect(plan)

    candidate = dict(plan)
    candidate["schema_version"] = SCHEMA_VERSION
    normalized = _validate_plan_shape(candidate, objective, situation)

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "status": "APPLIED",
        "repair_scope": "PLAN_SCHEMA_ENVELOPE_METADATA_ONLY",
        "original_schema_version_type": type(raw_version).__name__,
        "original_schema_version": redact_secrets(str(raw_version))[:120] if raw_version is not None else None,
        "normalized_schema_version": SCHEMA_VERSION,
        "objective_id": objective.objective_id,
        "task_count": len(normalized.get("tasks", [])),
        "task_content_modified_by_repair": False,
        "authority_bypass": False,
        "unsafe_contract_bypass": False,
        "production": "NO_GO",
    }
    _atomic_json(runtime_dir / "plan-schema-envelope-repair.json", artifact)
    return normalized


def _validate_plan_shape(plan: Mapping[str, Any], objective: Objective, situation: ProjectSituation) -> Dict[str, Any]:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanningKernelError("Execution plan schema_version must be 1.0.0")
    if plan.get("objective_id") != objective.objective_id:
        raise PlanningKernelError("Execution plan objective_id mismatch")
    raw_tasks = plan.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanningKernelError("Execution plan must contain at least one task")
    tasks: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise PlanningKernelError("Task must be an object")
        task = dict(raw)
        node_id = _bounded_text(task.get("node_id"), 120, "node_id")
        if node_id in seen:
            raise PlanningKernelError(f"Duplicate node_id: {node_id}")
        seen.add(node_id)
        run_type = _bounded_text(task.get("run_type"), 40, "run_type").upper()
        if run_type not in _ALLOWED_RUN_TYPES:
            raise PlanningKernelError(f"Unsupported run_type: {run_type}")
        task["node_id"] = node_id
        task["run_type"] = run_type
        task["authority_id"] = _bounded_text(task.get("authority_id"), 160, "authority_id")
        task["risk_class"] = _bounded_text(task.get("risk_class", objective.risk_class), 16, "risk_class")
        if not isinstance(task.get("mutating"), bool):
            raise PlanningKernelError(f"Task {node_id} mutating must be boolean")
        if run_type == "PROCESS" and task["mutating"]:
            raise PlanningKernelError(
                f"Task {node_id} PROCESS execution cannot be mutating; use bounded FILE or GIT workers"
            )
        task["dependencies"] = _string_list(task.get("dependencies", []), "dependencies", 64, 120)
        task["scope_tags"] = _string_list(task.get("scope_tags", []), "scope_tags", 32, 120)
        task["write_scope"] = _string_list(task.get("write_scope", []), "write_scope", 64, 500)
        if run_type == "FILE" and task["mutating"] and not task["write_scope"]:
            raise PlanningKernelError(f"Task {node_id} mutating FILE task requires non-empty write_scope")
        if not isinstance(task.get("payload"), dict):
            raise PlanningKernelError(f"Task {node_id} payload must be object")
        _validate_worker_payload(node_id, run_type, task["payload"])
        if run_type == "FILE" and task["mutating"]:
            target_name = Path(str(task["payload"].get("path", "")).replace("\\", "/")).name.lower()
            target_stem = target_name.split(".", 1)[0]
            normalized_stem = re.sub(r"[^a-z0-9]+", "_", target_stem).strip("_")
            synthetic_suffix = any(
                normalized_stem == marker.replace("-", "_")
                or normalized_stem.endswith("_" + marker.replace("-", "_"))
                for marker in _SYNTHETIC_BOOKKEEPING_STEMS
            )
            if target_name in _SYNTHETIC_BOOKKEEPING_STEMS or synthetic_suffix:
                raise PlanningKernelError(
                    f"Task {node_id} attempts to create a synthetic bookkeeping artifact"
                )
        if run_type == "FILE" and task["payload"].get("action") == "write_file":
            target = str(task["payload"].get("path", ""))
            if task["mutating"] and not any(
                target == scope.rstrip("/") or target.startswith(scope.rstrip("/") + "/")
                for scope in task["write_scope"]
            ):
                raise PlanningKernelError(f"Task {node_id} FILE target is outside declared write_scope")
        for key in ("expected_artifacts", "tests", "evidence_requirements", "completion_criteria"):
            task[key] = _string_list(task.get(key, []), key, 64, 500)
        if run_type in _MUTATING_RUN_TYPES and task["mutating"] is False and run_type in ("FILE", "GIT"):
            # Git read operations exist, but planner must explicitly mark mutation truthfully.
            payload_text = json.dumps(task["payload"], sort_keys=True).lower()
            if any(x in payload_text for x in ("write", "patch", "commit", "push", "branch", "checkout")):
                raise PlanningKernelError(f"Task {node_id} understates mutation intent")
        tasks.append(task)
    for task in tasks:
        for dep in task["dependencies"]:
            if dep not in seen:
                raise PlanningKernelError(f"Task {task['node_id']} depends on unknown node {dep}")
    _assert_acyclic(tasks)
    normalized = dict(plan)
    normalized["tasks"] = tasks
    normalized["project_id"] = situation.project_id
    normalized["bound_source_sha"] = situation.control_sha
    if situation.execution_base_sha:
        normalized["bound_execution_base_sha"] = situation.execution_base_sha
    normalized["objective"] = dataclasses.asdict(objective)
    normalized["generated_at"] = _utc_now()
    return normalized


def _validate_worker_payload(node_id: str, run_type: str, payload: Mapping[str, Any]) -> None:
    """Reject planner output that cannot be dispatched by the selected native worker."""
    if not payload:
        raise PlanningKernelError(f"Task {node_id} payload must not be empty")

    if run_type == "FILE":
        action = payload.get("action")
        if action not in ("read_file", "write_file", "apply_patch"):
            raise PlanningKernelError(f"Task {node_id} FILE payload has unsupported action")
        if action in ("read_file", "write_file") and not str(payload.get("path", "")).strip():
            raise PlanningKernelError(f"Task {node_id} FILE {action} payload requires path")
        if action == "write_file" and not isinstance(payload.get("content"), str):
            raise PlanningKernelError(f"Task {node_id} FILE write_file payload requires content")
        if action == "apply_patch" and not str(payload.get("patch", "")).strip():
            raise PlanningKernelError(f"Task {node_id} FILE apply_patch payload requires patch")
        return

    if run_type in ("PROCESS", "TEST", "BUILD"):
        cmd = payload.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(
            isinstance(arg, str) and arg and arg == arg.strip() for arg in cmd
        ):
            raise PlanningKernelError(f"Task {node_id} {run_type} payload requires non-empty string argv cmd")
        binary = Path(cmd[0]).name.lower()
        if binary.endswith(".exe"):
            binary = binary[:-4]
        if binary not in NativeProcessWorker.ALLOWED_BINARIES:
            raise PlanningKernelError(f"Task {node_id} requests unsupported process binary: {binary}")
        lowered_args = [arg.lower() for arg in cmd[1:]]
        if binary in ("python", "py") and any(arg in ("-c", "-m") for arg in lowered_args):
            raise PlanningKernelError(f"Task {node_id} cannot use inline/module Python execution")
        if binary == "node" and any(arg in ("-e", "--eval", "-p", "--print") for arg in lowered_args):
            raise PlanningKernelError(f"Task {node_id} cannot use inline Node execution")
        env = payload.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(key, str) for key in env):
            raise PlanningKernelError(f"Task {node_id} {run_type} env must be an object with string keys")
        return

    if run_type == "GIT":
        action = payload.get("action")
        args = payload.get("args", [])
        if not isinstance(action, str) or not action.strip():
            raise PlanningKernelError(f"Task {node_id} GIT payload requires action")
        action = action.strip().lower()
        if action not in _ALLOWED_GIT_ACTIONS:
            raise PlanningKernelError(f"Task {node_id} requests unsupported git action: {action}")
        if not isinstance(args, list) or not all(
            isinstance(arg, str) and arg == arg.strip() for arg in args
        ):
            raise PlanningKernelError(f"Task {node_id} GIT payload args must be a string array")
        if action in NativeGitWorker.PROHIBITED_SUBCOMMANDS:
            raise PlanningKernelError(f"Task {node_id} requests prohibited git action: {action}")
        if action == "push" and any(
            arg in ("--force", "-f", "--force-with-lease", "--force-if-includes")
            or arg.startswith("--force=")
            or arg.startswith("--force-with-lease=")
            for arg in args
        ):
            raise PlanningKernelError(f"Task {node_id} requests prohibited force push")
        return

    if run_type == "CI" and not str(payload.get("sha", "")).strip():
        raise PlanningKernelError(f"Task {node_id} CI payload requires explicit sha")
    if run_type == "BROWSER" and not str(payload.get("url", "")).strip():
        raise PlanningKernelError(f"Task {node_id} BROWSER payload requires url")
    if run_type == "MODEL_REASONING":
        if not str(payload.get("prompt", "")).strip() or not isinstance(payload.get("schema"), dict):
            raise PlanningKernelError(f"Task {node_id} MODEL_REASONING payload requires prompt and schema")


def _assert_acyclic(tasks: Sequence[Mapping[str, Any]]) -> None:
    graph = {str(t["node_id"]): list(t.get("dependencies", [])) for t in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise PlanningKernelError("Execution plan contains dependency cycle")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def compile_execution_plan(
    situation: ProjectSituation,
    objective: Objective,
    routing_policy_path: Path,
    runtime_dir: Path,
    *,
    backend_override: Optional[Any] = None,
    capture_adapter_override: Optional[Any] = None,
    repair_context: Optional[Mapping[str, Any]] = None,
    forbidden_task_ids: Sequence[str] = (),
    forbidden_read_paths: Sequence[str] = (),
    forbidden_task_signatures: Sequence[str] = (),
    workspace: Optional[Path] = None,
    workspace_source_generation: Optional[str] = None,
    allow_recovered_repair: bool = True,
    batch_number: Optional[int] = None,
) -> Dict[str, Any]:
    forbidden_ids = {str(item) for item in forbidden_task_ids if str(item).strip()}
    forbidden_read_identities = {
        str(item).strip().lower()
        for item in forbidden_read_paths if str(item).strip()
    }
    forbidden_signatures = {
        str(item) for item in forbidden_task_signatures if str(item).strip()
    }
    workspace_manifest = _bounded_workspace_file_manifest(workspace, objective)
    available_process_binaries = _available_process_binaries()

    # If the workspace contains no Python scripts, remove python/py from available binaries and inject rule
    has_python_scripts = False
    if workspace is not None:
        try:
            has_python_scripts = any(workspace.resolve().glob("*.py")) or any(workspace.resolve().glob("*/*.py"))
        except Exception:
            has_python_scripts = False
    if not has_python_scripts:
        available_process_binaries = [b for b in available_process_binaries if b not in ("python", "py")]
        python_workspace_rule = (
            "\nPYTHON_UNAVAILABLE_RULE: There are NO Python scripts in this workspace. "
            "Python commands (`python` or `py`) are FORBIDDEN and will be rejected. "
            "Use ONLY available binaries (e.g. git) or FILE actions (write_file, read_file).\n"
        )
    else:
        python_workspace_rule = ""

    # Pre-Implementation Design Intelligence Stage (R10-R15) for UI Planning
    di_pre_evidence: Optional[Dict[str, Any]] = None
    di_remediation_findings: List[str] = []
    if _objective_task_class(objective) == TaskClass.REPO_UI_PLANNING.value and workspace is not None:
        try:
            from extensions.design_intelligence.contracts import DesignProjectBrief
            from extensions.design_intelligence.design_loop import AutonomousDesignLoopPipeline
            from extensions.design_intelligence.browser_capture import RealBrowserCaptureAdapter
            from extensions.design_intelligence.critics import DesignCriticEnsemble, VisualCriticAdapter

            ws_root = workspace.resolve()
            index_html_path = ws_root / "index.html"
            if not index_html_path.is_file():
                di_pre_evidence = {
                    "schema_version": "1.0.0",
                    "stage": "PRE_IMPLEMENTATION",
                    "project_id": situation.project_id,
                    "batch_number": batch_number,
                    "outcome": "DESIGN_EVIDENCE_UNAVAILABLE",
                    "reason": f"Required UI render entrypoint not found: {index_html_path}",
                }
            else:
                real_html = index_html_path.read_text(encoding="utf-8", errors="replace")
                index_css_path = ws_root / "index.css"
                real_css = index_css_path.read_text(encoding="utf-8", errors="replace") if index_css_path.is_file() else ""

                evidence_dir = runtime_dir / "screenshots" / f"batch-{int(batch_number or 0):04d}-pre"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                capture_adapter = capture_adapter_override or RealBrowserCaptureAdapter(output_dir=str(evidence_dir))

                try:
                    visual_manifest = capture_adapter.capture_manifest(str(index_html_path), f"pre-{int(batch_number or 0):04d}")
                except Exception as capture_exc:
                    exc_msg = str(capture_exc)
                    unavail_tokens = ("not installed", "executable doesn't exist", "please run", "browser", "playwright")
                    if any(t in exc_msg.lower() for t in unavail_tokens):
                        outcome_type = "DESIGN_EVIDENCE_UNAVAILABLE"
                    else:
                        outcome_type = "DESIGN_RUNTIME_FAILURE"
                    di_pre_evidence = {
                        "schema_version": "1.0.0",
                        "stage": "PRE_IMPLEMENTATION",
                        "project_id": situation.project_id,
                        "batch_number": batch_number,
                        "outcome": outcome_type,
                        "error": exc_msg,
                        "reason": f"Visual screenshot capture unavailable: {exc_msg}",
                    }
                    visual_manifest = None

                if visual_manifest is not None:
                    brief = DesignProjectBrief(
                        brief_id=f"brief-{situation.project_id}",
                        project_id=situation.project_id,
                        tenant_name=situation.project_id,
                        industry="Software & Healthcare",
                        target_audience="Clinical & Operations Users",
                        core_job_to_be_done=objective.title,
                        brand_posture="Clinical Precision",
                    )
                    from extensions.design_intelligence.visual_provider import RealVisualCriticAdapter

                    visual_adapter = None
                    if os.environ.get("GEMINI_API_KEY", "").strip():
                        visual_adapter = RealVisualCriticAdapter()
                    critic_ensemble = DesignCriticEnsemble(visual_adapter=visual_adapter)
                    pipeline = AutonomousDesignLoopPipeline(critic_ensemble=critic_ensemble, max_design_review_cycles=2)
                    di_res = pipeline.run_pipeline(
                        brief=brief,
                        initial_html=real_html,
                        initial_css=real_css,
                        evidence_manifest=visual_manifest,
                    )

                    passed_critics = [f.critic_name for f in di_res.final_scorecard.critic_findings if f.verdict.value == "PASS"]
                    failing_findings = [f"{f.critic_name}: {f.details}" for f in di_res.final_scorecard.critic_findings if f.verdict.value == "FAIL"]
                    di_remediation_findings = list(failing_findings)
                    if di_res.blockers:
                        di_remediation_findings.extend(di_res.blockers)

                    outcome = "DESIGN_INTELLIGENCE_SUCCESS" if di_res.overall_verdict.value == "PASS" else "DESIGN_REMEDIATION_REQUIRED"
                    di_pre_evidence = {
                        "schema_version": "1.0.0",
                        "design_intelligence_execution_id": di_res.pipeline_id,
                        "stage": "PRE_IMPLEMENTATION",
                        "project_id": situation.project_id,
                        "batch_number": batch_number,
                        "outcome": outcome,
                        "overall_verdict": di_res.overall_verdict.value,
                        "cycles_completed": di_res.cycles_completed,
                        "human_review_state": di_res.human_review_state.value,
                        "executed_rules": ["R10", "R11", "R12", "R13", "R14", "R15", "R17"],
                        "critics_passed": passed_critics,
                        "remediation_findings": di_remediation_findings,
                        "visual_manifest_id": visual_manifest.manifest_id,
                        "viewports_captured": visual_manifest.viewports_captured,
                        "file_hashes": visual_manifest.file_hashes,
                    }

            if di_pre_evidence:
                di_artifact_path = runtime_dir / f"design-intelligence-pre-{int(batch_number or 0):04d}.json"
                _atomic_json(di_artifact_path, di_pre_evidence)
        except Exception as exc:
            di_pre_evidence = {
                "schema_version": "1.0.0",
                "stage": "PRE_IMPLEMENTATION",
                "project_id": situation.project_id,
                "batch_number": batch_number,
                "outcome": "DESIGN_RUNTIME_FAILURE",
                "error": str(exc),
            }
            di_artifact_path = runtime_dir / f"design-intelligence-pre-{int(batch_number or 0):04d}.json"
            _atomic_json(di_artifact_path, di_pre_evidence)

    design_intelligence_guidance = ""
    if _objective_task_class(objective) == TaskClass.REPO_UI_PLANNING.value:
        design_intelligence_guidance = (
            f"\nDESIGN_INTELLIGENCE_POLICY=MANDATORY_FOR_UI_LANE\n"
            f"DESIGN_PRE_OUTCOME={di_pre_evidence.get('outcome') if di_pre_evidence else 'NONE'}\n"
            f"DESIGN_REMEDIATION_FINDINGS={json.dumps(di_remediation_findings, ensure_ascii=False)}\n"
            "UI_PLANNING_RULE: The execution plan MUST explicitly address and remediate all failing design critic findings "
            "and visual issues identified in DESIGN_REMEDIATION_FINDINGS. Ensure visual elements, responsiveness across all 6 viewports, "
            "and brand posture are strictly advanced."
        )

    prompt = (
        "You are the AOS Planner->DAG compiler. Produce a bounded non-production execution plan for the selected objective. "
        "The plan is advisory until validated. Use only worker payload formats proven by the worker source below. "
        "Every task payload MUST be non-empty and executable as-is; never emit placeholder or omitted worker arguments. "
        "Never create bookkeeping, status, next_action, or completion marker files; tasks must advance or verify canonical product work. "
        "Use at most four concise tasks in one small meaningful batch, with concise tests/evidence and safe parallelism. "
        "Every task requires a canonical authority_id. Never emit production, force-push, history rewrite, destructive, secret, payment, "
        "legal/compliance, or material trust/security changes. Do not invent evidence. "
        "The top-level schema_version MUST be the exact string \"1.0.0\". Return exactly the requested JSON.\n\n"
        f"OBJECTIVE={json.dumps(dataclasses.asdict(objective), ensure_ascii=False, sort_keys=True)}\n"
        f"SITUATION={json.dumps(_situation_prompt_payload(situation), ensure_ascii=False, sort_keys=True)}\n"
        f"REPAIR_CONTEXT={json.dumps(dict(repair_context or {}), ensure_ascii=False, sort_keys=True)}\n"
        f"COMPLETED_TASK_IDS_NOT_TO_REPEAT={json.dumps(sorted(forbidden_ids), ensure_ascii=False)}\n"
        f"COMPLETED_READ_IDENTITIES_NOT_TO_REPEAT={json.dumps(sorted(forbidden_read_identities), ensure_ascii=False)}\n"
        "COMPLETED_ACTION_SIGNATURES_NOT_TO_REPEAT="
        f"{json.dumps(_bounded_task_signatures_for_prompt(sorted(forbidden_signatures)), ensure_ascii=False, sort_keys=True)}\n"
        "COMPLETED_READ_CONTEXT_RULE=Treat completed_read_context as fresh, hash-bound local observation. "
        "Use it to plan concrete product work or verification; do not reread an unchanged file in the same source generation. "
        "A changed file or source generation is a new read identity and may be read again.\n"
        "PROGRESS_RULE=Do not repeat a completed action under a new task id. A batch made entirely of generic "
        "git identity/status checks and runtime version probes is invalid because it does not advance product work.\n"
        "FILE_READ_RULE=Use read_file only for an exact representative_existing_path below or for an exact "
        "expected_artifact declared by a transitive dependency. Never invent next_action, status, report, or marker files; "
        "when no relevant path is known, prefer bounded GIT status/diff/log/ls-files or PROCESS/TEST verification.\n"
        f"WORKSPACE_FILE_MANIFEST={json.dumps(workspace_manifest, ensure_ascii=False, sort_keys=True)}\n"
        "PROCESS_BINARY_RULE=Use PROCESS/TEST/BUILD only when cmd[0] is listed in AVAILABLE_PROCESS_BINARIES; "
        "an allowlisted but unavailable binary is not executable and must not be planned. Never use python -c or "
        "python -m; Python may only receive an existing workspace-relative script path.\n"
        f"{python_workspace_rule}"
        f"AVAILABLE_PROCESS_BINARIES={json.dumps(available_process_binaries)}\n"
        f"{design_intelligence_guidance}\n"
        f"WORKER_CONTRACTS={_worker_contract_summary()}"
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    recovered_repair = (
        _recover_waiting_plan_repair(
            runtime_dir, batch_number, situation, objective, prompt_sha256,
        )
        if allow_recovered_repair else None
    )
    proposal: Dict[str, Any] = recovered_repair[0] if recovered_repair else {}
    validation_error = recovered_repair[1] if recovered_repair else ""
    normalized: Optional[Dict[str, Any]] = None
    for attempt in ((1,) if recovered_repair else (0, 1)):
        attempt_prompt = prompt
        if attempt:
            python_guidance = ""
            if "inline/module Python execution" in validation_error or "Python payload requires an existing workspace-relative script" in validation_error or "Python script does not exist" in validation_error:
                python_guidance = (
                    "\nPYTHON_PAYLOAD_RULE: Python inline execution (`-c` or `-m`) is prohibited. "
                    "Python commands may ONLY invoke an existing workspace-relative script path (e.g. `python path/to/script.py`). "
                    "For all other execution, you MUST use ONLY the exact binaries present in AVAILABLE_PROCESS_BINARIES (e.g. git). "
                    "Never plan npm, npx, or node if they are not listed in AVAILABLE_PROCESS_BINARIES."
                )
            elif "process binary is unavailable in runtime environment" in validation_error:
                python_guidance = (
                    f"\nPROCESS_BINARY_RULE: The requested binary is unavailable on this host. "
                    f"You MUST use ONLY binaries present in AVAILABLE_PROCESS_BINARIES: {json.dumps(available_process_binaries)}. "
                    "Do not plan tasks for npm, npx, node, or any binary not in that list."
                )
            elif "repeats completed FILE read target" in validation_error or "repeats completed action signatures" in validation_error or "repeats completed task identities" in validation_error:
                python_guidance = (
                    "\nACTION_DEDUPLICATION_RULE: Do not repeat action signatures or task identities from previously completed batches. "
                    "All previously executed actions (e.g. repeated `read_file` on completed paths, identical `git status` commands, or identical task IDs) are strictly forbidden. "
                    "Instead, plan forward-advancing tasks: write new artifacts, inspect novel unexamined files, run different verification checks, or execute authorized non-duplicate milestones."
                )
            attempt_prompt += (
                "\n\nVALIDATION_REPAIR_REQUIRED: The previous proposal was rejected locally and was not executed. "
                "Return a corrected full plan; do not repeat the defect.\n"
                f"VALIDATION_ERROR={validation_error}"
                f"{python_guidance}\n"
                f"PREVIOUS_INVALID_PLAN={json.dumps(proposal, ensure_ascii=False, sort_keys=True)[:2500]}"
            )
        proposal = _reason(
            situation,
            routing_policy_path,
            runtime_dir,
            "plan-dag" if attempt == 0 else "plan-dag-repair",
            attempt_prompt,
            PLAN_SCHEMA,
            objective.authority_id,
            backend_override=backend_override,
            task_class=_objective_task_class(objective),
        )
        try:
            repair_pruned: Dict[str, str] = {}
            pre_salvage_applied = False
            proposal_for_validation = proposal
            if attempt and isinstance(proposal.get("tasks"), list):
                raw_tasks = proposal["tasks"]
                for task in raw_tasks:
                    if not isinstance(task, Mapping) or task.get("run_type") not in ("PROCESS", "TEST", "BUILD"):
                        continue
                    payload = task.get("payload")
                    cmd = payload.get("cmd") if isinstance(payload, Mapping) else None
                    if not isinstance(cmd, list) or len(cmd) < 2:
                        continue
                    executable = Path(str(cmd[0])).name.lower().removesuffix(".exe")
                    if executable in ("python", "py") and str(cmd[1]).startswith("-"):
                        repair_pruned[str(task.get("node_id", ""))] = "PYTHON_INLINE_OR_MODULE"
                changed = True
                while changed:
                    changed = False
                    for task in raw_tasks:
                        node_id = str(task.get("node_id", "")) if isinstance(task, Mapping) else ""
                        if node_id in repair_pruned or not isinstance(task, Mapping):
                            continue
                        if any(str(dependency) in repair_pruned for dependency in task.get("dependencies", [])):
                            repair_pruned[node_id] = "DEPENDS_ON_REJECTED_TASK"
                            changed = True
                if repair_pruned and len(repair_pruned) < len(raw_tasks):
                    pre_salvage_applied = True
                    retained_ids = {
                        str(task.get("node_id", "")) for task in raw_tasks
                        if isinstance(task, Mapping) and str(task.get("node_id", "")) not in repair_pruned
                    }
                    proposal_for_validation = {
                        **proposal,
                        "tasks": [
                            task for task in raw_tasks
                            if isinstance(task, Mapping) and str(task.get("node_id", "")) in retained_ids
                        ],
                        "parallel_safe_groups": [
                            retained
                            for group in proposal.get("parallel_safe_groups", [])
                            if (retained := [str(node_id) for node_id in group if str(node_id) in retained_ids])
                        ],
                    }
            normalized = _normalize_plan_schema_envelope(
                proposal_for_validation, objective, situation, runtime_dir,
            )
            if attempt:
                # A repair response can correct most of a plan while stubbornly
                # retaining one independently-invalid task.  Preserve the safe,
                # useful portion instead of discarding the entire bounded batch.
                # Tasks that depend on a rejected task are rejected transitively;
                # an all-invalid plan still fails closed below.
                rejected: Dict[str, str] = dict(repair_pruned)
                tasks_by_id = {task["node_id"]: task for task in normalized["tasks"]}
                workspace_root = workspace.resolve() if workspace is not None else None
                for task in normalized["tasks"]:
                    node_id = task["node_id"]
                    signature = _task_signature(task)
                    if node_id in forbidden_ids:
                        rejected[node_id] = "COMPLETED_TASK_ID"
                        continue
                    is_read_task = (
                        task["run_type"] == "FILE"
                        and task["payload"].get("action") == "read_file"
                    )
                    if signature in forbidden_signatures and not is_read_task:
                        rejected[node_id] = "COMPLETED_ACTION_SIGNATURE"
                        continue
                    if task["run_type"] in ("PROCESS", "TEST", "BUILD"):
                        cmd = task["payload"]["cmd"]
                        binary = str(cmd[0])
                        if shutil.which(binary) is None:
                            rejected[node_id] = "PROCESS_BINARY_UNAVAILABLE"
                            continue
                        executable = Path(binary).name.lower().removesuffix(".exe")
                        if executable in ("python", "py") and workspace_root is not None:
                            if len(cmd) < 2 or str(cmd[1]).startswith("-"):
                                rejected[node_id] = "PYTHON_INLINE_OR_MODULE"
                                continue
                            script = (workspace_root / str(cmd[1])).resolve()
                            if (
                                (script != workspace_root and workspace_root not in script.parents)
                                or not script.is_file()
                            ):
                                rejected[node_id] = "PYTHON_SCRIPT_UNAVAILABLE"
                                continue
                    if task["run_type"] == "FILE" and task["payload"].get("action") == "read_file":
                        read_identity = _current_read_identity(
                            workspace,
                            str(task["payload"].get("path", "")),
                            workspace_source_generation,
                        )
                        if read_identity in forbidden_read_identities:
                            rejected[node_id] = "COMPLETED_READ_PATH"

                changed = True
                while changed:
                    changed = False
                    for task in normalized["tasks"]:
                        node_id = task["node_id"]
                        if node_id in rejected:
                            continue
                        if any(dependency in rejected for dependency in task.get("dependencies", [])):
                            rejected[node_id] = "DEPENDS_ON_REJECTED_TASK"
                            changed = True

                normalized_rejected = set(rejected) & set(tasks_by_id)
                post_salvage_applied = False
                if normalized_rejected and len(normalized_rejected) < len(normalized["tasks"]):
                    post_salvage_applied = True
                    retained_ids = set(tasks_by_id) - normalized_rejected
                    normalized = {
                        **normalized,
                        "tasks": [task for task in normalized["tasks"] if task["node_id"] in retained_ids],
                        "parallel_safe_groups": [
                            retained
                            for group in normalized.get("parallel_safe_groups", [])
                            if (retained := [node_id for node_id in group if node_id in retained_ids])
                        ],
                    }
                if pre_salvage_applied or post_salvage_applied:
                    _atomic_json(runtime_dir / f"plan-dag-pruning-{int(batch_number or 0):04d}.json", {
                        "schema_version": "1.0.0",
                        "status": "APPLIED",
                        "reason": "REPAIR_RESPONSE_PARTIAL_SALVAGE",
                        "rejected_tasks": rejected,
                        "retained_task_ids": sorted(task["node_id"] for task in normalized["tasks"]),
                    })
            for task in normalized["tasks"]:
                if task["run_type"] not in ("PROCESS", "TEST", "BUILD"):
                    continue
                binary = str(task["payload"]["cmd"][0])
                if shutil.which(binary) is None:
                    raise PlanningKernelError(
                        f"Task {task['node_id']} process binary is unavailable in runtime environment: {binary}"
                    )
            if workspace is not None:
                workspace_root = workspace.resolve()
                tasks_by_id = {task["node_id"]: task for task in normalized["tasks"]}
                for task in normalized["tasks"]:
                    if task["run_type"] not in ("PROCESS", "TEST", "BUILD"):
                        continue
                    cmd = task["payload"]["cmd"]
                    binary = Path(str(cmd[0])).name.lower().removesuffix(".exe")
                    if binary not in ("python", "py"):
                        continue
                    if len(cmd) < 2 or str(cmd[1]).startswith("-"):
                        raise PlanningKernelError(
                            f"Task {task['node_id']} Python payload requires an existing workspace-relative script"
                        )
                    script = (workspace_root / str(cmd[1])).resolve()
                    if script != workspace_root and workspace_root not in script.parents:
                        raise PlanningKernelError(
                            f"Task {task['node_id']} Python script escapes managed workspace"
                        )
                    if not script.is_file():
                        raise PlanningKernelError(
                            f"Task {task['node_id']} Python script does not exist: {cmd[1]}"
                        )

                def dependency_ancestors(task: Mapping[str, Any]) -> set[str]:
                    ancestors: set[str] = set()
                    pending = list(task.get("dependencies", []))
                    while pending:
                        dependency_id = pending.pop()
                        if dependency_id in ancestors:
                            continue
                        ancestors.add(dependency_id)
                        dependency = tasks_by_id.get(dependency_id)
                        if dependency is not None:
                            pending.extend(dependency.get("dependencies", []))
                    return ancestors

                def is_declared_upstream_artifact(task: Mapping[str, Any], target: Path) -> bool:
                    for dependency_id in dependency_ancestors(task):
                        dependency = tasks_by_id[dependency_id]
                        for artifact in dependency.get("expected_artifacts", []):
                            artifact_path = (workspace_root / str(artifact)).resolve()
                            if artifact_path != workspace_root and workspace_root not in artifact_path.parents:
                                continue
                            if artifact_path == target:
                                return True
                    return False

                for task in normalized["tasks"]:
                    if task["run_type"] != "FILE" or task["payload"].get("action") != "read_file":
                        continue
                    read_identity = _current_read_identity(
                        workspace,
                        str(task["payload"].get("path", "")),
                        workspace_source_generation,
                    )
                    if read_identity in forbidden_read_identities:
                        raise PlanningKernelError(
                            f"Task {task['node_id']} repeats completed FILE read target: "
                            f"{task['payload'].get('path')}"
                        )
                    target = (workspace_root / str(task["payload"].get("path", ""))).resolve()
                    if target != workspace_root and workspace_root not in target.parents:
                        raise PlanningKernelError(
                            f"Task {task['node_id']} FILE read target escapes managed workspace"
                        )
                    if not target.is_file() and not is_declared_upstream_artifact(task, target):
                        raise PlanningKernelError(
                            f"Task {task['node_id']} FILE read target does not exist: "
                            f"{task['payload'].get('path')}"
                        )
            duplicates = sorted(
                task["node_id"] for task in normalized["tasks"] if task["node_id"] in forbidden_ids
            )
            if duplicates:
                raise PlanningKernelError(
                    f"Execution plan repeats completed task identities: {duplicates}"
                )
            repeated_actions = sorted({
                _task_signature(task)
                for task in normalized["tasks"]
                if _task_signature(task) in forbidden_signatures
                and not (
                    task["run_type"] == "FILE"
                    and task["payload"].get("action") == "read_file"
                )
            })
            if repeated_actions:
                raise PlanningKernelError(
                    f"Execution plan repeats completed action signatures: {repeated_actions}"
                )
            if normalized["tasks"] and all(_is_generic_readiness_task(task) for task in normalized["tasks"]):
                raise PlanningKernelError(
                    "Execution plan contains only generic environment readiness checks and does not advance product work"
                )
            resolver = CanonicalAuthorityResolver(situation)
            for task in normalized["tasks"]:
                resolver.validate_task(task)
            break
        except PlanningKernelError as exc:
            validation_error = redact_secrets(str(exc))[:500]
            if attempt:
                _save_waiting_plan_repair(
                    runtime_dir, batch_number, situation, objective, prompt_sha256,
                    proposal, validation_error, status="EXHAUSTED",
                )
                raise PlannerValidationExhausted(
                    f"PLANNER_VALIDATION_REPAIR_EXHAUSTED: {validation_error}"
                ) from exc
            _save_waiting_plan_repair(
                runtime_dir, batch_number, situation, objective, prompt_sha256,
                proposal, validation_error, status="PENDING",
            )

    if normalized is None:  # pragma: no cover - loop either succeeds or raises
        raise PlanningKernelError("Execution plan validation did not produce a plan")
    _shadow_deliberate(
        runtime_dir,
        situation,
        "REPLANNING" if repair_context else "ARCHITECTURE",
        prompt,
        normalized,
        routing_policy_path=routing_policy_path,
        schema=PLAN_SCHEMA,
    )

    # Attach pre-implementation Design Intelligence evidence to plan if present
    if di_pre_evidence:
        normalized["design_intelligence_execution_id"] = di_pre_evidence.get("design_intelligence_execution_id")
        normalized["design_intelligence_evidence"] = di_pre_evidence
        normalized["design_intelligence_outcome"] = di_pre_evidence.get("outcome")

    return normalized


def classify_batch_failure(receipt: Mapping[str, Any], batch_runtime: Path) -> str:
    failed = [str(x).lower() for x in receipt.get("failed_task_ids", [])]
    joined = " ".join(failed)
    if "test" in joined:
        return "TEST_FAILURE"
    if "build" in joined:
        return "BUILD_FAILURE"
    if "browser" in joined:
        return "BROWSER_FAILURE"
    if "ci" in joined:
        return "CI_FAILURE"
    attempts = batch_runtime / "provider-attempts.jsonl"
    if attempts.exists():
        text = attempts.read_text(encoding="utf-8", errors="replace").upper()
        if "QUOTA_EXHAUSTED" in text:
            return "PROVIDER_QUOTA_EXHAUSTED"
        if "UNAVAILABLE" in text:
            return "PROVIDER_UNAVAILABLE"
    return "RETRYABLE_EXECUTION"


def detect_completion(
    situation: ProjectSituation,
    routing_policy_path: Path,
    runtime_dir: Path,
    recent_receipt: Mapping[str, Any],
    *,
    backend_override: Optional[Any] = None,
) -> Dict[str, Any]:
    if recent_receipt.get("failed_task_ids"):
        return {"disposition": "REPLAN", "rationale": "Recent batch has failed tasks", "satisfied_criteria": [], "unsatisfied_criteria": list(situation.completion_criteria)}
    prompt = (
        "You are the AOS project completion detector. DAG_EMPTY is NOT PROJECT_COMPLETE. "
        "PROJECT_COMPLETE is allowed only if all applicable canonical roadmap/completion criteria are actually satisfied by evidence. "
        "If authorized work remains, return REPLAN. If a genuine human red-line/authority boundary is reached, return HUMAN_REQUIRED. "
        "Do not infer completion from absence of a current task. Return exactly the requested JSON.\n\n"
        f"SITUATION={json.dumps(_situation_prompt_payload(situation, canonical_excerpt_max_chars=COMPLETION_CANONICAL_EXCERPT_MAX_CHARS), ensure_ascii=False, sort_keys=True)}\n"
        f"RECENT_RECEIPT={json.dumps(dict(recent_receipt), ensure_ascii=False, sort_keys=True)}"
    )
    authority_hint = next(iter(sorted(situation.authority_records)), "NONE")
    proposal = _reason(
        situation, routing_policy_path, runtime_dir, "completion-detection", prompt, COMPLETION_SCHEMA,
        authority_hint, backend_override=backend_override,
    )
    disposition = proposal.get("disposition")
    if disposition not in ("PROJECT_COMPLETE", "REPLAN", "HUMAN_REQUIRED"):
        raise PlanningKernelError("Invalid completion disposition")
    if disposition == "PROJECT_COMPLETE":
        next_action = (situation.canonical_next_action or "").upper()
        status = (situation.current_status or "").upper()
        canonical_complete = any(token in status for token in ("COMPLETE", "CLOSED", "DONE")) or any(
            token in next_action for token in ("PROJECT COMPLETE", "NO FURTHER", "CLOSED", "COMPLETE")
        )
        if not canonical_complete:
            proposal = dict(proposal)
            proposal["disposition"] = "REPLAN"
            proposal["rationale"] = "Provider proposed PROJECT_COMPLETE but canonical state does not prove project completion"
    _shadow_deliberate(
        runtime_dir,
        situation,
        "COMPLETION_ASSESSMENT",
        prompt,
        proposal,
        routing_policy_path=routing_policy_path,
        schema=COMPLETION_SCHEMA,
    )
    return dict(proposal)


def _kernel_checkpoint_path(runtime_dir: Path) -> Path:
    return runtime_dir / "planning-kernel-checkpoint.json"


def _write_kernel_checkpoint(runtime_dir: Path, state: Mapping[str, Any]) -> None:
    payload = dict(state)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload["updated_at"] = _utc_now()
    _atomic_json(_kernel_checkpoint_path(runtime_dir), payload)


def _receipt_sha256(receipt: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_repair_artifact_path(runtime_dir: Path, batch_number: int) -> Path:
    return runtime_dir / f"plan-dag-repair-{batch_number:04d}.json"


def _recover_waiting_plan_repair(
    runtime_dir: Path,
    batch_number: Optional[int],
    situation: ProjectSituation,
    objective: Objective,
    prompt_sha256: str,
) -> Optional[Tuple[Dict[str, Any], str]]:
    if batch_number is None:
        return None
    artifact = _read_json(_plan_repair_artifact_path(runtime_dir, batch_number))
    if set(artifact) != {
        "schema_version", "status", "batch_number", "situation_id",
        "canonical_source_sha", "canonical_execution_base_sha", "objective_id",
        "prompt_sha256", "validation_error", "previous_invalid_plan",
    }:
        return None
    if (
        artifact.get("schema_version") != SCHEMA_VERSION
        or artifact.get("status") != "PENDING"
        or artifact.get("batch_number") != batch_number
        or artifact.get("situation_id") != situation.identity()
        or artifact.get("canonical_source_sha") != situation.control_sha
        or artifact.get("canonical_execution_base_sha") != situation.execution_base_sha
        or artifact.get("objective_id") != objective.objective_id
        or artifact.get("prompt_sha256") != prompt_sha256
        or not isinstance(artifact.get("validation_error"), str)
        or not isinstance(artifact.get("previous_invalid_plan"), dict)
    ):
        return None
    return dict(artifact["previous_invalid_plan"]), artifact["validation_error"]


def _save_waiting_plan_repair(
    runtime_dir: Path,
    batch_number: Optional[int],
    situation: ProjectSituation,
    objective: Objective,
    prompt_sha256: str,
    proposal: Mapping[str, Any],
    validation_error: str,
    status: str = "PENDING",
) -> None:
    if batch_number is None:
        return
    _atomic_json(_plan_repair_artifact_path(runtime_dir, batch_number), {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "batch_number": batch_number,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "objective_id": objective.objective_id,
        "prompt_sha256": prompt_sha256,
        "validation_error": validation_error,
        "previous_invalid_plan": dict(proposal),
    })


def _recover_waiting_completion(
    runtime_dir: Path,
    batch_number: int,
    checkpoint: Mapping[str, Any],
    prior_situation: Mapping[str, Any],
    fresh_situation: ProjectSituation,
    recent_receipt: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Reuse a REPLAN decision only across its exact provider-wait boundary."""
    if checkpoint.get("phase") != "WAITING_FOR_REASONING_PROVIDER":
        return None
    situation_id = fresh_situation.identity()
    if (
        checkpoint.get("batch_number") != batch_number
        or checkpoint.get("situation_id") != situation_id
        or checkpoint.get("canonical_source_sha") != fresh_situation.control_sha
        or checkpoint.get("canonical_execution_base_sha") != fresh_situation.execution_base_sha
        or prior_situation.get("situation_id") != situation_id
        or prior_situation.get("control_sha") != fresh_situation.control_sha
        or prior_situation.get("execution_base_sha") != fresh_situation.execution_base_sha
    ):
        return None
    artifact = _read_json(runtime_dir / f"completion-{batch_number:04d}.json")
    if set(artifact) != {
        "schema_version", "situation_id", "canonical_source_sha",
        "canonical_execution_base_sha", "recent_receipt_sha256", "completion",
    }:
        return None
    if (
        artifact.get("schema_version") != SCHEMA_VERSION
        or artifact.get("situation_id") != situation_id
        or artifact.get("canonical_source_sha") != fresh_situation.control_sha
        or artifact.get("canonical_execution_base_sha") != fresh_situation.execution_base_sha
        or artifact.get("recent_receipt_sha256") != _receipt_sha256(recent_receipt)
    ):
        return None
    completion = artifact.get("completion")
    if not isinstance(completion, dict) or not Draft202012Validator(COMPLETION_SCHEMA).is_valid(completion):
        return None
    if completion.get("disposition") != "REPLAN":
        return None
    return dict(completion)


def _save_replan_completion(
    runtime_dir: Path,
    batch_number: int,
    situation: ProjectSituation,
    recent_receipt: Mapping[str, Any],
    completion: Mapping[str, Any],
) -> None:
    if completion.get("disposition") != "REPLAN":
        return
    _atomic_json(runtime_dir / f"completion-{batch_number:04d}.json", {
        "schema_version": SCHEMA_VERSION,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "recent_receipt_sha256": _receipt_sha256(recent_receipt),
        "completion": dict(completion),
    })


def _recover_waiting_objective(
    runtime_dir: Path,
    batch_number: int,
    checkpoint: Mapping[str, Any],
    prior_situation: Mapping[str, Any],
    fresh_situation: ProjectSituation,
) -> Optional[Objective]:
    """Reuse a durable objective only across an identical provider-wait retry."""
    if checkpoint.get("phase") != "WAITING_FOR_REASONING_PROVIDER":
        return None
    situation_id = fresh_situation.identity()
    if (
        checkpoint.get("batch_number") != batch_number
        or checkpoint.get("situation_id") != situation_id
        or checkpoint.get("canonical_source_sha") != fresh_situation.control_sha
        or checkpoint.get("canonical_execution_base_sha") != fresh_situation.execution_base_sha
        or prior_situation.get("situation_id") != situation_id
        or prior_situation.get("control_sha") != fresh_situation.control_sha
        or prior_situation.get("execution_base_sha") != fresh_situation.execution_base_sha
    ):
        return None
    raw_objective = _read_json(runtime_dir / f"objective-{batch_number:04d}.json")
    if not raw_objective or not Draft202012Validator(OBJECTIVE_SCHEMA).is_valid(raw_objective):
        return None
    try:
        objective = Objective.from_dict(raw_objective)
    except (PlanningKernelError, TypeError, ValueError):
        return None
    record = fresh_situation.authority_records.get(objective.authority_id.upper())
    if objective.risk_class not in ("R0", "R1") or record is None or record.superseded:
        return None
    return objective


def _bounded_runtime_evidence(batch_runtime: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name in ("host-receipt.json", "canonical-binding.json"):
        path = batch_runtime / name
        if path.exists():
            result[name] = _read_json(path)
    events = batch_runtime / "run-events.jsonl"
    if events.exists():
        lines = events.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
        result["recent_run_events"] = [redact_secrets(line)[:1500] for line in lines]
    return result


def run_autonomous_project(
    descriptor_path: Path,
    workspace: Path,
    runtime_dir: Path,
    routing_policy_path: Path,
    *,
    goal: str = DEFAULT_GOAL,
    constraints: Sequence[str] = (),
    red_lines: Sequence[str] = DEFAULT_RED_LINES,
    max_batches: int = 12,
    max_iterations_per_batch: int = 30,
    backend_override: Optional[Any] = None,
    situation_factory: Optional[Callable[..., ProjectSituation]] = None,
    batch_executor: Optional[Callable[..., Mapping[str, Any]]] = None,
    strategy_generation: int = 0,
    recovery_failure_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _hydrate_credentials()
    checkpoint = _read_json(_kernel_checkpoint_path(runtime_dir))
    checkpoint_strategy_generation = int(checkpoint.get("strategy_generation", 0) or 0)
    strategy_changed = strategy_generation > checkpoint_strategy_generation
    if strategy_changed:
        checkpoint = {
            **checkpoint,
            "strategy_generation": strategy_generation,
            "recovery_failure_context": dict(recovery_failure_context or {}),
        }
        _write_kernel_checkpoint(runtime_dir, checkpoint)
    synth = situation_factory or synthesize_project_situation

    # Reconstruct durable batch execution history from disk
    durable_history = reconstruct_batch_history(runtime_dir, workspace=workspace)

    # Reconstruct completed_batches window
    raw_completed_batches = checkpoint.get("completed_batches", [])
    if isinstance(raw_completed_batches, list) and raw_completed_batches:
        completed_batches = list(raw_completed_batches)
    else:
        completed_batches = list(durable_history.recent_completed_batches)

    # Calculate authoritative monotonic counts
    ckpt_completed_count = int(checkpoint.get("total_completed_batch_count", checkpoint.get("completed_batch_count", 0)) or 0)
    total_completed_batch_count = max(durable_history.total_executed_batches, ckpt_completed_count, len(completed_batches))

    ckpt_successful_count = int(checkpoint.get("successful_batch_count", 0) or 0)
    successful_batch_count = max(durable_history.successful_batches, ckpt_successful_count)

    ckpt_failed_count = int(checkpoint.get("failed_batch_count", 0) or 0)
    failed_batch_count = max(durable_history.failed_batches, ckpt_failed_count)

    # Authoritative batch_number monotonic progression
    raw_batch_num = checkpoint.get("batch_number")
    ckpt_batch_number = int(raw_batch_num) if raw_batch_num is not None else 0
    batch_number = max(durable_history.highest_batch_number + 1, ckpt_batch_number, len(completed_batches))

    replan_reason = checkpoint.get("replan_reason")
    if strategy_changed:
        family = str((recovery_failure_context or {}).get("failure_family") or "UNKNOWN")
        replan_reason = f"RECOVERY_STRATEGY_ESCALATION:{family}"

    # Maintain cumulative sets from durable history
    cumulative_completed_task_ids = set(durable_history.completed_task_ids)
    cumulative_completed_signatures = set(durable_history.completed_task_signatures)

    resumed_receipt: Optional[Dict[str, Any]] = None

    # Resume an interrupted batch before invoking new reasoning.
    if checkpoint.get("phase") == "EXECUTING" and checkpoint.get("active_plan_path"):
        plan_path = Path(checkpoint["active_plan_path"])
        batch_runtime = Path(checkpoint["active_batch_runtime"])
        if plan_path.is_file():
            if batch_executor is None:
                from aos.autonomous_host import run_host
                receipt = run_host(
                    descriptor_path=descriptor_path,
                    plan_path=plan_path,
                    workspace=workspace,
                    runtime_dir=batch_runtime,
                    routing_policy_path=routing_policy_path,
                    max_iterations=max_iterations_per_batch,
                )
            else:
                receipt = dict(batch_executor(plan_path=plan_path, batch_runtime=batch_runtime, resume=True))
            resumed_receipt = dict(receipt)
            completed_batches.append({"batch_number": batch_number, "receipt": resumed_receipt, "resumed": True})
            total_completed_batch_count += 1
            if resumed_receipt.get("failed_task_ids") or float(resumed_receipt.get("progress", 0.0)) < 100.0:
                failed_batch_count += 1
            else:
                successful_batch_count += 1

            for t_id in resumed_receipt.get("completed_task_ids", []):
                cumulative_completed_task_ids.add(str(t_id).strip())

            batch_number += 1
            _write_kernel_checkpoint(runtime_dir, {
                **checkpoint,
                "phase": "BATCH_COMPLETE",
                "batch_number": batch_number,
                "completed_batch_count": total_completed_batch_count,
                "total_completed_batch_count": total_completed_batch_count,
                "successful_batch_count": successful_batch_count,
                "failed_batch_count": failed_batch_count,
                "recent_completed_batches": list(completed_batches)[-30:],
                "completed_batches": list(completed_batches)[-30:],
                "last_receipt": dict(receipt),
                "active_plan_path": None,
                "active_batch_runtime": None,
            })
            replan_reason = "PROCESS_RESTART_RESUME"

    recent_receipt: Dict[str, Any] = resumed_receipt or (dict(checkpoint.get("last_receipt", {})) if isinstance(checkpoint.get("last_receipt"), dict) else {})

    for _ in range(max_batches):
        situation_path = runtime_dir / f"situation-{batch_number:04d}.json"
        prior_situation = _read_json(situation_path)
        situation = synth(
            descriptor_path=descriptor_path,
            workspace=workspace,
            goal=goal,
            constraints=constraints,
            red_lines=red_lines,
        )
        _atomic_json(situation_path, situation.to_dict())
        if situation.ambiguity_reasons:
            result = _final_result(
                situation, batch_number, completed_batches, "HUMAN_REQUIRED", "CANONICAL_CONTRADICTION",
                recent_receipt, runtime_dir,
                total_completed_batch_count=total_completed_batch_count,
                successful_batch_count=successful_batch_count,
                failed_batch_count=failed_batch_count,
            )
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
            return result

        if recent_receipt:
            completion = None if strategy_changed else _recover_waiting_completion(
                runtime_dir, batch_number, checkpoint, prior_situation, situation, recent_receipt,
            )
            if completion is None:
                try:
                    completion = detect_completion(
                        situation, routing_policy_path, runtime_dir, recent_receipt,
                        backend_override=backend_override,
                    )
                except WaitingForReasoningProvider as exc:
                    result = _final_result(
                        situation, batch_number, completed_batches, "WAITING_FOR_REASONING_PROVIDER", str(exc),
                        recent_receipt, runtime_dir,
                        total_completed_batch_count=total_completed_batch_count,
                        successful_batch_count=successful_batch_count,
                        failed_batch_count=failed_batch_count,
                    )
                    result["required_task_class"] = exc.task_class
                    result["quota_retry_after_epoch"] = exc.retry_after_epoch
                    result["quota_key"] = exc.quota_key
                    _write_kernel_checkpoint(runtime_dir, {**result, "phase": "WAITING_FOR_REASONING_PROVIDER"})
                    return result
                _save_replan_completion(
                    runtime_dir, batch_number, situation, recent_receipt, completion,
                )
            if completion["disposition"] == "PROJECT_COMPLETE":
                result = _final_result(
                    situation, batch_number, completed_batches, "PROJECT_COMPLETE", completion.get("rationale", ""),
                    recent_receipt, runtime_dir,
                    total_completed_batch_count=total_completed_batch_count,
                    successful_batch_count=successful_batch_count,
                    failed_batch_count=failed_batch_count,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "PROJECT_COMPLETE"})
                return result
            if completion["disposition"] == "HUMAN_REQUIRED":
                result = _final_result(
                    situation, batch_number, completed_batches, "HUMAN_REQUIRED", completion.get("rationale", ""),
                    recent_receipt, runtime_dir,
                    total_completed_batch_count=total_completed_batch_count,
                    successful_batch_count=successful_batch_count,
                    failed_batch_count=failed_batch_count,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
                return result
            replan_reason = completion.get("rationale") or replan_reason or "AUTHORIZED_WORK_REMAINS"

        try:
            # If stagnated, do not recover prior waiting objective; select a fresh objective with stagnation context
            if strategy_changed or str(replan_reason or "").startswith("STAGNATION_NO_FORWARD_PROGRESS"):
                objective = None
            else:
                objective = _recover_waiting_objective(
                    runtime_dir, batch_number, checkpoint, prior_situation, situation,
                )
            if objective is None:
                objective = select_objective(
                    situation, routing_policy_path, runtime_dir,
                    backend_override=backend_override, replan_reason=replan_reason,
                )
            _atomic_json(runtime_dir / f"objective-{batch_number:04d}.json", dataclasses.asdict(objective))

            session_completed_task_ids = {
                str(task_id)
                for completed in completed_batches
                if isinstance(completed, Mapping)
                for receipt in [completed.get("receipt", {})]
                if isinstance(receipt, Mapping)
                for task_id in receipt.get("completed_task_ids", [])
            }
            all_completed_task_ids = sorted(cumulative_completed_task_ids | session_completed_task_ids)

            current_workspace_generation = build_workspace_source_generation(
                project_id=situation.project_id,
                canonical_source_sha=situation.control_sha,
                canonical_execution_base_sha=situation.execution_base_sha,
            )

            read_history_batches = list(durable_history.durable_batches)
            read_history_batches.extend(completed_batches)

            completed_read_context = _bounded_completed_read_context(
                runtime_dir,
                workspace,
                read_history_batches,
                workspace_source_generation=current_workspace_generation,
            )
            all_completed_read_identities = sorted(
                set(completed_read_context["completed_read_identities"])
            )

            session_signatures = _completed_task_signatures(
                runtime_dir, completed_batches,
            )
            all_completed_signatures = sorted(cumulative_completed_signatures | set(session_signatures))

            repair_context = {
                "replan_reason": replan_reason,
                "recent_receipt": {
                    "completed_task_ids": list(recent_receipt.get("completed_task_ids", [])),
                    "failed_task_ids": list(recent_receipt.get("failed_task_ids", [])),
                    "progress": recent_receipt.get("progress"),
                },
                "completed_read_context": completed_read_context,
                "recovery_failure_context": dict(recovery_failure_context or {}),
                "strategy_generation": strategy_generation,
            } if replan_reason or recent_receipt else {}
            plan = compile_execution_plan(
                situation, objective, routing_policy_path, runtime_dir,
                backend_override=backend_override,
                repair_context=repair_context,
                forbidden_task_ids=all_completed_task_ids,
                forbidden_read_paths=all_completed_read_identities,
                forbidden_task_signatures=all_completed_signatures,
                workspace=workspace,
                workspace_source_generation=current_workspace_generation,
                allow_recovered_repair=not strategy_changed,
                batch_number=batch_number,
            )
        except WaitingForReasoningProvider as exc:
            result = _final_result(
                situation, batch_number, completed_batches, "WAITING_FOR_REASONING_PROVIDER", str(exc),
                recent_receipt, runtime_dir,
                total_completed_batch_count=total_completed_batch_count,
                successful_batch_count=successful_batch_count,
                failed_batch_count=failed_batch_count,
            )
            result["required_task_class"] = exc.task_class
            result["quota_retry_after_epoch"] = exc.retry_after_epoch
            result["quota_key"] = exc.quota_key
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "WAITING_FOR_REASONING_PROVIDER"})
            return result
        except PlannerValidationExhausted as exc:
            # A provider successfully returned structured output, but that plan
            # failed local validation. This is not provider unavailability and
            # must not open a provider-wait/respawn loop. End the bounded cycle
            # with explicit replan context; a continuous worker fresh-selects an
            # objective on its next cycle while preserving command lineage.
            result = _final_result(
                situation, batch_number, completed_batches, "BOUNDED_RUN_EXHAUSTED", str(exc),
                recent_receipt, runtime_dir,
                total_completed_batch_count=total_completed_batch_count,
                successful_batch_count=successful_batch_count,
                failed_batch_count=failed_batch_count,
            )
            result["replan_reason"] = str(exc)
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "BOUNDED_RUN_EXHAUSTED"})
            return result
        except (HumanRequired, AuthorityDenied, CanonicalDrift) as exc:
            result = _final_result(
                situation, batch_number, completed_batches, "HUMAN_REQUIRED", str(exc),
                recent_receipt, runtime_dir,
                total_completed_batch_count=total_completed_batch_count,
                successful_batch_count=successful_batch_count,
                failed_batch_count=failed_batch_count,
            )
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
            return result

        batch_runtime = runtime_dir / "batches" / f"batch-{batch_number:04d}"
        plan_path = batch_runtime / "generated-run-plan.json"
        _atomic_json(plan_path, plan)
        _write_kernel_checkpoint(runtime_dir, {
            "project_id": situation.project_id,
            "goal": goal,
            "phase": "EXECUTING",
            "batch_number": batch_number,
            "situation_id": situation.identity(),
            "canonical_source_sha": situation.control_sha,
            "canonical_execution_base_sha": situation.execution_base_sha,
            "objective": dataclasses.asdict(objective),
            "active_plan_path": str(plan_path),
            "active_batch_runtime": str(batch_runtime),
            "completed_batch_count": total_completed_batch_count,
            "total_completed_batch_count": total_completed_batch_count,
            "successful_batch_count": successful_batch_count,
            "failed_batch_count": failed_batch_count,
            "recent_completed_batches": list(completed_batches)[-30:],
            "completed_batches": list(completed_batches)[-30:],
            "replan_reason": replan_reason,
            "ag_invocation_count": 0,
            "production": "NO_GO",
        })
        if batch_executor is None:
            from aos.autonomous_host import run_host
            receipt = run_host(
                descriptor_path=descriptor_path,
                plan_path=plan_path,
                workspace=workspace,
                runtime_dir=batch_runtime,
                routing_policy_path=routing_policy_path,
                max_iterations=max_iterations_per_batch,
            )
        else:
            receipt = dict(batch_executor(plan_path=plan_path, batch_runtime=batch_runtime, resume=False))
        recent_receipt = dict(receipt)
        if plan.get("design_intelligence_execution_id"):
            recent_receipt["design_intelligence_execution_id"] = plan["design_intelligence_execution_id"]
            if plan.get("design_intelligence_evidence"):
                recent_receipt["design_intelligence_evidence"] = plan["design_intelligence_evidence"]
        completed_batches.append({
            "batch_number": batch_number,
            "objective_id": objective.objective_id,
            "canonical_source_sha": situation.control_sha,
            "receipt": recent_receipt,
            "resumed": False,
        })
        total_completed_batch_count += 1
        for t_id in recent_receipt.get("completed_task_ids", []):
            cumulative_completed_task_ids.add(str(t_id).strip())

        # Post-Implementation Visual Stage (R13-R17) for UI Planning Lineage
        di_post_blockers: List[str] = []
        if _objective_task_class(objective) == TaskClass.REPO_UI_PLANNING.value and workspace is not None:
            try:
                from extensions.design_intelligence.contracts import DesignProjectBrief
                from extensions.design_intelligence.design_loop import AutonomousDesignLoopPipeline
                from extensions.design_intelligence.browser_capture import RealBrowserCaptureAdapter
                from extensions.design_intelligence.critics import DesignCriticEnsemble, VisualCriticAdapter

                ws_root = workspace.resolve()
                index_html_path = ws_root / "index.html"
                if index_html_path.is_file():
                    real_html = index_html_path.read_text(encoding="utf-8", errors="replace")
                    index_css_path = ws_root / "index.css"
                    real_css = index_css_path.read_text(encoding="utf-8", errors="replace") if index_css_path.is_file() else ""

                    evidence_dir = runtime_dir / "screenshots" / f"batch-{int(batch_number or 0):04d}-post"
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    capture_adapter = RealBrowserCaptureAdapter(output_dir=str(evidence_dir))
                    visual_manifest = capture_adapter.capture_manifest(str(index_html_path), f"post-{int(batch_number or 0):04d}")

                    brief = DesignProjectBrief(
                        brief_id=f"brief-{situation.project_id}",
                        project_id=situation.project_id,
                        tenant_name=situation.project_id,
                        industry="Software & Healthcare",
                        target_audience="Clinical & Operations Users",
                        core_job_to_be_done=objective.title,
                        brand_posture="Clinical Precision",
                    )
                    from extensions.design_intelligence.visual_provider import RealVisualCriticAdapter

                    visual_adapter = None
                    if os.environ.get("GEMINI_API_KEY", "").strip():
                        visual_adapter = RealVisualCriticAdapter()
                    critic_ensemble = DesignCriticEnsemble(visual_adapter=visual_adapter)
                    pipeline = AutonomousDesignLoopPipeline(critic_ensemble=critic_ensemble, max_design_review_cycles=2)
                    di_res = pipeline.run_pipeline(
                        brief=brief,
                        initial_html=real_html,
                        initial_css=real_css,
                        evidence_manifest=visual_manifest,
                    )
                    passed_critics = [f.critic_name for f in di_res.final_scorecard.critic_findings if f.verdict.value == "PASS"]
                    failing_findings = [f"{f.critic_name}: {f.details}" for f in di_res.final_scorecard.critic_findings if f.verdict.value == "FAIL"]
                    if di_res.overall_verdict.value != "PASS":
                        di_post_blockers = list(failing_findings)
                    if di_res.blockers:
                        di_post_blockers.extend(di_res.blockers)

                    di_post_evidence = {
                        "schema_version": "1.0.0",
                        "design_intelligence_execution_id": di_res.pipeline_id,
                        "stage": "POST_IMPLEMENTATION",
                        "project_id": situation.project_id,
                        "batch_number": batch_number,
                        "overall_verdict": di_res.overall_verdict.value,
                        "cycles_completed": di_res.cycles_completed,
                        "human_review_state": di_res.human_review_state.value,
                        "critics_passed": passed_critics,
                        "failing_findings": failing_findings,
                        "blockers": di_post_blockers,
                        "visual_manifest_id": visual_manifest.manifest_id,
                        "viewports_captured": visual_manifest.viewports_captured,
                        "file_hashes": visual_manifest.file_hashes,
                    }
                    di_post_artifact_path = runtime_dir / f"design-intelligence-post-{int(batch_number or 0):04d}.json"
                    _atomic_json(di_post_artifact_path, di_post_evidence)
                    recent_receipt["design_intelligence_post_evidence"] = di_post_evidence
            except Exception as exc:
                di_post_blockers.append(f"DESIGN_POST_STAGE_FAILURE: {str(exc)}")

        consecutive_failures = int(checkpoint.get("consecutive_failures", 0))
        if recent_receipt.get("failed_task_ids") or float(recent_receipt.get("progress", 0.0)) < 100.0 or di_post_blockers:
            failed_batch_count += 1
            if di_post_blockers and not recent_receipt.get("failed_task_ids"):
                failure_class = "DESIGN_REMEDIATION_REQUIRED"
            else:
                failure_class = classify_batch_failure(recent_receipt, batch_runtime)
            if failure_class in ("AUTHORITY_FAILURE", "SECURITY_FAILURE", "SCHEMA_CONTRACT_FAILURE", "CANONICAL_DRIFT"):
                result = _final_result(
                    situation, batch_number, completed_batches, "HUMAN_REQUIRED", failure_class,
                    recent_receipt, runtime_dir,
                    total_completed_batch_count=total_completed_batch_count,
                    successful_batch_count=successful_batch_count,
                    failed_batch_count=failed_batch_count,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
                return result
            consecutive_failures += 1
            if consecutive_failures >= 3:
                replan_reason = f"STAGNATION_NO_FORWARD_PROGRESS:{failure_class}"
            else:
                replan_reason = failure_class
        else:
            successful_batch_count += 1
            consecutive_failures = 0
            replan_reason = "MEANINGFUL_BATCH_COMPLETE_FRESH_READ_REQUIRED"
        batch_number += 1
        _write_kernel_checkpoint(runtime_dir, {
            "project_id": situation.project_id,
            "goal": goal,
            "phase": "BATCH_COMPLETE",
            "batch_number": batch_number,
            "completed_batch_count": total_completed_batch_count,
            "total_completed_batch_count": total_completed_batch_count,
            "successful_batch_count": successful_batch_count,
            "failed_batch_count": failed_batch_count,
            "recent_completed_batches": list(completed_batches)[-30:],
            "completed_batches": list(completed_batches)[-30:],
            "last_receipt": recent_receipt,
            "replan_reason": replan_reason,
            "consecutive_failures": consecutive_failures,
            "ag_invocation_count": 0,
            "production": "NO_GO",
        })

    situation = synth(
        descriptor_path=descriptor_path,
        workspace=workspace,
        goal=goal,
        constraints=constraints,
        red_lines=red_lines,
    )
    result = _final_result(
        situation, batch_number, completed_batches, "BOUNDED_RUN_EXHAUSTED",
        "Batch bound reached; resume from durable planning checkpoint without user task injection.",
        recent_receipt, runtime_dir,
        total_completed_batch_count=total_completed_batch_count,
        successful_batch_count=successful_batch_count,
        failed_batch_count=failed_batch_count,
    )
    _write_kernel_checkpoint(runtime_dir, {**result, "phase": "BOUNDED_RUN_EXHAUSTED"})
    return result


def _final_result(
    situation: ProjectSituation,
    batch_number: int,
    completed_batches: Sequence[Mapping[str, Any]],
    disposition: str,
    reason: str,
    recent_receipt: Mapping[str, Any],
    runtime_dir: Path,
    *,
    total_completed_batch_count: Optional[int] = None,
    successful_batch_count: Optional[int] = None,
    failed_batch_count: Optional[int] = None,
) -> Dict[str, Any]:
    total_count = total_completed_batch_count if total_completed_batch_count is not None else len(completed_batches)
    recent = list(completed_batches)[-30:]
    result = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": _utc_now(),
        "project_id": situation.project_id,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "batch_number": batch_number,
        "completed_batch_count": total_count,
        "total_completed_batch_count": total_count,
        "successful_batch_count": successful_batch_count if successful_batch_count is not None else total_count,
        "failed_batch_count": failed_batch_count if failed_batch_count is not None else 0,
        "recent_completed_batches": recent,
        "completed_batches": recent,
        "last_receipt": dict(recent_receipt),
        "disposition": disposition,
        "reason": redact_secrets(reason)[:2000],
        "run_plan_required_for_normal_mode": False,
        "ag_backend_enabled": False,
        "ag_invocation_count": 0,
        "production": "NO_GO",
    }
    _atomic_json(runtime_dir / "autonomous-project-result.json", result)
    return result
