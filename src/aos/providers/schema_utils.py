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
# Ambiguous mappings (such as exact_text, text_matches, url_exact, path_exact) are NOT permitted.
# route/url aliases only apply to route-like targets; full URLs fail closed.
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
}

# Explicitly recognized transport fallback keys that may be mapped to target
_RECOGNIZED_TRANSPORT_FALLBACK_KEYS = ("selector", "path")


def normalize_assertion_dict(assertion: Any) -> Any:
    """Deterministically normalize assertion dictionary aliases while preserving semantics.
    
    1. Unknown fields (e.g. negate, operator, expected, count) are NOT silently discarded;
       they are preserved so Draft202012Validator fails closed on additionalProperties.
    2. Conflicting target representations (e.g. both 'target' and 'selector' with different values)
       are preserved to fail closed or raise validation mismatch.
    3. Ambiguous mappings (exact_text, text_matches, url_exact, path_exact) are rejected.
    4. Full URLs (http://, https://) in route assertions are not normalized into route-only checks.
    """
    if not isinstance(assertion, dict):
        return assertion
    norm = dict(assertion)
    raw_type = norm.get("type")
    canonical_type = None
    if isinstance(raw_type, str):
        cleaned = raw_type.strip().lower()
        if cleaned in CANONICAL_ASSERTION_TYPES:
            canonical_type = cleaned
            norm["type"] = cleaned
        elif cleaned in ASSERTION_TYPE_ALIASES:
            canonical_type = ASSERTION_TYPE_ALIASES[cleaned]
            norm["type"] = canonical_type

    # Detect conflicting target representations when both target and a fallback are provided
    target_val = norm.get("target")
    has_target = target_val is not None
    fallback_key_used = None

    for fallback_key in _RECOGNIZED_TRANSPORT_FALLBACK_KEYS:
        if fallback_key in norm and norm[fallback_key] is not None:
            if has_target:
                # If both target and fallback are present, check for conflict
                if str(norm[fallback_key]).strip() != str(target_val).strip():
                    # Conflicting values: leave both or mark conflicting to fail closed
                    # Leaving both intact will cause additionalProperties failure or schema failure
                    pass
                else:
                    # Identical values: safe to remove the redundant fallback transport key
                    norm.pop(fallback_key, None)
            else:
                # Target absent: map fallback key to target
                norm["target"] = norm[fallback_key]
                fallback_key_used = fallback_key
                break

    if fallback_key_used:
        norm.pop(fallback_key_used, None)

    # If the assertion type is route or route_exact, ensure full URLs (with scheme) fail closed
    # rather than being silently evaluated as route-only checks
    effective_type = norm.get("type")
    effective_target = norm.get("target")
    if effective_type in ("route", "route_exact") and isinstance(effective_target, str):
        target_str = effective_target.strip().lower()
        if target_str.startswith("http://") or target_str.startswith("https://"):
            # A full URL is not a valid route target. Invalidate type to fail closed under schema enum
            norm["type"] = "unsupported_full_url_route"

    return norm


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
