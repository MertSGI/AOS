"""Fresh, machine-local operational truth projection for AOS Runtime V1.

This module deliberately does not read canonical ``STATE.json``.  Governance
state and live operational observations have different authorities and
lifetimes.  Every projection is rebuilt from its observation sources and
atomically replaces the previous machine-local file.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json

CURRENT_TRUTH_CONTRACT_VERSION = "1.0.0"
CURRENT_TRUTH_FILENAME = "current-truth.json"
PROTECTED_COMMANDS = {
    "lari": "continue-b181ddc574c25c2aa0f2a6b9",
    "lari_ui_v2": "continue-61be4ab1af53cfa646d773ce",
    "historical_maintenance": "continue-8a922a56b955ce3ae073fa86",
}


def _known(value: Any, *basis: str) -> Dict[str, Any]:
    return {"status": "KNOWN", "value": value, "evidence_basis": list(basis)}


def _unknown(reason: str, *basis: str) -> Dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "value": None,
        "reason": reason,
        "evidence_basis": list(basis),
    }


def _contradiction(values: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "status": "CONTRADICTION",
        "value": None,
        "reason": reason,
        "observed_values": dict(values),
        "evidence_basis": list(values),
    }


def _read_mapping(path: Path) -> tuple[Optional[Dict[str, Any]], str]:
    if not path.is_file():
        return None, "MISSING"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "UNREADABLE_OR_INVALID_JSON"
    if not isinstance(value, dict):
        return None, "NOT_AN_OBJECT"
    return value, "READ"


def _git_sha(repo_root: Path, ref: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        return None
    return value


def _live_health(config: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        port = int(config.get("port", 8770))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health",
            headers={"Accept": "application/json", "User-Agent": "AOS-Current-Truth/1.0"},
        )
        with urllib.request.urlopen(request, timeout=1.5) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict) or value.get("contract_version") != CONTRACT_VERSION:
        return None
    return value


def _identity_observation(
    candidates: Mapping[str, Any],
    *,
    missing_reason: str,
    contradiction_reason: str,
) -> Dict[str, Any]:
    present = {
        source: str(value)
        for source, value in candidates.items()
        if value is not None and str(value).strip()
    }
    if not present:
        return _unknown(missing_reason, *candidates.keys())
    if len(set(present.values())) > 1:
        return _contradiction(present, contradiction_reason)
    return _known(next(iter(present.values())), *present.keys())


def _active_slot(runtime_home: Path) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Path]:
    supervisor_config, _ = _read_mapping(runtime_home / "supervisor-config.json")
    configured_root = supervisor_config.get("supervisor_root") if supervisor_config else None
    supervisor_root = Path(str(configured_root)).expanduser().resolve() if configured_root else runtime_home / "supervisor"
    pointer, _ = _read_mapping(supervisor_root / "active-slot.json")
    if not pointer or pointer.get("active") not in ("stable", "candidate"):
        return pointer, None, supervisor_root
    key = "candidate_slot_id" if pointer["active"] == "candidate" else "stable_slot_id"
    slot_id = str(pointer.get(key) or "")
    slot, _ = _read_mapping(supervisor_root / "slots" / f"{slot_id}.json")
    return pointer, slot, supervisor_root


def _candidate_manifest(
    runtime_home: Path,
    runtime_config: Mapping[str, Any],
    slot: Optional[Mapping[str, Any]],
) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    roots: list[Path] = []
    configured = runtime_config.get("runtime_slot_root")
    if configured:
        roots.append(Path(str(configured)).expanduser().resolve())
    source_sha = (slot or {}).get("source_sha") or runtime_config.get("candidate_source_sha")
    if source_sha:
        roots.append(runtime_home / "candidate" / str(source_sha))
    for root in roots:
        path = root / "candidate-manifest.json"
        value, _ = _read_mapping(path)
        if value is not None:
            return value, path
    return None, (roots[0] / "candidate-manifest.json") if roots else None


def _policy_observation(runtime_config: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    production_candidates: Dict[str, Any] = {"runtime_contract": "NO_GO"}
    if "production" in runtime_config:
        production_candidates["runtime-config.json"] = runtime_config.get("production")
    production = _identity_observation(
        production_candidates,
        missing_reason="PRODUCTION_POLICY_UNAVAILABLE",
        contradiction_reason="RUNTIME_CONFIG_CONFLICTS_WITH_PRODUCTION_NO_GO",
    )

    paid_sources: Dict[str, Any] = {}
    projects = runtime_config.get("projects")
    if isinstance(projects, dict):
        for project_id, profile in projects.items():
            if not isinstance(profile, dict) or not profile.get("routing_policy_path"):
                continue
            policy, _ = _read_mapping(Path(str(profile["routing_policy_path"])).expanduser().resolve())
            if policy is None:
                continue
            enabled = bool(policy.get("allow_paid_fallback", False)) and bool(
                policy.get("paid_fallback_enabled", False)
            )
            paid_sources[f"routing_policy:{project_id}"] = "ENABLED" if enabled else "DISABLED"
    if not paid_sources:
        paid_sources["runtime_contract_default"] = "DISABLED"
    paid = _identity_observation(
        paid_sources,
        missing_reason="PAID_FALLBACK_POLICY_UNAVAILABLE",
        contradiction_reason="PAID_FALLBACK_POLICY_SOURCES_CONFLICT",
    )
    if any(value == "ENABLED" for value in paid_sources.values()):
        paid = _contradiction(paid_sources, "PAID_FALLBACK_MUST_REMAIN_DISABLED")
    return production, paid


def _command_observations(runtime_root: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    admissions, admission_read = _read_mapping(runtime_root / "command-admission.json")
    records = admissions.get("records") if admissions else None
    if not isinstance(records, dict):
        records = {}
    command_states: Dict[str, Any] = {}
    command_admissions: Dict[str, Any] = {}
    for label, command_id in PROTECTED_COMMANDS.items():
        state, state_read = _read_mapping(runtime_root / "commands" / command_id / "state.json")
        if state is None or not state.get("state"):
            command_states[label] = _unknown(
                f"COMMAND_STATE_{state_read}",
                f"commands/{command_id}/state.json",
            )
        else:
            command_states[label] = _known(
                str(state["state"]),
                f"commands/{command_id}/state.json",
            )
        record = records.get(command_id)
        if not isinstance(record, dict) or not record.get("state"):
            command_admissions[label] = _unknown(
                f"COMMAND_ADMISSION_{admission_read if admissions is None else 'RECORD_MISSING'}",
                "command-admission.json",
            )
        else:
            command_admissions[label] = _known(
                str(record["state"]),
                "command-admission.json",
            )
        command_states[label]["command_id"] = command_id
        command_admissions[label]["command_id"] = command_id
    admission_store = (
        _known("READABLE", "command-admission.json")
        if admissions is not None
        else _unknown(f"COMMAND_ADMISSION_STORE_{admission_read}", "command-admission.json")
    )
    admission_store["commands"] = command_admissions
    return command_states, admission_store


def _walk_observations(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        if value.get("status") in {"KNOWN", "UNKNOWN", "CONTRADICTION"}:
            yield value
        for child in value.values():
            yield from _walk_observations(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_observations(child)


def generate_current_truth(
    runtime_home: Path,
    repo_root: Path,
    *,
    runtime_observation: Optional[Mapping[str, Any]] = None,
    query_runtime: bool = True,
) -> Dict[str, Any]:
    """Build a fresh projection without consulting a previous projection."""
    runtime_home = runtime_home.expanduser().resolve()
    repo_root = repo_root.expanduser().resolve()
    runtime_config, config_read = _read_mapping(runtime_home / "runtime-config.json")
    runtime_config = runtime_config or {}
    runtime_root_value = runtime_config.get("runtime_root")
    runtime_root = (
        Path(str(runtime_root_value)).expanduser().resolve()
        if runtime_root_value
        else runtime_home / "state"
    )

    health = dict(runtime_observation) if runtime_observation is not None else None
    if health is None and query_runtime:
        health = _live_health(runtime_config)
    pointer, slot, supervisor_root = _active_slot(runtime_home)
    manifest, manifest_path = _candidate_manifest(runtime_home, runtime_config, slot)

    repo_head = _git_sha(repo_root, "HEAD")
    operations_ref = runtime_config.get("operations_ref") or runtime_config.get("operations_branch")
    operations_repo_value = runtime_config.get("operations_repo_path") or runtime_config.get("authoritative_repo_path")
    operations_repo = Path(str(operations_repo_value)).expanduser().resolve() if operations_repo_value else repo_root
    operations_sha = _git_sha(operations_repo, str(operations_ref)) if operations_ref else None

    pointer_slot_id = None
    if pointer and pointer.get("active") in ("stable", "candidate"):
        pointer_key = "candidate_slot_id" if pointer["active"] == "candidate" else "stable_slot_id"
        pointer_slot_id = pointer.get(pointer_key)

    configured_source = _identity_observation(
        {
            "runtime-config.json": runtime_config.get("candidate_source_sha"),
            "active slot record": (slot or {}).get("source_sha"),
            "candidate manifest": (manifest or {}).get("candidate_source_sha"),
            "candidate build binding": (manifest or {}).get("build_source_sha"),
        },
        missing_reason="CONFIGURED_SOURCE_SHA_UNAVAILABLE",
        contradiction_reason="SLOT_CANDIDATE_SOURCE_IDENTITY_CONFLICT",
    )
    configured_slot = _identity_observation(
        {
            "runtime-config.json": runtime_config.get("runtime_slot_id"),
            "active slot pointer": pointer_slot_id,
            "active slot record": (slot or {}).get("slot_id"),
            "candidate manifest": (manifest or {}).get("candidate_slot_id"),
        },
        missing_reason="CONFIGURED_SLOT_ID_UNAVAILABLE",
        contradiction_reason="ACTIVE_SLOT_CANDIDATE_IDENTITY_CONFLICT",
    )
    source_binding = _identity_observation(
        {
            "runtime-config.json": runtime_config.get("candidate_source_sha"),
            "active slot record": (slot or {}).get("source_sha"),
            "candidate manifest": (manifest or {}).get("candidate_source_sha"),
            "candidate build binding": (manifest or {}).get("build_source_sha"),
            "live runtime health": (health or {}).get("runtime_source_sha"),
        },
        missing_reason="SOURCE_BINDING_UNAVAILABLE",
        contradiction_reason="RUNTIME_SLOT_CANDIDATE_SOURCE_IDENTITY_CONFLICT",
    )
    slot_binding = _identity_observation(
        {
            "runtime-config.json": runtime_config.get("runtime_slot_id"),
            "active slot pointer": pointer_slot_id,
            "active slot record": (slot or {}).get("slot_id"),
            "candidate manifest": (manifest or {}).get("candidate_slot_id"),
            "live runtime health": (health or {}).get("runtime_slot_id"),
        },
        missing_reason="SLOT_BINDING_UNAVAILABLE",
        contradiction_reason="ACTIVE_SLOT_RUNTIME_CANDIDATE_IDENTITY_CONFLICT",
    )

    if health and health.get("runtime_state"):
        runtime_health = _known(str(health["runtime_state"]), "live runtime health")
    else:
        runtime_health = _unknown("LIVE_RUNTIME_HEALTH_UNAVAILABLE", "loopback /v1/health")

    maintenance, maintenance_read = _read_mapping(runtime_root / "maintenance-state.json")
    if maintenance and maintenance.get("state") in ("PAUSED_SAFE", "RUNNING"):
        maintenance_observation = _known(str(maintenance["state"]), "maintenance-state.json")
    elif health and isinstance(health.get("paused"), bool):
        maintenance_observation = _known(
            "PAUSED_SAFE" if health["paused"] else "RUNNING",
            "live runtime health",
        )
    else:
        maintenance_observation = _unknown(
            f"MAINTENANCE_STATE_{maintenance_read}",
            "maintenance-state.json",
            "live runtime health",
        )

    command_states, admission_store = _command_observations(runtime_root)
    production_observation, paid_observation = _policy_observation(runtime_config)

    observations: Dict[str, Any] = {
        "repository": {
            "local_checkout_head": (
                _known(repo_head, "git rev-parse HEAD")
                if repo_head
                else _unknown("REPOSITORY_HEAD_UNAVAILABLE", "git rev-parse HEAD")
            ),
            "configured_operations_ref": (
                _known(str(operations_ref), "runtime-config.json")
                if operations_ref
                else _unknown("OPERATIONS_REF_NOT_CONFIGURED", "runtime-config.json")
            ),
            "observed_operations_ref_sha": (
                _known(operations_sha, f"git rev-parse {operations_ref}")
                if operations_sha
                else _unknown("OPERATIONS_REF_SHA_UNAVAILABLE", "configured operations repository")
            ),
        },
        "runtime": {
            "configuration": (
                _known("READABLE", "runtime-config.json")
                if config_read == "READ"
                else _unknown(f"RUNTIME_CONFIG_{config_read}", "runtime-config.json")
            ),
            "source_sha": (
                _known(str(health["runtime_source_sha"]), "live runtime health")
                if health and health.get("runtime_source_sha")
                else _unknown("LIVE_RUNTIME_SOURCE_SHA_UNAVAILABLE", "loopback /v1/health")
            ),
            "slot_id": (
                _known(str(health["runtime_slot_id"]), "live runtime health")
                if health and health.get("runtime_slot_id")
                else _unknown("LIVE_RUNTIME_SLOT_ID_UNAVAILABLE", "loopback /v1/health")
            ),
            "health": runtime_health,
            "maintenance": maintenance_observation,
        },
        "slot_candidate": {
            "active_slot_pointer": (
                _known(
                    {
                        "active": pointer.get("active"),
                        "slot_id": pointer_slot_id,
                        "promotion_state": pointer.get("promotion_state"),
                    },
                    "active-slot.json",
                )
                if pointer_slot_id
                else _unknown("ACTIVE_SLOT_POINTER_UNAVAILABLE", "active-slot.json")
            ),
            "candidate_manifest": (
                _known(
                    {
                        "candidate_slot_id": manifest.get("candidate_slot_id"),
                        "candidate_source_sha": manifest.get("candidate_source_sha"),
                        "build_source_sha": manifest.get("build_source_sha"),
                        "provenance": manifest.get("provenance"),
                    },
                    str(manifest_path),
                )
                if manifest is not None
                else _unknown("CANDIDATE_MANIFEST_UNAVAILABLE", str(manifest_path or "candidate-manifest.json"))
            ),
            "configured_source_sha": configured_source,
            "configured_slot_id": configured_slot,
            "source_binding": source_binding,
            "slot_binding": slot_binding,
        },
        "commands": {
            "admission_store": admission_store,
            "protected_command_states": command_states,
        },
        "policy": {
            "production": production_observation,
            "paid_fallback": paid_observation,
        },
    }

    contradictions = [
        {
            "path": path,
            "reason": observation.get("reason"),
            "observed_values": observation.get("observed_values", {}),
        }
        for path, observation in (
            ("slot_candidate.configured_source_sha", configured_source),
            ("slot_candidate.configured_slot_id", configured_slot),
            ("slot_candidate.source_binding", source_binding),
            ("slot_candidate.slot_binding", slot_binding),
            ("policy.production", production_observation),
            ("policy.paid_fallback", paid_observation),
        )
        if observation["status"] == "CONTRADICTION"
    ]
    all_observations = list(_walk_observations(observations))
    overall_status = (
        "CONTRADICTION"
        if contradictions
        else "UNKNOWN"
        if any(item["status"] == "UNKNOWN" for item in all_observations)
        else "KNOWN"
    )
    return {
        "contract_version": CURRENT_TRUTH_CONTRACT_VERSION,
        "observed_at": utc_now(),
        "overall_status": overall_status,
        "refresh_status": "DEGRADED" if overall_status != "KNOWN" else "READY",
        "projection_authority": "MACHINE_LOCAL_OPERATIONAL_TELEMETRY",
        "canonical_governance_source": "docs/project-control/",
        "observations": observations,
        "contradictions": contradictions,
        "production": "NO_GO",
        "paid_fallback": "DISABLED",
    }


def _degraded_projection(error: BaseException) -> Dict[str, Any]:
    return {
        "contract_version": CURRENT_TRUTH_CONTRACT_VERSION,
        "observed_at": utc_now(),
        "overall_status": "UNKNOWN",
        "refresh_status": "DEGRADED",
        "projection_authority": "MACHINE_LOCAL_OPERATIONAL_TELEMETRY",
        "canonical_governance_source": "docs/project-control/",
        "observations": {
            "projection": _unknown(
                f"REFRESH_FAILED_{error.__class__.__name__}",
                "fresh observation cycle",
            )
        },
        "contradictions": [],
        "production": "NO_GO",
        "paid_fallback": "DISABLED",
    }


def refresh_current_truth(
    runtime_home: Path,
    repo_root: Path,
    *,
    runtime_observation: Optional[Mapping[str, Any]] = None,
    query_runtime: bool = True,
) -> Dict[str, Any]:
    """Generate and atomically persist a fresh projection.

    Collection failures become an explicit degraded/unknown document.  Failure
    to persist is allowed to propagate so the caller can surface it without
    crashing Runtime V1.
    """
    runtime_home = runtime_home.expanduser().resolve()
    try:
        projection = generate_current_truth(
            runtime_home,
            repo_root,
            runtime_observation=runtime_observation,
            query_runtime=query_runtime,
        )
    except Exception as exc:
        projection = _degraded_projection(exc)
    atomic_json(runtime_home / CURRENT_TRUTH_FILENAME, projection)
    return projection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate fresh AOS operational current truth")
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--repo-root", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    projection = refresh_current_truth(Path(args.runtime_home), Path(args.repo_root))
    print(json.dumps(projection, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
