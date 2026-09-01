"""Deterministic rehearsal evidence provenance validation module for AOS."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aos.validate import load_json_strict, validate_document

EXECUTION_CLAIM_CLASSES = {
    "DISPOSABLE_EXECUTION",
    "RUNTIME_EXECUTION",
    "DEPLOYMENT_EXECUTION",
    "MIGRATION_EXECUTION",
    "CLEANUP_EXECUTION",
    "ROLLBACK_EXECUTION",
}

NON_EXECUTION_CLAIM_CLASSES = {
    "STATIC_INSPECTION",
    "PLANNING",
    "AUTHORITY_DECISION",
}

VALID_CLAIM_CLASSES = EXECUTION_CLAIM_CLASSES | NON_EXECUTION_CLAIM_CLASSES

VALID_COMPONENT_ORIGINS = {
    "AOS_CORE",
    "REHEARSAL_HARNESS",
    "EXTERNAL_TOOL",
    "CONCEPTUAL",
}


class RehearsalEvidenceValidationError:
    """Error details for rehearsal evidence validation."""

    def __init__(self, code: str, message: str, step_id: Optional[str] = None):
        self.code = code
        self.message = message
        self.step_id = step_id

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.step_id is not None:
            res["step_id"] = self.step_id
        return res

    def __str__(self) -> str:
        prefix = f"[Step '{self.step_id}'] " if self.step_id else ""
        return f"{prefix}{self.code}: {self.message}"


class RehearsalEvidenceValidationResult:
    """Result of rehearsal report validation."""

    def __init__(
        self,
        is_valid: bool,
        derived_classification: str,
        errors: List[RehearsalEvidenceValidationError],
    ):
        self.is_valid = is_valid
        self.derived_classification = derived_classification
        self.errors = errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "derived_classification": self.derived_classification,
            "errors": [e.to_dict() for e in self.errors],
        }


def validate_rehearsal_report(
    report: Dict[str, Any],
    repo_root: Optional[Path | str] = None,
) -> RehearsalEvidenceValidationResult:
    """Deterministically validate rehearsal report schema, component provenance, and derive classification."""
    errors: List[RehearsalEvidenceValidationError] = []

    # 1. Validate JSON schema
    schema_validation = validate_document("rehearsal_report", report)
    if not schema_validation.is_valid:
        for err in schema_validation.errors:
            errors.append(
                RehearsalEvidenceValidationError(
                    code="SCHEMA_INVALID",
                    message=str(err),
                )
            )
        return RehearsalEvidenceValidationResult(
            is_valid=False,
            derived_classification="FAIL",
            errors=errors,
        )

    resolved_repo_root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    steps = report.get("steps", [])

    has_fail = False
    has_required_not_executed = False

    # 2. Validate individual steps
    for step in steps:
        step_id = step.get("step_id", "UNKNOWN")
        claim_class = step.get("claim_class")
        required_for_pass = step.get("required_for_pass_candidate", True)
        status = step.get("status")
        component = step.get("component", {})
        origin = component.get("origin")
        provenance = step.get("execution_provenance", {})
        executed = provenance.get("executed", False)
        blocker = step.get("blocker")

        # Track status
        if status == "FAIL":
            has_fail = True
        elif status == "NOT_EXECUTED" and required_for_pass:
            has_required_not_executed = True

        # Invariant A: NOT_EXECUTED requires executed=False and non-empty blocker
        if status == "NOT_EXECUTED":
            if executed:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="EXECUTION_PASS_WITHOUT_EXECUTION",
                        message="Status NOT_EXECUTED cannot have executed=true",
                        step_id=step_id,
                    )
                )
                has_fail = True
            if not blocker or not str(blocker).strip():
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="NOT_EXECUTED_WITHOUT_BLOCKER",
                        message="Status NOT_EXECUTED requires non-empty blocker reason",
                        step_id=step_id,
                    )
                )
                has_fail = True

        # Invariant B: Execution claim classes require executed=true for status=PASS
        if claim_class in EXECUTION_CLAIM_CLASSES:
            if status == "PASS" and not executed:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="EXECUTION_PASS_WITHOUT_EXECUTION",
                        message=f"Claim class '{claim_class}' with status=PASS requires executed=true",
                        step_id=step_id,
                    )
                )
                has_fail = True

        # Invariant C: CONCEPTUAL component cannot claim execution PASS
        if origin == "CONCEPTUAL":
            if claim_class in EXECUTION_CLAIM_CLASSES and status == "PASS":
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="CONCEPTUAL_EXECUTION_PASS_FORBIDDEN",
                        message=f"CONCEPTUAL component cannot claim status=PASS for execution claim class '{claim_class}'",
                        step_id=step_id,
                    )
                )
                has_fail = True

        # Invariant D: Evidence class inflation checks
        # Static inspection cannot yield PASS for execution claim classes
        if claim_class in EXECUTION_CLAIM_CLASSES and not executed and status == "PASS":
            errors.append(
                RehearsalEvidenceValidationError(
                    code="EVIDENCE_CLASS_INFLATION",
                    message=f"Cannot claim status=PASS for '{claim_class}' without actual execution evidence",
                    step_id=step_id,
                )
            )
            has_fail = True

        # Invariant E: Authority Decision Match
        if claim_class == "AUTHORITY_DECISION":
            exp_dec = step.get("expected_decision")
            obs_dec = step.get("observed_decision")
            if exp_dec != obs_dec:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="AUTHORITY_DECISION_MISMATCH",
                        message=f"Expected authority decision '{exp_dec}' != observed authority decision '{obs_dec}'",
                        step_id=step_id,
                    )
                )
                has_fail = True
                if status == "PASS":
                    errors.append(
                        RehearsalEvidenceValidationError(
                            code="AUTHORITY_DECISION_MISMATCH",
                            message="Status cannot be PASS when expected and observed decisions differ",
                            step_id=step_id,
                        )
                    )

        # Invariant F: Component Origin Verification
        if origin == "AOS_CORE":
            mod_name = component.get("module")
            sym_name = component.get("symbol")
            source_path_str = component.get("source_path")
            source_sha = component.get("source_sha256")

            if not mod_name or not sym_name or not source_path_str or not source_sha:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="CORE_MODULE_UNRESOLVED",
                        message="AOS_CORE component requires module, symbol, source_path, and source_sha256",
                        step_id=step_id,
                    )
                )
                has_fail = True
            else:
                # 1. Verify module is part of AOS
                if not (mod_name == "aos" or mod_name.startswith("aos.")):
                    errors.append(
                        RehearsalEvidenceValidationError(
                            code="CORE_MODULE_UNRESOLVED",
                            message=f"Module '{mod_name}' is not part of canonical 'aos' package",
                            step_id=step_id,
                        )
                    )
                    has_fail = True
                else:
                    # Try importing module
                    try:
                        mod = importlib.import_module(mod_name)
                    except ImportError as e:
                        errors.append(
                            RehearsalEvidenceValidationError(
                                code="CORE_MODULE_UNRESOLVED",
                                message=f"Failed to import AOS module '{mod_name}': {e}",
                                step_id=step_id,
                            )
                        )
                        mod = None
                        has_fail = True

                    if mod is not None:
                        # 2. Verify symbol exists
                        if not hasattr(mod, sym_name):
                            errors.append(
                                RehearsalEvidenceValidationError(
                                    code="CORE_SYMBOL_UNRESOLVED",
                                    message=f"Symbol '{sym_name}' not found in module '{mod_name}'",
                                    step_id=step_id,
                                )
                            )
                            has_fail = True
                        else:
                            # 3. Resolve source file path
                            try:
                                real_source_file = Path(inspect.getsourcefile(mod) or inspect.getfile(mod)).resolve()
                            except Exception as e:
                                errors.append(
                                    RehearsalEvidenceValidationError(
                                        code="CORE_MODULE_UNRESOLVED",
                                        message=f"Cannot inspect source file for module '{mod_name}': {e}",
                                        step_id=step_id,
                                    )
                                )
                                real_source_file = None
                                has_fail = True

                            if real_source_file:
                                # 4. Check source inside repo
                                try:
                                    rel_path = real_source_file.relative_to(resolved_repo_root)
                                    rel_path_posix = rel_path.as_posix()
                                except ValueError:
                                    errors.append(
                                        RehearsalEvidenceValidationError(
                                            code="CORE_SOURCE_OUTSIDE_REPO",
                                            message=f"Source file '{real_source_file}' is outside repository root '{resolved_repo_root}'",
                                            step_id=step_id,
                                        )
                                    )
                                    rel_path_posix = None
                                    has_fail = True

                                if rel_path_posix is not None:
                                    # 5. Check path matches claimed path
                                    claimed_path_posix = Path(source_path_str).as_posix()
                                    if rel_path_posix != claimed_path_posix:
                                        errors.append(
                                            RehearsalEvidenceValidationError(
                                                code="CORE_SOURCE_PATH_MISMATCH",
                                                message=f"Claimed source path '{claimed_path_posix}' != actual resolved path '{rel_path_posix}'",
                                                step_id=step_id,
                                            )
                                        )
                                        has_fail = True

                                    # 6. Check source file hash
                                    actual_sha = hashlib.sha256(real_source_file.read_bytes()).hexdigest()
                                    if actual_sha != source_sha.lower():
                                        errors.append(
                                            RehearsalEvidenceValidationError(
                                                code="CORE_SOURCE_HASH_MISMATCH",
                                                message=f"Claimed source SHA256 '{source_sha}' != actual SHA256 '{actual_sha}'",
                                                step_id=step_id,
                                            )
                                        )
                                        has_fail = True

        elif origin == "REHEARSAL_HARNESS":
            name = component.get("name")
            desc = component.get("description")
            source_path = component.get("source_path")
            source_sha = component.get("source_sha256")

            if not name or not (desc or source_path):
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="HARNESS_IDENTITY_INVALID",
                        message="REHEARSAL_HARNESS component requires name and either description or source_path",
                        step_id=step_id,
                    )
                )
                has_fail = True
            elif source_path:
                harness_file = resolved_repo_root / source_path
                if harness_file.exists() and harness_file.is_file() and source_sha:
                    actual_sha = hashlib.sha256(harness_file.read_bytes()).hexdigest()
                    if actual_sha != source_sha.lower():
                        errors.append(
                            RehearsalEvidenceValidationError(
                                code="HARNESS_IDENTITY_INVALID",
                                message=f"Harness source SHA256 '{source_sha}' != actual file SHA256 '{actual_sha}'",
                                step_id=step_id,
                            )
                        )
                        has_fail = True

        elif origin == "EXTERNAL_TOOL":
            name = component.get("name")
            if not name:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="EXTERNAL_TOOL_IDENTITY_INVALID",
                        message="EXTERNAL_TOOL component requires name",
                        step_id=step_id,
                    )
                )
                has_fail = True

        elif origin == "CONCEPTUAL":
            name = component.get("name")
            if not name:
                errors.append(
                    RehearsalEvidenceValidationError(
                        code="CONCEPTUAL_IDENTITY_INVALID",
                        message="CONCEPTUAL component requires name",
                        step_id=step_id,
                    )
                )
                has_fail = True

    # 3. Derive top-level classification
    if has_fail:
        derived_classification = "FAIL"
    elif has_required_not_executed:
        derived_classification = "HOLD_CAPABILITY_NOT_EXECUTED"
    else:
        derived_classification = "PASS_CANDIDATE"

    # 4. Check caller-supplied classification
    report_classification = report.get("top_level_classification")
    if report_classification != derived_classification:
        errors.append(
            RehearsalEvidenceValidationError(
                code="TOP_LEVEL_CLASSIFICATION_MISMATCH",
                message=f"Claimed top-level classification '{report_classification}' != deterministically derived classification '{derived_classification}'",
            )
        )
        is_valid = False
    else:
        is_valid = (len(errors) == 0)

    return RehearsalEvidenceValidationResult(
        is_valid=is_valid,
        derived_classification=derived_classification,
        errors=errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS Rehearsal Evidence Provenance CLI Validator")
    parser.add_argument("report_path", type=Path, help="Path to rehearsal report JSON file")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Path to canonical AOS repository root")
    args = parser.parse_args()

    try:
        report_data = load_json_strict(args.report_path)
    except Exception as e:
        print(f"Error loading JSON report file: {e}", file=sys.stderr)
        sys.exit(1)

    result = validate_rehearsal_report(report_data, repo_root=args.repo_root)

    print(json.dumps(result.to_dict(), indent=2))
    if not result.is_valid:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
