"""AOS Autonomous Host V1.

A first-class, restartable, AG-independent host around the accepted Native
Execution Fabric. The host fresh-binds canonical project control state, restores
an exact run plan into a durable DAG, performs model-provider invocation failover,
dispatches native workers, and persists checkpoint / provider-attempt evidence.

Production activation is intentionally out of scope. Antigravity is disabled by
default and can only be enabled explicitly by a future separately-authorized host
profile.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.provider_registry import ProviderRegistry, ProviderRouter, load_routing_policy
from aos.providers import GeminiPlannerProvider, GroqPlannerProvider, NemotronPlannerProvider, OllamaPlannerProvider
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_file


def _ensure_repo_extensions_importable() -> None:
    """Make the repository-shipped extensions package importable.

    Stable-runtime packaging includes the extensions package plus the hyphenated
    implementation directories as package data. Source checkouts are also
    supported directly.
    """
    candidates: List[Path] = []
    aos_home = os.environ.get("AOS_HOME")
    if aos_home:
        candidates.append(Path(aos_home))
    candidates.extend([Path(__file__).resolve().parents[2], Path.cwd()])
    for candidate in candidates:
        if (candidate / "extensions" / "__init__.py").exists():
            resolved = str(candidate.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


_ensure_repo_extensions_importable()

from extensions.autonomy_fabric.execution_backend import (  # noqa: E402
    EvidenceClass,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter  # noqa: E402
from extensions.autonomy_fabric.native_workers import (  # noqa: E402
    BrowserExecutionBackend,
    GitHubCIWorker,
    NativeFileWorker,
    NativeGitWorker,
    NativeProcessWorker,
    redact_secrets,
)
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator  # noqa: E402
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, FileRunJournal  # noqa: E402
from extensions.autonomy_fabric.task_dag import NodeGateType, TaskDAG  # noqa: E402


class ProviderAttemptStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NON_RETRYABLE_FAILED = "NON_RETRYABLE_FAILED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"


@dataclass
class ProviderAttempt:
    provider_id: str
    model_id: str
    status: ProviderAttemptStatus
    error_class: Optional[str] = None
    message: Optional[str] = None
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": self.status.value,
            "error_class": self.error_class,
            "message": self.message,
            "timestamp": self.timestamp,
        }


_PROVIDER_FACTORIES: Dict[str, Callable[[str], Any]] = {
    "nemotron": lambda model: NemotronPlannerProvider(model=model),
    "gemini": lambda model: GeminiPlannerProvider(model=model),
    "groq": lambda model: GroqPlannerProvider(model=model),
    "ollama": lambda model: OllamaPlannerProvider(model=model),
}


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: Optional[Path], payload: Dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _bounded_error_message(exc: Exception) -> str:
    # Keep provider attempts useful without persisting raw secret-bearing text.
    return redact_secrets(str(exc))[:500]


class ProviderFailoverReasoningBackend(ExecutionBackend):
    """Model reasoning backend with real post-invocation provider failover.

    Failover is allowed only for transient / unavailable / credential / quota /
    timeout classes. Contract/schema/security failures fail closed and are never
    routed around.
    """

    backend_id = "provider_failover_reasoning_backend"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.MODEL_REASONING}
    cost = ExecutionCost.FREE_TIER_CLOUD

    def __init__(
        self,
        provider_router: ProviderRouter,
        provider_factory: Optional[Callable[[str, str], Any]] = None,
        attempt_journal: Optional[Path] = None,
    ) -> None:
        self.provider_router = provider_router
        self.provider_factory = provider_factory or self._default_provider_factory
        self.attempt_journal = attempt_journal

    @staticmethod
    def _default_provider_factory(provider_id: str, model_id: str) -> Any:
        factory = _PROVIDER_FACTORIES.get(provider_id)
        if factory is None:
            raise PlannerContractError(f"No executable provider adapter registered for '{provider_id}'")
        return factory(model_id)

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def _record_attempt(self, attempt: ProviderAttempt) -> None:
        _append_jsonl(self.attempt_journal, attempt.to_dict())

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        prompt = request.payload.get("prompt", "")
        schema = request.payload.get("schema", {})
        risk_class = request.payload.get("risk_class", "R0")
        ignore_credentials = bool(request.payload.get("ignore_credentials", False))
        tried: List[str] = []
        attempts: List[ProviderAttempt] = []
        failed_provider: Optional[str] = None

        provider_limit = max(1, len(self.provider_router.registry.list_providers()))
        for _ in range(provider_limit):
            route = self.provider_router.select(
                risk_class=risk_class,
                skip_providers=tried,
                ignore_credentials=ignore_credentials,
                post_invocation_failed_provider=failed_provider,
            )
            if route is None:
                break

            provider_id = route.selected_provider_id
            model_id = route.selected_model_id
            tried.append(provider_id)

            try:
                provider = self.provider_factory(provider_id, model_id)
                plan_data, response_id, usage = provider.generate_plan(prompt, schema)
                if not isinstance(plan_data, dict):
                    raise PlannerContractError("Provider response is not a structured object")

                provenance = getattr(provider, "execution_provenance", "LOCAL_OFFLINE")
                evidence_class = (
                    EvidenceClass.LIVE_EXTERNAL_PROOF
                    if provenance == "LIVE_EXTERNAL"
                    else EvidenceClass.LOCAL_RUNTIME_PROOF
                )
                attempt = ProviderAttempt(
                    provider_id=provider_id,
                    model_id=model_id,
                    status=ProviderAttemptStatus.SUCCESS,
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                attempts.append(attempt)
                self._record_attempt(attempt)
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="SUCCESS",
                    exit_code=0,
                    workspace=request.workspace,
                    stdout_digest=f"Structured reasoning succeeded via {provider_id} ({model_id})",
                    evidence_payload={
                        "proposal": plan_data,
                        "provider_route": provider_id,
                        "model_id": model_id,
                        "response_id": response_id,
                        "usage": usage,
                        "provider_attempts": [item.to_dict() for item in attempts],
                        "fallback_used": len(attempts) > 1,
                    },
                    evidence_class=evidence_class,
                )
            except PlannerContractError as exc:
                attempt = ProviderAttempt(
                    provider_id=provider_id,
                    model_id=model_id,
                    status=ProviderAttemptStatus.NON_RETRYABLE_FAILED,
                    error_class=exc.__class__.__name__,
                    message=_bounded_error_message(exc),
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                attempts.append(attempt)
                self._record_attempt(attempt)
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    sanitized_errors=["PROVIDER_CONTRACT_FAILURE"],
                    evidence_payload={"provider_attempts": [item.to_dict() for item in attempts]},
                    evidence_class=EvidenceClass.SOURCE_PROOF,
                )
            except PlannerCredentialError as exc:
                status = ProviderAttemptStatus.UNAVAILABLE
                error_class = exc.__class__.__name__
                message = _bounded_error_message(exc)
            except PlannerTransientError as exc:
                raw = str(exc).upper()
                status = (
                    ProviderAttemptStatus.QUOTA_EXHAUSTED
                    if "RATE_LIMIT" in raw or "429" in raw or "QUOTA" in raw
                    else ProviderAttemptStatus.RETRYABLE_FAILED
                )
                error_class = exc.__class__.__name__
                message = _bounded_error_message(exc)
            except TimeoutError as exc:
                status = ProviderAttemptStatus.TIMED_OUT
                error_class = exc.__class__.__name__
                message = _bounded_error_message(exc)
            except (ConnectionError, OSError) as exc:
                status = ProviderAttemptStatus.UNAVAILABLE
                error_class = exc.__class__.__name__
                message = _bounded_error_message(exc)
            except Exception as exc:
                # Unknown errors are not safe to route around.
                attempt = ProviderAttempt(
                    provider_id=provider_id,
                    model_id=model_id,
                    status=ProviderAttemptStatus.NON_RETRYABLE_FAILED,
                    error_class=exc.__class__.__name__,
                    message=_bounded_error_message(exc),
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                attempts.append(attempt)
                self._record_attempt(attempt)
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    sanitized_errors=["UNKNOWN_PROVIDER_FAILURE_FAIL_CLOSED"],
                    evidence_payload={"provider_attempts": [item.to_dict() for item in attempts]},
                    evidence_class=EvidenceClass.SOURCE_PROOF,
                )

            attempt = ProviderAttempt(
                provider_id=provider_id,
                model_id=model_id,
                status=status,
                error_class=error_class,
                message=message,
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            attempts.append(attempt)
            self._record_attempt(attempt)
            failed_provider = provider_id

        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="model_reasoner",
            task_id=request.task_id,
            request_id=request.request_id,
            status="DEGRADED",
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=["ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"],
            evidence_payload={
                "failure_class": "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE",
                "provider_attempts": [item.to_dict() for item in attempts],
                "local_reasoning_result": (
                    "LOCAL_REASONING_UNAVAILABLE"
                    if any(a.provider_id == "ollama" for a in attempts)
                    and not any(a.provider_id == "ollama" and a.status == ProviderAttemptStatus.SUCCESS for a in attempts)
                    else "NOT_SELECTED_OR_NOT_REQUIRED"
                ),
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )


def probe_ollama_models(base_url: str = "http://localhost:11434", timeout: float = 2.0) -> Dict[str, Any]:
    """Read-only local Ollama probe. Never downloads a model."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [m.get("name") for m in payload.get("models", []) if isinstance(m, dict) and m.get("name")]
        return {"available": True, "models": models}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {"available": False, "models": []}


