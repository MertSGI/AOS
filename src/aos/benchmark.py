"""AOS Cross-Provider Shadow Benchmark CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.planner import PlannerProvider
from aos.provider_registry import ProviderRegistry, ProviderRouter, load_routing_policy
from aos.providers.gemini import GeminiPlannerProvider
from aos.providers.groq import GroqPlannerProvider
from aos.providers.ollama import OllamaPlannerProvider
from aos.planner import OpenAIPlannerProvider
from aos.shadow import run_shadow_orchestration


PROVIDER_FACTORIES = {
    "gemini": lambda model: GeminiPlannerProvider(model=model),
    "groq": lambda model: GroqPlannerProvider(model=model),
    "ollama": lambda model: OllamaPlannerProvider(model=model),
    "openai": lambda model: OpenAIPlannerProvider(model=model),
}


def run_benchmark(
    descriptor_path: str,
    expectation_path: Optional[str],
    routing_policy_path: str,
    provider_ids: List[str],
    repeat: int = 3,
    trace_dir_override: Optional[Path] = None,
    provider_overrides: Optional[Dict[str, PlannerProvider]] = None,
) -> Dict[str, Any]:
    """Execute shadow benchmark across multiple providers.

    Returns a summary dict with per-provider results.
    """
    registry = load_routing_policy(routing_policy_path)
    results: Dict[str, Any] = {
        "benchmark_status": "PASS",
        "providers": {},
        "total_runs": 0,
        "total_pass": 0,
        "total_hold": 0,
    }

    for pid in provider_ids:
        entry = registry.get_provider(pid)
        if entry is None:
            results["providers"][pid] = {"status": "PROVIDER_NOT_FOUND"}
            results["benchmark_status"] = "HOLD"
            continue

        if provider_overrides and pid in provider_overrides:
            provider = provider_overrides[pid]
        else:
            factory = PROVIDER_FACTORIES.get(pid)
            if factory is None:
                results["providers"][pid] = {"status": "FACTORY_NOT_FOUND"}
                results["benchmark_status"] = "HOLD"
                continue
            provider = factory(entry.model_id)

        provider_results = []
        for i in range(repeat):
            disp, traces, code = run_shadow_orchestration(
                descriptor_path,
                expectation_path=expectation_path,
                repeat=1,
                provider_override=provider,
                trace_dir_override=trace_dir_override,
            )
            run_result = {
                "run": i + 1,
                "disposition": disp,
                "exit_code": code,
                "trace_count": len(traces),
            }
            if traces:
                run_result["trace_id"] = traces[0].get("trace_id")
                run_result["provider"] = traces[0].get("planner_provider")
                run_result["model"] = traces[0].get("model")
                run_result["usage"] = traces[0].get("usage")
                run_result["mutation_performed"] = traces[0].get("mutation_performed")
            provider_results.append(run_result)
            results["total_runs"] += 1
            if disp == "SHADOW_ACCEPT":
                results["total_pass"] += 1
            else:
                results["total_hold"] += 1
                results["benchmark_status"] = "HOLD"

            # If STALE_EXPECTATION, stop entire benchmark
            if disp == "STALE_EXPECTATION":
                results["benchmark_status"] = "STALE"
                results["providers"][pid] = {
                    "status": "STALE_EXPECTATION",
                    "runs": provider_results,
                }
                return results

        all_pass = all(r["disposition"] == "SHADOW_ACCEPT" for r in provider_results)
        results["providers"][pid] = {
            "status": "PASS" if all_pass else "HOLD",
            "runs": provider_results,
        }

    return results


def main(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AOS Cross-Provider Shadow Benchmark")
    parser.add_argument("--project", required=True, help="Path to project descriptor JSON file")
    parser.add_argument("--expectation", help="Path to shadow expectation JSON file")
    parser.add_argument("--routing-policy", required=True, help="Path to planner routing policy JSON file")
    parser.add_argument("--providers", required=True, help="Comma-separated provider IDs to benchmark")
    parser.add_argument("--repeat", type=int, default=3, help="Number of shadow decisions per provider")

    parsed = parser.parse_args(args)
    provider_ids = [p.strip() for p in parsed.providers.split(",")]

    results = run_benchmark(
        descriptor_path=parsed.project,
        expectation_path=parsed.expectation,
        routing_policy_path=parsed.routing_policy,
        provider_ids=provider_ids,
        repeat=parsed.repeat,
    )

    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if results["benchmark_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
