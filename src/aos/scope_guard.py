"""Deterministic Scope Guard module for AOS-3."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ScopeValidationResult:
    """Result of scope validation on worker changes."""

    def __init__(
        self,
        is_valid: bool,
        allowed_paths: List[str],
        forbidden_paths: List[str],
        violations: List[str],
    ):
        self.is_valid = is_valid
        self.allowed_paths = allowed_paths
        self.forbidden_paths = forbidden_paths
        self.violations = violations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "allowed_paths": self.allowed_paths,
            "forbidden_paths": self.forbidden_paths,
            "violations": self.violations,
        }


def validate_scope(
    changed_paths: List[str],
    allowed_scope: Dict[str, Any],
) -> ScopeValidationResult:
    """Validate that all changed file paths strictly comply with allowed_scope requirements."""
    allowed_prefixes = [p.replace("\\", "/").rstrip("/") + "/" for p in allowed_scope.get("paths", [])]
    allowed_exact = [p.replace("\\", "/") for p in allowed_scope.get("paths", [])]

    forbidden_prefixes = [p.replace("\\", "/").rstrip("/") + "/" for p in allowed_scope.get("forbidden_paths", [])]
    forbidden_exact = [p.replace("\\", "/") for p in allowed_scope.get("forbidden_paths", [])]

    violations: List[str] = []

    for path in changed_paths:
        clean_path = path.replace("\\", "/")

        # 1. Check forbidden paths first
        is_forbidden = False
        for f_prefix in forbidden_prefixes:
            if clean_path.startswith(f_prefix):
                is_forbidden = True
                break
        if not is_forbidden and clean_path in forbidden_exact:
            is_forbidden = True

        if is_forbidden:
            violations.append(f"Path '{clean_path}' is explicitly forbidden by allowed_scope")
            continue

        # 2. Check allowed paths
        is_allowed = False
        for a_prefix in allowed_prefixes:
            if clean_path.startswith(a_prefix):
                is_allowed = True
                break
        if not is_allowed and clean_path in allowed_exact:
            is_allowed = True

        if not is_allowed:
            violations.append(f"Path '{clean_path}' is outside permitted allowed_scope.paths")

    return ScopeValidationResult(
        is_valid=len(violations) == 0,
        allowed_paths=allowed_scope.get("paths", []),
        forbidden_paths=allowed_scope.get("forbidden_paths", []),
        violations=violations,
    )
