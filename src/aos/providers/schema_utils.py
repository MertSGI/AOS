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


# Canonical allowed assertion types strictly defined in PLAN_SCHEMA
CANONICAL_ASSERTION_TYPES = frozenset({
    "selector_visible",
    "selector",
    "text_visible",
    "text",
    "route_exact",
    "route",
})

# Explicitly recognized, semantics-preserving assertion type aliases.
# Strictly preserves intent; unknown or ambiguous types are never mapped and fail closed.
ASSERTION_TYPE_ALIASES = {
    "visible": "selector_visible",
    "is_visible": "selector_visible",
    "element_visible": "selector_visible",
    "exists": "selector",
    "element": "selector",
    "element_exists": "selector",
    "css": "selector",
    "contains_text": "text_visible",
    "has_text": "text_visible",
    "text_matches": "text",
    "exact_text": "text",
    "url": "route",
    "path": "route",
    "url_exact": "route_exact",
    "path_exact": "route_exact",
}


def normalize_assertion_dict(assertion: Any) -> Any:
    """Deterministically normalize assertion dictionary aliases while preserving semantics."""
    if not isinstance(assertion, dict):
        return assertion
    norm = dict(assertion)
    raw_type = norm.get("type")
    if isinstance(raw_type, str):
        cleaned = raw_type.strip().lower()
        if cleaned in CANONICAL_ASSERTION_TYPES:
            norm["type"] = cleaned
        elif cleaned in ASSERTION_TYPE_ALIASES:
            norm["type"] = ASSERTION_TYPE_ALIASES[cleaned]
    # If model used selector/url/path instead of target, normalize to target if target is absent
    if "target" not in norm or norm.get("target") is None:
        for fallback_key in ("selector", "url", "path"):
            if fallback_key in norm and norm[fallback_key] is not None:
                norm["target"] = norm[fallback_key]
                break
    # Strictly conform to PLAN_SCHEMA assertion properties: {"type", "target", "description"}
    # Drop model-invented keys like 'selector', 'url', 'path' once target has been populated
    allowed_keys = {"type", "target", "description"}
    return {k: v for k, v in norm.items() if k in allowed_keys}


def sanitize_planner_output(data: Any, schema: Any) -> Any:
    """Prune only optional invalid nulls and normalize safe aliases while preserving required/allowed nulls."""
    if isinstance(data, dict):
        properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
        required = set(schema.get("required", [])) if isinstance(schema, Mapping) else set()
        normalized = {}
        for key, value in data.items():
            property_schema = properties.get(key, {}) if isinstance(properties, Mapping) else {}
            if value is None and key not in required and not _allows_null(property_schema):
                continue
            # If key is assertions in task payload, normalize each assertion item
            if key == "assertions" and isinstance(value, list):
                normalized_list = []
                for item in value:
                    item_norm = normalize_assertion_dict(item)
                    item_schema = property_schema.get("items", {}) if isinstance(property_schema, Mapping) else {}
                    normalized_list.append(sanitize_planner_output(item_norm, item_schema))
                normalized[key] = normalized_list
                continue
            normalized[key] = sanitize_planner_output(value, property_schema)
        return normalized
    if isinstance(data, list):
        item_schema = schema.get("items", {}) if isinstance(schema, Mapping) else {}
        return [sanitize_planner_output(item, item_schema) for item in data]
    return data