def refresh_canonical_binding(descriptor_path: Path, binding_path: Path) -> Dict[str, Any]:
    validation, _ = validate_file("project_descriptor", str(descriptor_path))
    if not validation.is_valid:
        raise ValueError(f"Invalid project descriptor: {[str(e) for e in validation.errors]}")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    adapter = ProjectSourceAdapter(descriptor["repository"], descriptor["control_ref"])
    source_sha = adapter.resolve_ref_to_sha()
    contents, hashes = adapter.fetch_canonical_context(source_sha, descriptor["control"])
    snapshot = adapter.build_normalized_snapshot(
        descriptor["project_id"], source_sha, contents, hashes, descriptor.get("projection")
    )
    if snapshot.get("has_ambiguity"):
        raise RuntimeError(f"Canonical source ambiguity: {snapshot.get('ambiguity_reasons', [])}")

    execution_base_sha = snapshot.get("next_action_execution_base_sha")
    if execution_base_sha:
        adapter.resolve_exact_revision(execution_base_sha)

    binding = {
        "schema_version": "1.0.0",
        "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": descriptor["project_id"],
        "repository": descriptor["repository"],
        "source_ref": descriptor["control_ref"],
        "source_sha": source_sha,
        "input_file_hashes": hashes,
        "current_status": snapshot.get("current_status"),
        "current_milestone": snapshot.get("current_milestone"),
        "canonical_next_action": snapshot.get("canonical_next_action"),
        "target_base_sha": snapshot.get("target_base_sha"),
        "execution_base_sha": execution_base_sha,
    }
    _atomic_json_write(binding_path, binding)
    return binding


