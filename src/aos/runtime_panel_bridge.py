"""AOS Direct bridge onto the shared Runtime Contract V1 API."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from aos.runtime_client import RuntimeClient, RuntimeClientError


def runtime_configured(config: Dict[str, Any]) -> bool:
    base_url = str(config.get("runtime_api_url") or "").strip()
    token_path = str(config.get("runtime_token_path") or "").strip()
    return bool(base_url and token_path)


def _client(config: Dict[str, Any]) -> RuntimeClient:
    base_url = str(config.get("runtime_api_url") or "").strip()
    token_path = str(config.get("runtime_token_path") or "").strip()
    if not base_url or not token_path:
        raise ValueError("Runtime V1 API is not configured in AOS Direct")
    return RuntimeClient(base_url, Path(token_path))


def runtime_status(config: Dict[str, Any]) -> Dict[str, Any]:
    if not runtime_configured(config):
        return {
            "runtime_v1": {"runtime_state": "NOT_CONFIGURED"},
            "host_state": "UNKNOWN",
            "pending_jobs": 0,
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }
    try:
        health = _client(config).health()
        return {
            "runtime_v1": health,
            "host_state": health.get("runtime_state", "UNKNOWN"),
            "pending_jobs": len(health.get("active_commands", []) or []),
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }
    except Exception as exc:
        return {
            "runtime_v1": {
                "runtime_state": "UNAVAILABLE",
                "error_class": exc.__class__.__name__,
                "message": str(exc)[:500],
            },
            "host_state": "RUNTIME_V1_UNAVAILABLE",
            "pending_jobs": 0,
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }


def submit_goal_to_runtime(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("Goal is required")
    constraints = payload.get("constraints", [])
    red_lines = payload.get("red_lines", [])
    if not isinstance(constraints, list) or any(not isinstance(x, str) for x in constraints):
        raise ValueError("constraints must be an array of strings")
    if not isinstance(red_lines, list) or any(not isinstance(x, str) for x in red_lines):
        raise ValueError("red_lines must be an array of strings")
    max_batches = int(payload.get("max_batches", 1))
    if not 1 <= max_batches <= 50:
        raise ValueError("max_batches must be between 1 and 50")
    result = _client(config).continue_project(
        goal=goal.strip(),
        project_id=payload.get("project_id"),
        constraints=constraints,
        red_lines=red_lines,
        max_batches_per_cycle=max_batches,
        max_iterations_per_batch=int(payload.get("max_iterations", 30)),
        continuous=True,
    )
    result["mode"] = "RUNTIME_V1_AUTONOMOUS_GOAL"
    result["run_plan_required"] = False
    return result
