"""AOS Shadow Orchestrator CLI and execution module."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aos.planner import (
    FakePlannerProvider,
    OpenAIPlannerProvider,
    PlannerContractError,
    PlannerCredentialError,
    PlannerError,
    PlannerProvider,
    PlannerTransientError,
)
from aos.policy import PolicyEngine
from aos.provider_registry import (
    ProviderEntry,
    ProviderExecutionContext,
    ProviderRegistry,
    ProviderRouter,
    RoutingResult,
    load_routing_policy,
)
from aos.providers import GeminiPlannerProvider, GroqPlannerProvider, OllamaPlannerProvider
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import load_schema, validate_document, validate_file

RUNTIME_TRACE_DIR = Path(__file__).resolve().parent.parent.parent / ".aos-runtime" / "shadow"

PROVIDER_MAP = {
    "gemini": GeminiPlannerProvider,
    "groq": GroqPlannerProvider,
    "ollama": OllamaPlannerProvider,
    "openai": OpenAIPlannerProvider,
}


def run_shadow_orchestration(
    descriptor_path: str,
    expectation_path: Optional[str] = None,
    repeat: int = 1,
    provider_override: Optional[PlannerProvider] = None,
    execution_context_override: Optional[ProviderExecutionContext] = None,
    trace_dir_override: Optional[Path] = None,
    max_network_retries: int = 1,
    adapter_override: Optional[ProjectSourceAdapter] = None,
    routing_policy_path: Optional[str] = None,
    source_mode: str = "live_guard",
    mutation_intent: str = "NONE",
    risk_class: str = "R0",
) -> Tuple[str, List[Dict[str, Any]], int]:
    """Execute shadow orchestration loop with expectation contract check, provider retry, fail-closed trace and snapshot validation."""
    # Safety Check: PINNED_PROOF must NEVER be used for mutation
    if source_mode == "pinned_proof" and mutation_intent != "NONE":
        print(f"HOLD: PINNED_PROOF mode cannot be used with mutation_intent '{mutation_intent}'", file=sys.stderr)
        return "HOLD", [], 1

    # 1. Validate descriptor
    res, code = validate_file("project_descriptor", descriptor_path)
    if not res.is_valid:
        print(f"INVALID_CANONICAL_STATE: Project descriptor '{descriptor_path}' invalid:", file=sys.stderr)
        for e in res.errors:
            print(f"  - {e}", file=sys.stderr)
        return "INVALID_CANONICAL_STATE", [], 5

    with open(descriptor_path, "r", encoding="utf-8") as f:
        desc = json.load(f)

    project_id = desc["project_id"]
    repository = desc["repository"]
    control_ref = desc["control_ref"]

    adapter = adapter_override or ProjectSourceAdapter(repository=repository, control_ref=control_ref)

    # 2. Pin source SHA based on source_mode
    expectation_data = None
    if expectation_path:
        exp_res, exp_code = validate_file("shadow_expectation", expectation_path)
        if not exp_res.is_valid:
            print(f"INVALID_CANONICAL_STATE: Shadow expectation file '{expectation_path}' invalid:", file=sys.stderr)
            for e in exp_res.errors:
                print(f"  - {e}", file=sys.stderr)
            return "INVALID_CANONICAL_STATE", [], 5

        with open(expectation_path, "r", encoding="utf-8") as ef:
            expectation_data = json.load(ef)

    if source_mode == "pinned_proof":
        if not expectation_data:
            print(f"INVALID_CANONICAL_STATE: PINNED_PROOF mode requires a valid shadow expectation file", file=sys.stderr)
            return "INVALID_CANONICAL_STATE", [], 5
        pinned_sha = expectation_data["expected_source_sha"]
    else:
        # LIVE_GUARD mode
        try:
            pinned_sha = adapter.resolve_ref_to_sha()
        except Exception as e:
            print(f"PROVIDER_UNAVAILABLE: Failed to resolve ref '{control_ref}': {e}", file=sys.stderr)
            return "PROVIDER_UNAVAILABLE", [], 4

    # 3. Fetch canonical context
    control_files = desc["control"]
    try:
        raw_contents, file_hashes = adapter.fetch_canonical_context(pinned_sha, control_files)
    except Exception as e:
        print(f"PROVIDER_UNAVAILABLE: Failed to fetch canonical context: {e}", file=sys.stderr)
        return "PROVIDER_UNAVAILABLE", [], 4

    # 4. Build normalized canonical project snapshot and validate
    projection_cfg = desc.get("projection")
    try:
        snapshot = adapter.build_normalized_snapshot(project_id, pinned_sha, raw_contents, file_hashes, projection_cfg)
    except Exception as e:
        print(f"INVALID_CANONICAL_STATE: Failed to build normalized project snapshot: {e}", file=sys.stderr)
        return "INVALID_CANONICAL_STATE", [], 5

    snap_val = validate_document("canonical_project_snapshot", snapshot)
    if not snap_val.is_valid:
        print(f"INVALID_CANONICAL_STATE: Canonical project snapshot invalid:", file=sys.stderr)
        for e in snap_val.errors:
            print(f"  - {e}", file=sys.stderr)
        return "INVALID_CANONICAL_STATE", [], 5

    # 5. Expectation Contract Check
    if expectation_data:
        mismatches = []
        if expectation_data["project_id"] != snapshot["project_id"]:
            mismatches.append(f"project_id '{expectation_data['project_id']}' != '{snapshot['project_id']}'")
        if expectation_data["expected_source_ref"] != snapshot["source_ref"]:
            mismatches.append(f"source_ref '{expectation_data['expected_source_ref']}' != '{snapshot['source_ref']}'")
        if expectation_data["expected_source_sha"] != snapshot["source_sha"]:
            mismatches.append(f"source_sha '{expectation_data['expected_source_sha']}' != '{snapshot['source_sha']}'")
        if expectation_data["expected_milestone"] != snapshot["current_milestone"]:
            mismatches.append(f"milestone '{expectation_data['expected_milestone']}' != '{snapshot['current_milestone']}'")
        if expectation_data["expected_canonical_next_action"] != snapshot["canonical_next_action"]:
            mismatches.append(f"next_action '{expectation_data['expected_canonical_next_action']}' != '{snapshot['canonical_next_action']}'")
        if expectation_data.get("expected_target_base_sha") != snapshot.get("target_base_sha"):
            mismatches.append(f"target_base_sha '{expectation_data.get('expected_target_base_sha')}' != '{snapshot.get('target_base_sha')}'")

        if mismatches:
            if source_mode == "live_guard":
                print(f"STALE_EXPECTATION: Live canonical snapshot drifted from expectation:", file=sys.stderr)
                for m in mismatches:
                    print(f"  - {m}", file=sys.stderr)
                return "STALE_EXPECTATION", [], 1
            else:
                print(f"HOLD: PINNED_PROOF snapshot verification mismatch:", file=sys.stderr)
                for m in mismatches:
                    print(f"  - {m}", file=sys.stderr)
                return "HOLD", [], 1

    # 6. Instantiate planner provider & policy router
    exec_ctx: Optional[ProviderExecutionContext] = execution_context_override

    if not exec_ctx and routing_policy_path:
        registry = load_routing_policy(routing_policy_path)
        router = ProviderRouter(registry)
        route_res = router.select(risk_class=risk_class)
        if not route_res:
            print("PROVIDER_POLICY_HOLD: No eligible provider for policy/risk/data class", file=sys.stderr)
            return "PROVIDER_POLICY_HOLD", [], 1
        exec_ctx = route_res.context

    if provider_override is not None:
        provider = provider_override
        provider_name = provider.__class__.__name__
        model_name = getattr(provider, "model", "fake-model")
    elif exec_ctx:
        pid = exec_ctx.provider_id
        cls = PROVIDER_MAP.get(pid)
        if not cls:
            print(f"PROVIDER_UNAVAILABLE: Provider class for '{pid}' not found", file=sys.stderr)
            return "PROVIDER_UNAVAILABLE", [], 4
        provider = cls(model=exec_ctx.model_id)
        provider_name = provider.__class__.__name__
        model_name = exec_ctx.model_id
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("PROVIDER_CREDENTIAL_REQUIRED: OPENAI_API_KEY environment variable missing", file=sys.stderr)
            return "PROVIDER_CREDENTIAL_REQUIRED", [], 3
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        provider_name = "OpenAIPlannerProvider"
        model_name = "gpt-5.6-sol"

    planner_schema = load_schema("planner_decision.schema.json")

    prompt = (
        f"PROJECT_ID: {project_id}\n"
        f"REPOSITORY: {repository}\n"
        f"RESOLVED SOURCE SHA: {pinned_sha}\n"
        f"CANONICAL MILESTONE: {snapshot['current_milestone']}\n"
        f"CANONICAL STATUS: {snapshot['current_status']}\n"
        f"CANONICAL NEXT ACTION: {snapshot['canonical_next_action']}\n"
        f"TARGET BASE SHA: {snapshot['target_base_sha'] or 'NONE'}\n\n"
        f"DECISIONS:\n{raw_contents.get('decisions', '')[:2000]}\n\n"
        f"ROADMAP:\n{raw_contents.get('roadmap', '')[:2000]}\n"
    )

    policy_engine = PolicyEngine()
    traces: List[Dict[str, Any]] = []
    overall_disposition = "SHADOW_ACCEPT"

    trace_dir = trace_dir_override or RUNTIME_TRACE_DIR
    trace_dir.mkdir(parents=True, exist_ok=True)

    for i in range(repeat):
        decision_data = None
        resp_id = None
        usage_data = None
        last_exception = None

        for attempt in range(1 + max_network_retries):
            try:
                decision_data, resp_id, usage_data = provider.generate_plan(prompt, planner_schema)
                last_exception = None
                break
            except (PlannerCredentialError, PermissionError) as pe:
                print(f"PROVIDER_CREDENTIAL_REQUIRED: {pe}", file=sys.stderr)
                return "PROVIDER_CREDENTIAL_REQUIRED", traces, 3
            except PlannerContractError as pce:
                print(f"HOLD: Planner contract failure: {pce}", file=sys.stderr)
                return "HOLD", traces, 1
            except (PlannerTransientError, ConnectionError, TimeoutError, OSError) as te:
                last_exception = te
                if attempt < max_network_retries:
                    continue
                else:
                    break
            except Exception as unk:
                print(f"HOLD: Unknown provider failure: {unk}", file=sys.stderr)
                return "HOLD", traces, 1

        if last_exception is not None or decision_data is None:
            print(f"PROVIDER_UNAVAILABLE: Planner transient evaluation failed after retries: {last_exception}", file=sys.stderr)
            return "PROVIDER_UNAVAILABLE", traces, 4

        # Check stale revision after batch step (re-resolution failure MUST fail closed in live_guard mode)
        if source_mode == "live_guard":
            try:
                re_resolved_sha = adapter.resolve_ref_to_sha()
            except Exception:
                re_resolved_sha = None
        else:
            re_resolved_sha = pinned_sha

        policy_checks, final_disp = policy_engine.evaluate(
            decision=decision_data,
            expected_project_id=project_id,
            pinned_source_sha=pinned_sha,
            canonical_snapshot=snapshot,
            re_resolved_sha=re_resolved_sha
        )

        trace_id = f"TRACE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        trace: Dict[str, Any] = {
            "schema_version": "0.1.0",
            "trace_id": trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "repository": repository,
            "configured_source_ref": control_ref,
            "resolved_source_sha": pinned_sha,
            "input_file_hashes": file_hashes,
            "planner_provider": provider_name,
            "model": model_name,
            "provider_response_id": resp_id,
            "usage": usage_data,
            "planner_decision": decision_data,
            "policy_checks": [c.to_dict() for c in policy_checks],
            "final_disposition": final_disp,
            "mutation_performed": False,
            "limitations": [
                f"Shadow mode execution only ({source_mode}).",
                "No product mutation performed.",
                "Evaluated against pinned canonical control revision."
            ]
        }

        # Populate real routing metadata from execution context if present
        if exec_ctx:
            trace["provider_id"] = exec_ctx.provider_id
            trace["billing_class"] = exec_ctx.billing_class
            trace["data_classification"] = exec_ctx.data_classification
            trace["selection_reason"] = exec_ctx.selection_reason
            trace["fallback_used"] = exec_ctx.fallback_used

        # Fail closed on trace schema validation failure
        trace_val = validate_document("shadow_trace", trace)
        if not trace_val.is_valid:
            print(f"FAIL_CLOSED: Generated shadow trace failed schema validation!", file=sys.stderr)
            for e in trace_val.errors:
                print(f"  - {e}", file=sys.stderr)
            final_disp = "HOLD"
            trace["final_disposition"] = "HOLD"

        if final_disp == "HOLD":
            overall_disposition = "HOLD"

        trace_path = trace_dir / f"{trace_id}.json"
        with open(trace_path, "w", encoding="utf-8") as tf:
            json.dump(trace, tf, indent=2, ensure_ascii=False)
            tf.write("\n")

        traces.append(trace)

    exit_code = 0 if overall_disposition == "SHADOW_ACCEPT" else 1
    return overall_disposition, traces, exit_code

def main(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AOS Shadow Orchestrator CLI")
    parser.add_argument("--project", required=True, help="Path to project descriptor JSON file")
    parser.add_argument("--expectation", help="Optional path to shadow expectation JSON file")
    parser.add_argument("--routing-policy", help="Optional path to planner routing policy JSON file")
    parser.add_argument("--source-mode", choices=["live_guard", "pinned_proof"], default="live_guard", help="Source resolution mode")
    parser.add_argument("--repeat", type=int, default=1, help="Number of shadow decisions to evaluate")

    parsed = parser.parse_args(args)
    disp, traces, code = run_shadow_orchestration(
        descriptor_path=parsed.project,
        expectation_path=parsed.expectation,
        routing_policy_path=parsed.routing_policy,
        source_mode=parsed.source_mode,
        repeat=parsed.repeat,
    )

    print(f"DISPOSITION: {disp}")
    print(f"EXECUTED TRACES: {len(traces)}")
    for t in traces:
        print(f"  - Trace ID: {t['trace_id']} | Disposition: {t['final_disposition']} | Mutation: {t['mutation_performed']}")

    return code

if __name__ == "__main__":
    sys.exit(main())