def load_bound_run_plan(
    plan_path: Path,
    project_id: str,
    source_sha: str,
    execution_base_sha: Optional[str] = None,
) -> Dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != "1.0.0":
        raise ValueError("Autonomous host run plan schema_version must be 1.0.0")
    if plan.get("project_id") != project_id:
        raise ValueError("Run plan project_id does not match project descriptor")
    if plan.get("bound_source_sha") != source_sha:
        raise ValueError("Run plan is stale: bound_source_sha does not match fresh canonical source")
    if execution_base_sha:
        if plan.get("bound_execution_base_sha") != execution_base_sha:
            raise ValueError(
                "Run plan is stale: bound_execution_base_sha does not match fresh canonical execution base"
            )
    elif plan.get("bound_execution_base_sha"):
        raise ValueError(
            "Run plan declares bound_execution_base_sha but canonical source exposes no execution base"
        )
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Run plan must contain at least one task")
    return plan


def build_dag(project_id: str, registry: AgentRunRegistry, plan: Dict[str, Any]) -> TaskDAG:
    dag = TaskDAG(project_id, registry)
    for item in plan["tasks"]:
        authority_id = item.get("authority_id")
        if not authority_id or authority_id == "NONE":
            raise ValueError(f"Task {item.get('node_id')} is missing live bounded authority")
        gate_name = item.get("gate_type", "NONE")
        try:
            gate_type = NodeGateType(gate_name)
        except ValueError as exc:
            raise ValueError(f"Unsupported gate_type '{gate_name}'") from exc
        node = dag.add_node(
            node_id=item["node_id"],
            run_type=item["run_type"],
            authority_id=authority_id,
            dependencies=item.get("dependencies", []),
            gate_type=gate_type,
        )
        # PersistentCoordinator intentionally treats payload as an extensible node field.
        node.payload = item.get("payload", {})
    return dag


def build_execution_router(policy_path: Path, runtime_dir: Path) -> ExecutionRouter:
    registry = load_routing_policy(str(policy_path))
    provider_router = ProviderRouter(registry)
    reasoning_backend = ProviderFailoverReasoningBackend(
        provider_router=provider_router,
        attempt_journal=runtime_dir / "provider-attempts.jsonl",
    )
    # Antigravity is deliberately absent. Host V1 proves Zero-AG continuity.
    return ExecutionRouter(
        backends=[
            NativeFileWorker(),
            NativeProcessWorker(),
            NativeGitWorker(),
            GitHubCIWorker(),
            BrowserExecutionBackend(),
            reasoning_backend,
        ],
        ag_required=False,
    )


