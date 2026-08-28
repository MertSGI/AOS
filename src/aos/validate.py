"""AOS deterministic schema validator module and CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "v0.1"

TYPE_TO_SCHEMA = {
    "state": "state.schema.json",
    "project_descriptor": "project_descriptor.schema.json",
    "task": "task.schema.json",
    "lease": "lease.schema.json",
    "evidence": "evidence.schema.json",
    "decision_event": "decision_event.schema.json",
    "escalation": "escalation.schema.json",
    "planner_decision": "planner_decision.schema.json",
    "shadow_trace": "shadow_trace.schema.json",
    "canonical_project_snapshot": "canonical_project_snapshot.schema.json",
    "shadow_expectation": "shadow_expectation.schema.json",
    "planner_routing_policy": "planner_routing_policy.schema.json",
    "control_request": "control_request.schema.json",
    "controlled_execution_result": "controlled_execution_result.schema.json",
    "worker_capability_attestation": "worker_capability_attestation.schema.json",
    "execution_authorization": "execution_authorization.schema.json",
}


class ValidationErrorDetails:
    def __init__(self, message: str, path: str, validator: str, validator_value: Any = None):
        self.message = message
        self.path = path
        self.validator = validator
        self.validator_value = validator_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "path": self.path,
            "validator": self.validator,
            "validator_value": self.validator_value,
        }

    def __str__(self) -> str:
        loc = f" at '{self.path}'" if self.path else ""
        return f"Error{loc}: {self.message}"


class ValidationResult:
    def __init__(self, is_valid: bool, doc_type: str, errors: List[ValidationErrorDetails] | None = None):
        self.is_valid = is_valid
        self.doc_type = doc_type
        self.errors = errors or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "doc_type": self.doc_type,
            "errors": [e.to_dict() for e in self.errors],
        }


class DuplicateJSONKeyError(ValueError):
    """Exception raised when a JSON object contains duplicate keys."""
    pass


def _reject_duplicate_object_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def loads_json_strict(text: str) -> Any:
    """Parse JSON string strictly rejecting duplicate keys at any nesting level."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_object_pairs)


def load_json_strict(path_or_file: str | Path | Any) -> Any:
    """Parse JSON file or file-like object strictly rejecting duplicate keys at any nesting level."""
    if isinstance(path_or_file, (str, Path)):
        with open(path_or_file, "r", encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=_reject_duplicate_object_pairs)
    return json.load(path_or_file, object_pairs_hook=_reject_duplicate_object_pairs)


def load_schema(schema_filename: str) -> Dict[str, Any]:
    schema_path = SCHEMA_DIR / schema_filename
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    return load_json_strict(schema_path)


def validate_document(doc_type: str, data: Any) -> ValidationResult:
    if doc_type not in TYPE_TO_SCHEMA:
        raise ValueError(
            f"Unknown document type '{doc_type}'. Supported types: {list(TYPE_TO_SCHEMA.keys())}"
        )

    schema_file = TYPE_TO_SCHEMA[doc_type]
    schema = load_schema(schema_file)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    errors: List[ValidationErrorDetails] = []
    for err in validator.iter_errors(data):
        json_path = "/".join([str(p) for p in err.absolute_path])
        errors.append(
            ValidationErrorDetails(
                message=err.message,
                path=json_path,
                validator=err.validator,
                validator_value=err.validator_value,
            )
        )

    return ValidationResult(is_valid=len(errors) == 0, doc_type=doc_type, errors=errors)


def validate_file(doc_type: str, file_path: str | Path) -> Tuple[ValidationResult, int]:
    path = Path(file_path)
    if not path.is_file():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return ValidationResult(False, doc_type, [ValidationErrorDetails(f"File not found: {path}", "", "file_exists")]), 2

    try:
        with open(path, "r", encoding="utf-8") as f:
            if path.suffix == ".jsonl":
                all_valid = True
                all_errors = []
                for physical_line_number, raw_line in enumerate(f, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        item = loads_json_strict(line)
                        res = validate_document(doc_type, item)
                        if not res.is_valid:
                            all_valid = False
                            for e in res.errors:
                                all_errors.append(
                                    ValidationErrorDetails(
                                        f"Line {physical_line_number}: {e.message}", e.path, e.validator, e.validator_value
                                    )
                                )
                    except DuplicateJSONKeyError as e:
                        all_valid = False
                        all_errors.append(
                            ValidationErrorDetails(
                                f"Line {physical_line_number}: {e}", "", "duplicate_json_key"
                            )
                        )
                return ValidationResult(all_valid, doc_type, all_errors), (0 if all_valid else 1)
            else:
                data = load_json_strict(path)
                res = validate_document(doc_type, data)
                return res, (0 if res.is_valid else 1)
    except DuplicateJSONKeyError as e:
        print(f"Duplicate JSON key error in {path}: {e}", file=sys.stderr)
        return ValidationResult(False, doc_type, [ValidationErrorDetails(str(e), "", "duplicate_json_key")]), 1
    except json.JSONDecodeError as jde:
        print(f"JSON decode error in {path}: {jde}", file=sys.stderr)
        return ValidationResult(False, doc_type, [ValidationErrorDetails(str(jde), "", "json_decode")]), 2
    except Exception as e:
        print(f"Unexpected error validating {path}: {e}", file=sys.stderr)
        return ValidationResult(False, doc_type, [ValidationErrorDetails(str(e), "", "exception")]), 2


def main(args: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic AOS Schema Validator")
    parser.add_argument(
        "type",
        choices=list(TYPE_TO_SCHEMA.keys()),
        help=f"Type of AOS document to validate ({', '.join(TYPE_TO_SCHEMA.keys())})",
    )
    parser.add_argument("file", help="Path to JSON or JSONL file to validate")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output result as machine-readable JSON",
    )

    parsed = parser.parse_args(args)
    result, code = validate_file(parsed.type, parsed.file)

    if parsed.json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        if result.is_valid:
            print(f"PASS: {parsed.file} is a valid {parsed.type}")
        else:
            print(f"FAIL: {parsed.file} is NOT a valid {parsed.type} ({len(result.errors)} errors):", file=sys.stderr)
            for err in result.errors:
                print(f"  - {err}", file=sys.stderr)

    return code


if __name__ == "__main__":
    sys.exit(main())
