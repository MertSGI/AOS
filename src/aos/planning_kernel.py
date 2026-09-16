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
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from aos.provider_registry import ProviderRouter, load_routing_policy
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_file


def _ensure_repo_extensions_importable() -> None:
    import sys
    candidates = []
    aos_home = os.environ.get("AOS_HOME")
    if aos_home:
        candidates.append(Path(aos_home))
    candidates.extend([Path(__file__).resolve().parents[2], Path.cwd()])
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


class PlanningKernelError(RuntimeError):
    pass


class HumanRequired(PlanningKernelError):
    def __init__(self, reason: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


class WaitingForReasoningProvider(PlanningKernelError):
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
                    "payload": {"type": "object"},
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
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _run_readonly(cmd: Sequence[str], cwd: Path, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(cwd), shell=False, text=True, capture_output=True, timeout=timeout
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
    priority = sorted(
        contents,
        key=lambda p: (
            0 if any(token in p.upper() for token in ("STATE", "ROADMAP", "DECISION", "EVIDENCE", "RESUME", "CURRENT")) else 1,
            p,
        ),
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
                if current is None or len(candidate.text) > len(current.text):
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
                "safety": "shell=False; bounded timeout; clean environment",
            },
            "NativeGitWorker": {
                "run_type": "GIT",
                "payload": {"action": "git subcommand", "args": "argv array"},
                "prohibited_subcommands": sorted(NativeGitWorker.PROHIBITED_SUBCOMMANDS),
                "safety": "force push and prohibited subcommands are denied",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_prompt_excerpt(excerpt: str, max_chars: int = 8000) -> str:
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
        hydrate_environment(overwrite=False)
    except Exception:
        return


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
) -> Dict[str, Any]:
    _hydrate_credentials()
    if backend_override is None:
        from aos.autonomous_host import ProviderFailoverReasoningBackend
        backend = ProviderFailoverReasoningBackend(
            ProviderRouter(load_routing_policy(str(routing_policy_path))),
            attempt_journal=runtime_dir / "provider-attempts.jsonl",
        )
    else:
        backend = backend_override
    request = ExecutionRequest(
        task_id=task_id,
        project_id=situation.project_id,
        workspace=str(runtime_dir),
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id=authority_id,
        payload={"prompt": prompt, "schema": schema, "risk_class": "R0"},
    )
    result = backend.execute(request)
    if result.status != "SUCCESS":
        failure = str(result.evidence_payload.get("failure_class", result.status))
        if result.status in ("DEGRADED", "WAITING_FOR_REASONING_PROVIDER") or "UNAVAILABLE" in failure.upper():
            raise WaitingForReasoningProvider(failure)
        raise PlanningKernelError(f"Reasoning failed: {failure}")
    proposal = result.evidence_payload.get("proposal")
    if not isinstance(proposal, dict):
        raise PlanningKernelError("Reasoning backend returned no structured proposal")
    return proposal


def _situation_prompt_payload(situation: ProjectSituation) -> Dict[str, Any]:
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
        "canonical_excerpt": _bounded_prompt_excerpt(situation.canonical_excerpt),
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
        + json.dumps(_situation_prompt_payload(situation), ensure_ascii=False, sort_keys=True)
    )
    proposal = _reason(
        situation, routing_policy_path, runtime_dir, "objective-selection", prompt, OBJECTIVE_SCHEMA,
        authority_hint, backend_override=backend_override,
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
        task["dependencies"] = _string_list(task.get("dependencies", []), "dependencies", 64, 120)
        task["scope_tags"] = _string_list(task.get("scope_tags", []), "scope_tags", 32, 120)
        task["write_scope"] = _string_list(task.get("write_scope", []), "write_scope", 64, 500)
        if not isinstance(task.get("payload"), dict):
            raise PlanningKernelError(f"Task {node_id} payload must be object")
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
    repair_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    prompt = (
        "You are the AOS Planner->DAG compiler. Produce a bounded non-production execution plan for the selected objective. "
        "The plan is advisory until validated. Use only worker payload formats proven by the worker source below. "
        "Prefer small meaningful batches, explicit tests/evidence, safe parallelism, and rollback where relevant. "
        "Every task requires a canonical authority_id. Never emit production, force-push, history rewrite, destructive, secret, payment, "
        "legal/compliance, or material trust/security changes. Do not invent evidence. "
        "The top-level schema_version MUST be the exact string \"1.0.0\". Return exactly the requested JSON.\n\n"
        f"OBJECTIVE={json.dumps(dataclasses.asdict(objective), ensure_ascii=False, sort_keys=True)}\n"
        f"SITUATION={json.dumps(_situation_prompt_payload(situation), ensure_ascii=False, sort_keys=True)}\n"
        f"REPAIR_CONTEXT={json.dumps(dict(repair_context or {}), ensure_ascii=False, sort_keys=True)}\n"
        f"WORKER_CONTRACTS={_worker_contract_summary()}"
    )
    proposal = _reason(
        situation, routing_policy_path, runtime_dir, "plan-dag", prompt, PLAN_SCHEMA,
        objective.authority_id, backend_override=backend_override,
    )
    normalized = _normalize_plan_schema_envelope(proposal, objective, situation, runtime_dir)
    resolver = CanonicalAuthorityResolver(situation)
    for task in normalized["tasks"]:
        resolver.validate_task(task)
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
        f"SITUATION={json.dumps(_situation_prompt_payload(situation), ensure_ascii=False, sort_keys=True)}\n"
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
    return dict(proposal)


def _kernel_checkpoint_path(runtime_dir: Path) -> Path:
    return runtime_dir / "planning-kernel-checkpoint.json"


def _write_kernel_checkpoint(runtime_dir: Path, state: Mapping[str, Any]) -> None:
    payload = dict(state)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload["updated_at"] = _utc_now()
    _atomic_json(_kernel_checkpoint_path(runtime_dir), payload)


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
) -> Dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = _read_json(_kernel_checkpoint_path(runtime_dir))
    synth = situation_factory or synthesize_project_situation
    completed_batches = list(checkpoint.get("completed_batches", [])) if isinstance(checkpoint.get("completed_batches"), list) else []
    batch_number = int(checkpoint.get("batch_number", len(completed_batches)))
    replan_reason = checkpoint.get("replan_reason")

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
            _write_kernel_checkpoint(runtime_dir, {
                **checkpoint,
                "phase": "BATCH_COMPLETE",
                "completed_batches": completed_batches,
                "last_receipt": dict(receipt),
            })
            replan_reason = "PROCESS_RESTART_RESUME"

    recent_receipt: Dict[str, Any] = resumed_receipt or (dict(checkpoint.get("last_receipt", {})) if isinstance(checkpoint.get("last_receipt"), dict) else {})

    for _ in range(max_batches):
        situation = synth(
            descriptor_path=descriptor_path,
            workspace=workspace,
            goal=goal,
            constraints=constraints,
            red_lines=red_lines,
        )
        _atomic_json(runtime_dir / f"situation-{batch_number:04d}.json", situation.to_dict())
        if situation.ambiguity_reasons:
            result = _final_result(
                situation, batch_number, completed_batches, "HUMAN_REQUIRED", "CANONICAL_CONTRADICTION",
                recent_receipt, runtime_dir,
            )
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
            return result

        if recent_receipt:
            completion = detect_completion(
                situation, routing_policy_path, runtime_dir, recent_receipt,
                backend_override=backend_override,
            )
            if completion["disposition"] == "PROJECT_COMPLETE":
                result = _final_result(
                    situation, batch_number, completed_batches, "PROJECT_COMPLETE", completion.get("rationale", ""),
                    recent_receipt, runtime_dir,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "PROJECT_COMPLETE"})
                return result
            if completion["disposition"] == "HUMAN_REQUIRED":
                result = _final_result(
                    situation, batch_number, completed_batches, "HUMAN_REQUIRED", completion.get("rationale", ""),
                    recent_receipt, runtime_dir,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
                return result
            replan_reason = completion.get("rationale") or replan_reason or "AUTHORIZED_WORK_REMAINS"

        try:
            objective = select_objective(
                situation, routing_policy_path, runtime_dir,
                backend_override=backend_override, replan_reason=replan_reason,
            )
            _atomic_json(runtime_dir / f"objective-{batch_number:04d}.json", dataclasses.asdict(objective))
            repair_context = _bounded_runtime_evidence(Path(checkpoint.get("active_batch_runtime", runtime_dir))) if replan_reason else {}
            plan = compile_execution_plan(
                situation, objective, routing_policy_path, runtime_dir,
                backend_override=backend_override, repair_context=repair_context,
            )
        except WaitingForReasoningProvider as exc:
            result = _final_result(
                situation, batch_number, completed_batches, "WAITING_FOR_REASONING_PROVIDER", str(exc),
                recent_receipt, runtime_dir,
            )
            _write_kernel_checkpoint(runtime_dir, {**result, "phase": "WAITING_FOR_REASONING_PROVIDER"})
            return result
        except (HumanRequired, AuthorityDenied, CanonicalDrift) as exc:
            result = _final_result(
                situation, batch_number, completed_batches, "HUMAN_REQUIRED", str(exc),
                recent_receipt, runtime_dir,
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
            "completed_batches": completed_batches,
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
        completed_batches.append({
            "batch_number": batch_number,
            "objective_id": objective.objective_id,
            "canonical_source_sha": situation.control_sha,
            "receipt": recent_receipt,
            "resumed": False,
        })
        if recent_receipt.get("failed_task_ids") or float(recent_receipt.get("progress", 0.0)) < 100.0:
            failure_class = classify_batch_failure(recent_receipt, batch_runtime)
            if failure_class in ("AUTHORITY_FAILURE", "SECURITY_FAILURE", "SCHEMA_CONTRACT_FAILURE", "CANONICAL_DRIFT"):
                result = _final_result(
                    situation, batch_number, completed_batches, "HUMAN_REQUIRED", failure_class,
                    recent_receipt, runtime_dir,
                )
                _write_kernel_checkpoint(runtime_dir, {**result, "phase": "HUMAN_REQUIRED"})
                return result
            replan_reason = failure_class
        else:
            replan_reason = "MEANINGFUL_BATCH_COMPLETE_FRESH_READ_REQUIRED"
        batch_number += 1
        _write_kernel_checkpoint(runtime_dir, {
            "project_id": situation.project_id,
            "goal": goal,
            "phase": "BATCH_COMPLETE",
            "batch_number": batch_number,
            "completed_batches": completed_batches,
            "last_receipt": recent_receipt,
            "replan_reason": replan_reason,
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
) -> Dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": _utc_now(),
        "project_id": situation.project_id,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "batch_number": batch_number,
        "completed_batch_count": len(completed_batches),
        "completed_batches": list(completed_batches)[-30:],
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
