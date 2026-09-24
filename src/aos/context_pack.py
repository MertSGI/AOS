"""Bounded, durable, sanitized continuity contract between execution resources."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Mapping


MAX_CONTEXT_PACK_BYTES = 32_768
_FORBIDDEN_KEYS = {"prompt", "transcript", "reasoning", "credential", "cookie", "token", "raw_error", "headers", "body"}


def _bounded_map(value: Mapping[str, Any], *, limit: int = 256) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, item in list(value.items())[:limit]:
        if any(word in str(key).lower() for word in _FORBIDDEN_KEYS):
            continue
        text = str(item)
        if len(str(key)) <= 256 and len(text) <= 512:
            result[str(key)] = text
    return result


def build_context_pack(
    *, objective_id: str, authority_id: str, source_sha: str,
    workspace_fingerprint: str, checkpoint_id: str,
    completed_work_unit_ids: Iterable[str] = (),
    completed_work_unit_signatures: Mapping[str, str] = {},
    artifact_hashes: Mapping[str, str] = {},
    remaining_work: Iterable[str] = (),
    availability: Mapping[str, Any] = {},
    boundaries: Iterable[str] = (),
    read_context: Iterable[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    pack: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "objective_id": str(objective_id)[:256],
        "authority_id": str(authority_id)[:256],
        "source_sha": source_sha,
        "workspace_fingerprint": workspace_fingerprint,
        "checkpoint_id": str(checkpoint_id)[:256],
        "completed_work_unit_ids": sorted({str(item)[:256] for item in completed_work_unit_ids})[:512],
        "completed_work_unit_signatures": _bounded_map(completed_work_unit_signatures, limit=512),
        "artifact_hashes": _bounded_map(artifact_hashes, limit=512),
        "remaining_work": [str(item)[:512] for item in list(remaining_work)[:256]],
        "availability": _bounded_map(availability),
        "boundaries": [str(item)[:256] for item in list(boundaries)[:64]],
        "read_context": [],
    }
    if len(source_sha) != 40 or len(workspace_fingerprint) != 64:
        raise ValueError("ContextPack requires exact source and workspace bindings")
    for observation in list(read_context)[:64]:
        safe = {
            key: str(observation[key])[:4000]
            for key in ("path", "content_sha256", "source_generation", "redacted_excerpt")
            if key in observation and not any(word in key.lower() for word in _FORBIDDEN_KEYS)
        }
        candidate = {**pack, "read_context": [*pack["read_context"], safe]}
        if len(json.dumps(candidate, sort_keys=True).encode("utf-8")) > MAX_CONTEXT_PACK_BYTES - 100:
            break
        pack = candidate
    encoded = json.dumps(pack, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_PACK_BYTES - 100:
        raise ValueError("mandatory ContextPack fields exceed durable bound")
    pack["context_fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return pack


def handoff_seed(context_pack: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "completed_work_unit_ids": list(context_pack.get("completed_work_unit_ids", [])),
        "completed_work_unit_signatures": dict(context_pack.get("completed_work_unit_signatures", {})),
        "artifact_hashes": dict(context_pack.get("artifact_hashes", {})),
        "superseded_session_ids": list(context_pack.get("superseded_session_ids", [])),
    }
