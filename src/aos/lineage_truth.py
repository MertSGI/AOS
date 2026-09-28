"""Generic durable command-lineage classification for runtime projections."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping


CURRENT = "CURRENT"
SUPERSEDED = "SUPERSEDED"
HISTORICAL = "HISTORICAL"
UNKNOWN = "UNKNOWN"
LINEAGE_STATUSES = {CURRENT, SUPERSEDED, HISTORICAL}

ACTIVE_LINEAGE_STATES = {
    "RUNNING",
    "RECOVERING",
    "EXECUTING",
    "WAITING_FOR_REASONING_PROVIDER",
    "WAITING_FOR_SOURCE_TRANSPORT",
    "QUEUED",
    "TECHNICAL_HOLD",
}


def durable_lineage_status(
    command: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[str, str]:
    """Read explicit lineage truth without treating runtime state as authority."""
    for source_name, source in (("state", state), ("command", command)):
        raw = source.get("lineage_status") or source.get("lineage_state")
        normalized = str(raw or "").strip().upper()
        if normalized in LINEAGE_STATUSES:
            return normalized, f"EXPLICIT_{source_name.upper()}"
    if state.get("superseded_by_command_id") or command.get("superseded_by_command_id"):
        return SUPERSEDED, "EXPLICIT_SUPERSESSION_RELATION"
    if bool(state.get("historical") or command.get("historical")):
        return HISTORICAL, "EXPLICIT_HISTORICAL_FLAG"
    return UNKNOWN, "LEGACY_UNCLASSIFIED"


def _timestamp(value: Any) -> float:
    try:
        normalized = str(value or "").strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def resolve_current_lineages(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Project explicit lineage truth, with a bounded legacy fallback.

    Explicit CURRENT/SUPERSEDED/HISTORICAL metadata wins. For legacy records,
    active non-terminal commands remain current. A terminal legacy command is
    current only when its project has no explicit or active current command;
    then the most recent durable terminal record is selected.
    """
    items = [dict(record) for record in records]
    resolved: Dict[str, Dict[str, str]] = {}
    by_project: Dict[str, list[Dict[str, Any]]] = {}
    for item in items:
        command_id = str(item.get("command_id") or "")
        project_id = str(item.get("project_id") or "UNKNOWN")
        status = str(item.get("lineage_status") or UNKNOWN).upper()
        basis = str(item.get("lineage_status_basis") or "LEGACY_UNCLASSIFIED")
        if status in LINEAGE_STATUSES:
            resolved[command_id] = {"status": status, "basis": basis}
        by_project.setdefault(project_id, []).append(item)

    for project_items in by_project.values():
        has_current = any(
            resolved.get(str(item.get("command_id") or ""), {}).get("status") == CURRENT
            for item in project_items
        )
        legacy_active = [
            item for item in project_items
            if str(item.get("lineage_status") or UNKNOWN).upper() == UNKNOWN
            and str(item.get("state") or "").upper() in ACTIVE_LINEAGE_STATES
        ]
        for item in legacy_active:
            command_id = str(item.get("command_id") or "")
            resolved[command_id] = {
                "status": CURRENT,
                "basis": "INFERRED_ACTIVE_LEGACY_LINEAGE",
            }
        has_current = has_current or bool(legacy_active)

        legacy_terminal = [
            item for item in project_items
            if str(item.get("lineage_status") or UNKNOWN).upper() == UNKNOWN
            and str(item.get("state") or "").upper() not in ACTIVE_LINEAGE_STATES
        ]
        selected_terminal_id = None
        if legacy_terminal and not has_current:
            selected = max(
                legacy_terminal,
                key=lambda item: (
                    _timestamp(item.get("updated_at") or item.get("created_at")),
                    str(item.get("command_id") or ""),
                ),
            )
            selected_terminal_id = str(selected.get("command_id") or "")
        for item in legacy_terminal:
            command_id = str(item.get("command_id") or "")
            resolved[command_id] = {
                "status": CURRENT if command_id == selected_terminal_id else HISTORICAL,
                "basis": (
                    "INFERRED_MOST_RECENT_LEGACY_LINEAGE"
                    if command_id == selected_terminal_id
                    else "INFERRED_SUPERSEDED_LEGACY_LINEAGE"
                ),
            }
    return resolved
