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


def configured_project_profiles(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    projects = config.get("projects")
    if isinstance(projects, dict):
        return {
            str(project_id): dict(profile)
            for project_id, profile in projects.items()
            if isinstance(profile, dict)
        }
    default_profile = config.get("default_project")
    if isinstance(default_profile, dict) and default_profile.get("project_id"):
        return {str(default_profile["project_id"]): dict(default_profile)}
    return {}


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
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    project_id = project_id.strip()
    projects = configured_project_profiles(config)
    if project_id not in projects:
        raise ValueError(f"Unknown project_id: {project_id}")
    result = _client(config).continue_project(
        goal=goal.strip(),
        project_id=project_id,
        constraints=constraints,
        red_lines=red_lines,
        max_batches_per_cycle=max_batches,
        max_iterations_per_batch=int(payload.get("max_iterations", 30)),
        continuous=True,
    )
    if result.get("project_id") != project_id:
        raise RuntimeClientError("Runtime response project_id did not match the requested project")
    resolved_fields = ("workspace", "descriptor_path", "routing_policy_path")
    if any(not isinstance(result.get(field), str) or not result.get(field) for field in resolved_fields):
        raise RuntimeClientError("Runtime response omitted the resolved project profile")
    result["mode"] = "RUNTIME_V1_AUTONOMOUS_GOAL"
    result["run_plan_required"] = False
    return result


def execute_command_on_runtime(command_name: str, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    client = _client(config)
    cmd = command_name.strip().lower()
    if cmd == "pause-safe":
        return client.pause_safe()
    if cmd == "resume":
        return client.resume()
    if cmd == "heartbeat-now":
        return client.heartbeat_now()
    if cmd == "checkpoint-now":
        return client.checkpoint_now()
    if cmd == "publish-relay-now":
        return client.publish_relay_now()
    if cmd == "restart-worker":
        cid = payload.get("command_id")
        if not cid:
            raise ValueError("command_id is required for restart-worker")
        return client.restart_worker(str(cid))
    raise ValueError(f"Unsupported runtime command: {command_name}")