def run_host(
    descriptor_path: Path,
    plan_path: Path,
    workspace: Path,
    runtime_dir: Path,
    routing_policy_path: Path,
    max_iterations: int = 20,
) -> Dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    canonical_binding = refresh_canonical_binding(
        descriptor_path, runtime_dir / "canonical-binding.json"
    )
    plan = load_bound_run_plan(
        plan_path,
        canonical_binding["project_id"],
        canonical_binding["source_sha"],
        canonical_binding.get("execution_base_sha"),
    )

    run_journal = FileRunJournal(str(runtime_dir / "run-events.jsonl"))
    registry = AgentRunRegistry(run_journal)
    dag = build_dag(canonical_binding["project_id"], registry, plan)
    router = build_execution_router(routing_policy_path, runtime_dir)
    coordinator = PersistentCoordinator(
        project_id=canonical_binding["project_id"],
        workspace_path=str(workspace),
        dag=dag,
        router=router,
        registry=registry,
        checkpoint_file=str(runtime_dir / "coordinator-checkpoint.json"),
    )
    state = coordinator.run_until_complete(max_iterations=max_iterations)
    receipt = {
        "schema_version": "1.0.0",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": canonical_binding["project_id"],
        "canonical_source_sha": canonical_binding["source_sha"],
        "canonical_execution_base_sha": canonical_binding.get("execution_base_sha"),
        "completed_task_ids": state.completed_task_ids,
        "failed_task_ids": state.failed_task_ids,
        "iteration_count": state.iteration_count,
        "progress": dag.compute_progress(),
        "ag_backend_enabled": False,
        "ag_invocation_count": 0,
        "production": "NO_GO",
        "ollama_probe": probe_ollama_models(),
    }
    _atomic_json_write(runtime_dir / "host-receipt.json", receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Autonomous Host V1 (Zero-AG, non-production)")
    parser.add_argument("--project", required=True, help="Project descriptor JSON")
    parser.add_argument(
        "--run-plan",
        help="Optional exact-source-bound manual run plan (debug/test/replay override only)",
    )
    parser.add_argument(
        "--goal",
        default="Continue this project to completion under standing authority.",
        help="Natural-language autonomous project goal used when --run-plan is omitted",
    )
    parser.add_argument(
        "--constraints-json",
        default="[]",
        help="JSON array of project constraints for autonomous mode",
    )
    parser.add_argument(
        "--red-lines-json",
        default="[]",
        help="JSON array of additional red lines for autonomous mode",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Authorized project workspace (defaults to current directory)",
    )
    parser.add_argument(
        "--runtime-dir",
        help="Durable AOS host state directory (defaults inside workspace)",
    )
    parser.add_argument("--max-batches", type=int, default=12)
    parser.add_argument(
        "--routing-policy",
        default="descriptors/nemotron.planner-policy.json",
        help="Provider routing policy JSON",
    )
    parser.add_argument("--max-iterations", type=int, default=20)
    return parser



def hydrate_local_reasoning_credentials() -> Dict[str, bool]:
    # Hydrate provider secrets from the OS vault into this process only.
    # Secret values are not returned, logged, serialized, or written to runtime state.
    try:
        from aos.secure_store import hydrate_environment
    except Exception:
        return {}
    try:
        return hydrate_environment(overwrite=False)
    except Exception:
        return {}

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    hydrate_local_reasoning_credentials()
    try:
        descriptor_path = Path(args.project).resolve()
        workspace = Path(args.workspace).resolve()
        runtime_dir = (
            Path(args.runtime_dir).resolve()
            if args.runtime_dir
            else (workspace / ".aos-runtime" / "autonomous-project").resolve()
        )
        routing_policy_path = Path(args.routing_policy).resolve()
        if args.run_plan:
            receipt = run_host(
                descriptor_path=descriptor_path,
                plan_path=Path(args.run_plan).resolve(),
                workspace=workspace,
                runtime_dir=runtime_dir,
                routing_policy_path=routing_policy_path,
                max_iterations=args.max_iterations,
            )
        else:
            from aos.planning_kernel import DEFAULT_RED_LINES, run_autonomous_project
            constraints = json.loads(args.constraints_json)
            extra_red_lines = json.loads(args.red_lines_json)
            if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
                raise ValueError("--constraints-json must be a JSON array of strings")
            if not isinstance(extra_red_lines, list) or not all(isinstance(x, str) for x in extra_red_lines):
                raise ValueError("--red-lines-json must be a JSON array of strings")
            receipt = run_autonomous_project(
                descriptor_path=descriptor_path,
                workspace=workspace,
                runtime_dir=runtime_dir,
                routing_policy_path=routing_policy_path,
                goal=args.goal,
                constraints=tuple(constraints),
                red_lines=tuple(DEFAULT_RED_LINES) + tuple(extra_red_lines),
                max_batches=args.max_batches,
                max_iterations_per_batch=args.max_iterations,
            )
    except Exception as exc:
        print(f"AOS_HOST_HOLD: {_bounded_error_message(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
