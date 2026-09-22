"""Shared provider-output normalization that preserves canonical schema semantics."""
from __future__ import annotations

from typing import Any, Mapping


def _allows_null(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    declared_type = schema.get("type")
    if declared_type == "null":
        return True
    if isinstance(declared_type, list) and "null" in declared_type:
        return True
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and any(_allows_null(branch) for branch in branches):
            return True
    return False


def sanitize_planner_output(data: Any, schema: Any) -> Any:
    """Prune only optional invalid nulls while preserving required/allowed nulls."""
    if isinstance(data, dict):
        properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
        required = set(schema.get("required", [])) if isinstance(schema, Mapping) else set()
        normalized = {}
        for key, value in data.items():
            property_schema = properties.get(key, {}) if isinstance(properties, Mapping) else {}
            if value is None and key not in required and not _allows_null(property_schema):
                continue
            normalized[key] = sanitize_planner_output(value, property_schema)
        return normalized
    if isinstance(data, list):
        item_schema = schema.get("items", {}) if isinstance(schema, Mapping) else {}
        return [sanitize_planner_output(item, item_schema) for item in data]
    return data
