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

from aos.planner import FakePlannerProvider, OpenAIPlannerProvider, PlannerProvider
from aos.policy import PolicyEngine
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document, validate_file

RUNTIME_TRACE_DIR = Path(__file__).resolve().parent.parent.parent / ".aos-runtime" / "shadow"

def run_shadow_orchestration(
    descriptor_path: str,
    repeat: int = 1,
    provider_override: Optional[PlannerProvider] = None,
    trace_dir_override: Optional[Path] = None
) -> Tuple[str, List[Dict[str, Any]], int]:
    """Execute shadow orchestration loop."""
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

    adapter = ProjectSourceAdapter(repository=repository, control_ref=control_ref)

    # 2. Pin source SHA
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

    # Validate state.json
    state_str = raw_contents.get("state", "{}")
    try:
        state_data = json.loads(state_str)
    except Exception as e:
        print(f"INVALID_CANONICAL_STATE: Failed to parse STATE.json: {e}", file=sys.stderr)
        return "INVALID_CANONICAL_STATE", [], 5

    state_val = validate_document("state", state_data)
    if not state_val.is_valid:
        print(f"INVALID_CANONICAL_STATE: Remote STATE.json schema invalid:", file=sys.stderr)
        for e in state_val.errors:
            print(f"  - {e}", file=sys.stderr)
        return "INVALID_CANONICAL_STATE", [], 5

    # 4. Instantiate planner provider
    if provider_override is not None:
        provider = provider_override
        provider_name = provider.__class__.__name__
        model_name = getattr(provider, "model", "fake-model")
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("PROVIDER_CREDENTIAL_REQUIRED: OPENAI_API_KEY environment variable missing", file=sys.stderr)
            return "PROVIDER_CREDENTIAL_REQUIRED", [], 3
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        provider_name = "OpenAI"
        model_name = "gpt-5.6-sol"

    load_planner_schema = validate_document("planner_decision", {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "source_sha": pinned_sha,
        "selected_milestone": state_data.get("current_milestone", ""),
        "selected_next_action": state_data.get("next_action", ""),
        "target_base_sha": None,
        "risk_class": "R0",
        "mutation_intent": "NONE",
        "ambiguity_detected": False,
        "ambiguity_reasons": [],
        "human_gate_required": False,
        "rationale": "Valid test rationale",
        "disposition": "SHADOW_ACCEPT"
    })

    from aos.validate import load_schema
    planner_schema = load_schema("planner_decision.schema.json")

    # Construct bounded prompt
    prompt = (
        f"PROJECT_ID: {project_id}\n"
        f"REPOSITORY: {repository}\n"
        f"RESOLVED SOURCE SHA: {pinned_sha}\n"
        f"CANONICAL MILESTONE: {state_data.get('current_milestone')}\n"
        f"CANONICAL STATUS: {state_data.get('current_status')}\n"
        f"CANONICAL NEXT ACTION: {state_data.get('next_action')}\n\n"
        f"DECISIONS:\n{raw_contents.get('decisions', '')[:2000]}\n\n"
        f"ROADMAP:\n{raw_contents.get('roadmap', '')[:2000]}\n"
    )

    policy_engine = PolicyEngine()
    traces: List[Dict[str, Any]] = []
    overall_disposition = "SHADOW_ACCEPT"

    trace_dir = trace_dir_override or RUNTIME_TRACE_DIR
    trace_dir.mkdir(parents=True, exist_ok=True)

    for i in range(repeat):
        try:
            decision_data, resp_id, usage_data = provider.generate_plan(prompt, planner_schema)
        except Exception as e:
            print(f"PROVIDER_UNAVAILABLE: Planner evaluation failed: {e}", file=sys.stderr)
            return "PROVIDER_UNAVAILABLE", traces, 4

        # Check stale revision after batch step
        try:
            re_resolved_sha = adapter.resolve_ref_to_sha()
        except Exception:
            re_resolved_sha = pinned_sha

        policy_checks, final_disp = policy_engine.evaluate(
            decision=decision_data,
            expected_project_id=project_id,
            pinned_source_sha=pinned_sha,
            canonical_state_data=state_data,
            re_resolved_sha=re_resolved_sha
        )

        if final_disp == "HOLD":
            overall_disposition = "HOLD"

        trace_id = f"TRACE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        trace = {
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
                "Shadow mode execution only.",
                "No product mutation performed.",
                "Evaluated against pinned canonical control revision."
            ]
        }

        # Validate trace against schema
        trace_val = validate_document("shadow_trace", trace)
        if not trace_val.is_valid:
            print(f"Warning: generated shadow trace failed schema validation: {[e.message for e in trace_val.errors]}", file=sys.stderr)

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
    parser.add_argument("--repeat", type=int, default=1, help="Number of shadow decisions to evaluate")

    parsed = parser.parse_args(args)
    disp, traces, code = run_shadow_orchestration(parsed.project, repeat=parsed.repeat)

    print(f"DISPOSITION: {disp}")
    print(f"EXECUTED TRACES: {len(traces)}")
    for t in traces:
        print(f"  - Trace ID: {t['trace_id']} | Disposition: {t['final_disposition']} | Mutation: {t['mutation_performed']}")

    return code

if __name__ == "__main__":
    sys.exit(main())
