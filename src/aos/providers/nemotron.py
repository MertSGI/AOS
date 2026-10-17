"""NVIDIA Nemotron PlannerProvider implementation reusing the OpenAI Python SDK.

Aligned with NVIDIA Nemotron 3 Ultra hosted API contract:
- STRUCTURED_MODE: chat_template_kwargs={"enable_thinking": False}, response_format json_schema, temperature=0.0
- DEEP_REASONING_MODE: chat_template_kwargs={"enable_thinking": True}, reasoning_budget=<budget>
- Strict local JSON Schema validation fail-closed
- Raw reasoning / chain-of-thought stripped and never persisted
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.provider_observation import (
    ContractFailureSubtype,
    FailureFamily,
    extract_rate_limit_observation,
    safe_contract_detail,
)
from aos.providers.schema_utils import sanitize_planner_output as _sanitize_planner_output

UNSUPPORTED_NEMOTRON_KEYWORDS = {"$schema", "$id"}


def project_nemotron_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a provider schema projection by stripping meta-schema keywords for strict Structured Outputs."""
    if not isinstance(schema, dict):
        return schema

    projected: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_NEMOTRON_KEYWORDS:
            continue
        if isinstance(v, dict):
            projected[k] = project_nemotron_schema(v)
        elif isinstance(v, list):
            projected[k] = [project_nemotron_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            projected[k] = v
    return projected


NEMOTRON_MAX_OUTPUT_TOKENS = 2200
NEMOTRON_PLAN_MAX_OUTPUT_TOKENS = 6144
NEMOTRON_INTENT_MAX_OUTPUT_TOKENS = 800
NEMOTRON_OBJECTIVE_MAX_OUTPUT_TOKENS = 1000
NEMOTRON_COMPLETION_MAX_OUTPUT_TOKENS = 1000


def nemotron_max_output_tokens(schema: Dict[str, Any]) -> int:
    """Reserve output capacity proportionate to the known reasoning contract."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return NEMOTRON_MAX_OUTPUT_TOKENS
    keys = set(properties)
    if {"intent_type", "target_ref"}.issubset(keys):
        return NEMOTRON_INTENT_MAX_OUTPUT_TOKENS
    if {"objective_id", "parallel_candidates", "completion_criteria"}.issubset(keys) and "tasks" not in keys:
        return NEMOTRON_OBJECTIVE_MAX_OUTPUT_TOKENS
    if {"disposition", "satisfied_criteria", "unsatisfied_criteria"}.issubset(keys):
        return NEMOTRON_COMPLETION_MAX_OUTPUT_TOKENS
    if {"objective_id", "tasks", "parallel_safe_groups"}.issubset(keys):
        return NEMOTRON_PLAN_MAX_OUTPUT_TOKENS
    return NEMOTRON_MAX_OUTPUT_TOKENS


def nemotron_system_instruction(schema: Dict[str, Any]) -> str:
    """Determine schema-aware system instructions for reasoning tasks."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    keys = set(properties) if isinstance(properties, dict) else set()
    if {"intent_type", "target_ref"}.issubset(keys):
        return (
            "You are the AOS Execution Planner. Your task is to evaluate the project context and "
            "select ONE compact intent matching the provided schema. You may only select an exact "
            "objective_id, an allowed intent_type, a target_ref from the catalog, and a verification_ref "
            "where required. Output a single JSON object matching the schema."
        )
    if {"objective_id", "parallel_candidates", "completion_criteria"}.issubset(keys) and "tasks" not in keys:
        return (
            "You are the AOS Objective Planner. Your task is to evaluate canonical project control "
            "context and select the next canonical objective matching the provided schema."
        )
    if {"disposition", "satisfied_criteria", "unsatisfied_criteria"}.issubset(keys):
        return (
            "You are the AOS Completion Evaluator. Your task is to evaluate batch evidence against "
            "canonical completion criteria and return a completion assessment matching the provided schema."
        )
    if {"objective_id", "tasks", "parallel_safe_groups"}.issubset(keys):
        return (
            "You are the AOS Execution Planner. Your task is to produce a bounded execution plan "
            "for the selected objective matching the provided schema."
        )
    return (
        "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
        "context and output a bounded planner decision JSON matching the provided schema. "
        "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
        "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
    )


# Thinking budget policy bounds
MIN_THINKING_BUDGET = 128
MAX_THINKING_BUDGET = 32768


def validate_thinking_budget(budget: Any) -> int:
    """Validate reasoning_budget fail-closed.
    
    Budget must be an integer within [MIN_THINKING_BUDGET, MAX_THINKING_BUDGET].
    """
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError(f"Invalid thinking budget: must be an integer, got {type(budget).__name__}")
    if budget < MIN_THINKING_BUDGET or budget > MAX_THINKING_BUDGET:
        raise ValueError(
            f"Invalid thinking budget {budget}: out of policy bounds [{MIN_THINKING_BUDGET}, {MAX_THINKING_BUDGET}]"
        )
    return budget


class NemotronPlannerProvider:
    """PlannerProvider adapter for NVIDIA Nemotron using OpenAI-compatible API.
    
    Per Controller Audit R2:
    - Structured planner execution (generate_plan) strictly uses enable_thinking=False
    - Deep reasoning is separated from the structured planner pass
    - Errors are sanitized into bounded categories without leaking raw credentials or URLs
    """

    NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
    DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
    execution_provenance = "LIVE_EXTERNAL"

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        if os.environ.get("AOS_DETERMINISTIC_MODEL_BACKEND") == "1":
            req = set(schema.get("required", []))
            props = set(schema.get("properties", {}))
            p_lower = prompt.lower()

            # 1. COMPLETION_SCHEMA
            if "disposition" in req and "satisfied_criteria" in req:
                if "tax" in p_lower and ("wp-canary-tax" in p_lower or "tax.py" in p_lower) and ("recent_completed_batches" in p_lower or "completed" in p_lower or "history" in p_lower):
                    return {
                        "disposition": "PROJECT_COMPLETE",
                        "rationale": "All canary repair objectives verified complete.",
                        "satisfied_criteria": ["python test_discount.py passes", "python test_tax.py passes"],
                        "unsatisfied_criteria": [],
                    }, "det-comp-001", None
                return {
                    "disposition": "REPLAN",
                    "rationale": "Pending canary repair objectives.",
                    "satisfied_criteria": [],
                    "unsatisfied_criteria": ["python test_tax.py passes"],
                }, "det-comp-002", None

            # 2. OBJECTIVE_SCHEMA
            if "objective_id" in req and "title" in req and "risk_class" in req:
                if "replan_reason=initial" not in p_lower and ("wp-canary-discount" in p_lower or "discount" in p_lower):
                    return {
                        "objective_id": "WP-CANARY-TAX",
                        "title": "Repair tax calculation defect",
                        "description": "Repair tax calculation in src/tax.py and verify test_tax.py passes",
                        "authority_id": "AUTHORITY-CANARY-A",
                        "risk_class": "R0",
                        "rationale": "Repair tax calculation in src/tax.py",
                        "scope_tags": ["canary", "tax"],
                        "completion_criteria": ["python test_tax.py passes"],
                        "parallel_candidates": [],
                    }, "det-obj-002", None
                return {
                    "objective_id": "WP-CANARY-DISCOUNT",
                    "title": "Repair discount calculation defect",
                    "description": "Repair discount calculation in src/discount.py and verify test_discount.py passes",
                    "authority_id": "AUTHORITY-CANARY-A",
                    "risk_class": "R0",
                    "rationale": "Repair discount calculation in src/discount.py",
                    "scope_tags": ["canary", "discount"],
                    "completion_criteria": ["python test_discount.py passes"],
                    "parallel_candidates": [],
                }, "det-obj-001", None

            # 3. PLAN_INTENT_SCHEMA
            if "intent_type" in req and "target_ref" in req:
                target = "WP-CANARY-DISCOUNT"
                if "OBJECTIVE=" in prompt:
                    try:
                        obj_text = prompt.split("OBJECTIVE=", 1)[1].split("\nINTENT_CATALOG=", 1)[0].strip()
                        obj_dict = json.loads(obj_text)
                        target = obj_dict.get("objective_id", "WP-CANARY-DISCOUNT")
                    except Exception:
                        pass
                return {
                    "schema_version": "1.0.0",
                    "objective_id": target,
                    "intent_type": "IMPLEMENT_AUTHORIZED_WORK_PACKAGE",
                    "target_ref": target,
                }, "det-intent-001", None

            # 4. PLAN_SCHEMA (DAG fallback)
            if "tasks" in req and "parallel_safe_groups" in req:
                target = "WP-CANARY-DISCOUNT"
                if "OBJECTIVE=" in prompt:
                    try:
                        obj_text = prompt.split("OBJECTIVE=", 1)[1].split("\n", 1)[0].strip()
                        obj_dict = json.loads(obj_text)
                        target = obj_dict.get("objective_id", "WP-CANARY-DISCOUNT")
                    except Exception:
                        pass
                kind = "tax" if "tax" in target.lower() else "discount"
                node_id = f"TASK-AGENTIC-WP-{target.replace('-', '_')}"
                return {
                    "schema_version": "1.0.0",
                    "objective_id": target,
                    "tasks": [
                        {
                            "node_id": node_id,
                            "run_type": "FILE",
                            "authority_id": "AUTHORITY-CANARY-A",
                            "risk_class": "R0",
                            "mutating": True,
                            "dependencies": [],
                            "scope_tags": ["canary", kind],
                            "write_scope": [f"src/{kind}.py"],
                            "payload": {"action": "write_file"},
                            "expected_artifacts": [f"src/{kind}.py"],
                            "tests": [f"python test_{kind}.py"],
                            "evidence_requirements": ["test_pass"],
                            "completion_criteria": [f"python test_{kind}.py passes"],
                        }
                    ],
                    "parallel_safe_groups": [[node_id]],
                    "rollback_strategy": "none",
                }, "det-plan-001", None

            # 5. PEER_REVIEW / COUNCIL / PROBE
            if "verdict" in props:
                return {
                    "verdict": "AGREE",
                    "confidence": 1.0,
                    "rationale": "Deterministic offline agreement.",
                }, "det-rev-001", None

            if "success" in props or "status" in props:
                return {"status": "SUCCESS", "success": True}, "det-probe-001", None

        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise PlannerCredentialError("NVIDIA_API_KEY environment variable is missing")

        import openai
        client = openai.OpenAI(api_key=api_key, base_url=self.NVIDIA_BASE_URL)

        instructions = nemotron_system_instruction(schema)

        provider_schema = project_nemotron_schema(schema)

        # NVIDIA Nemotron 3 Ultra hosted contract:
        # Structured planner execution strictly disables deep reasoning (enable_thinking=False)
        extra_body: Dict[str, Any] = {
            "chat_template_kwargs": {"enable_thinking": False}
        }

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "planner_decision",
                        "strict": True,
                        "schema": provider_schema,
                    },
                },
                max_tokens=nemotron_max_output_tokens(schema),
                temperature=0.0,
                store=False,
                extra_body=extra_body,
            )
        except Exception as e:
            # Bounded sanitized error classification (no raw str(e) or headers leaked)
            if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
                raise PlannerTransientError(
                    "Nemotron transient error: TIMEOUT_OR_CONNECTION",
                    failure_family=(FailureFamily.TIMEOUT if isinstance(e, openai.APITimeoutError) else FailureFamily.NETWORK),
                    rate_limit_observation=extract_rate_limit_observation(
                        provider_id="nemotron", model_id=self.model,
                        task_class="structured_planning", exc=e,
                    ),
                ) from e
            elif isinstance(e, openai.RateLimitError):
                raise PlannerTransientError(
                    "Nemotron transient error: RATE_LIMIT",
                    failure_family=FailureFamily.SERVER_CAPACITY,
                    rate_limit_observation=extract_rate_limit_observation(
                        provider_id="nemotron", model_id=self.model,
                        task_class="structured_planning", exc=e,
                    ),
                ) from e
            elif isinstance(e, openai.InternalServerError):
                raise PlannerTransientError("Nemotron transient server error: INTERNAL_SERVER_ERROR", failure_family=FailureFamily.SERVER_CAPACITY) from e
            elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                raise PlannerCredentialError("Nemotron auth/permission failure: AUTH_OR_PERMISSION_DENIED") from e
            elif isinstance(e, openai.BadRequestError):
                raise PlannerContractError(
                    "Nemotron invalid request/schema: BAD_REQUEST",
                    subtype=ContractFailureSubtype.BAD_REQUEST,
                    safe_detail=safe_contract_detail(exception_class=e.__class__.__name__, http_status=getattr(e, "status_code", None)),
                ) from e
            elif isinstance(e, openai.APIStatusError) and getattr(e, "status_code", 0) >= 500:
                raise PlannerTransientError("Nemotron transient server error: SERVER_STATUS_ERROR", failure_family=FailureFamily.SERVER_CAPACITY) from e
            else:
                err_str = str(e).lower()
                if any(w in err_str for w in ("timeout", "connection", "rate", "429", "500", "502", "503", "504")):
                    raise PlannerTransientError("Nemotron transient error: NETWORK_OR_SERVER", failure_family=FailureFamily.NETWORK) from e
                raise PlannerContractError(
                    "Nemotron provider contract failure: PROVIDER_CONTRACT_ERROR",
                    safe_detail=safe_contract_detail(exception_class=e.__class__.__name__, http_status=getattr(e, "status_code", None)),
                ) from e

        if not response.choices:
            raise PlannerContractError(
                "Nemotron returned no choices",
                subtype=ContractFailureSubtype.NO_CHOICES,
                safe_detail=safe_contract_detail(choice_count=0),
            )

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            truncation_err = PlannerTransientError("Nemotron response reached configured output capacity before completing JSON")
            u = getattr(response, "usage", None)
            truncation_err.safe_detail = safe_contract_detail(
                finish_reason="length",
                output_tokens=getattr(u, "completion_tokens", None) if u else None,
                total_tokens=getattr(u, "total_tokens", None) if u else None,
            )
            raise truncation_err
        if finish_reason and finish_reason != "stop":
            raise PlannerContractError(
                f"Nemotron response finished with unacceptable reason: {finish_reason}",
                subtype=ContractFailureSubtype.BAD_FINISH_REASON,
                safe_detail=safe_contract_detail(finish_reason=finish_reason),
            )

        content_str = getattr(choice.message, "content", None)
        if not content_str:
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise PlannerContractError("Nemotron model refused response", subtype=ContractFailureSubtype.REFUSAL)
            raise PlannerContractError("Nemotron returned empty content", subtype=ContractFailureSubtype.EMPTY_CONTENT)

        # INVARIANT: Raw reasoning/chain-of-thought content is strictly dropped and never returned or serialized
        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(
                "Nemotron output is not valid JSON",
                subtype=ContractFailureSubtype.INVALID_JSON,
                safe_detail=safe_contract_detail(parser_class=e.__class__.__name__, line=getattr(e, "lineno", None), column=getattr(e, "colno", None)),
            ) from e

        parsed_decision = _sanitize_planner_output(parsed_decision, schema)

        # Local schema validation fail-closed (JSON parse alone is NOT schema validation)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = list(validator.iter_errors(parsed_decision))
        if errors:
            error = errors[0]
            pointer = "/" + "/".join(str(item) for item in error.absolute_path) if error.absolute_path else "/"

            # Bounded correction attempt: allow at most ONE retry providing exact enum and validator feedback
            # Check if this error is an enum or schema constraint failure that can be corrected
            repaired = False
            try:
                allowed_enums_hint = ""
                if "assertions" in pointer and error.validator == "enum":
                    allowed_enums_hint = " Allowed assertion types are exactly: ['selector_visible', 'selector', 'text_visible', 'text', 'route_exact', 'route']."
                repair_instructions = (
                    f"{instructions}\n"
                    f"SCHEMA_CORRECTION_REQUIRED: Your previous response failed schema validation at '{pointer}' "
                    f"with error '{error.message}'.{allowed_enums_hint} "
                    "You MUST correct this schema error and return the full corrected JSON strictly adhering to PLAN_SCHEMA."
                )
                repair_response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": repair_instructions},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "planner_decision",
                            "strict": True,
                            "schema": provider_schema,
                        },
                    },
                    max_tokens=nemotron_max_output_tokens(schema),
                    temperature=0.0,
                    store=False,
                    extra_body=extra_body,
                )
                if repair_response.choices:
                    repair_choice = repair_response.choices[0]
                    repair_content = getattr(repair_choice.message, "content", None)
                    if repair_content:
                        repaired_decision = json.loads(repair_content)
                        repaired_decision = _sanitize_planner_output(repaired_decision, schema)
                        repair_errors = list(validator.iter_errors(repaired_decision))
                        if not repair_errors:
                            parsed_decision = repaired_decision
                            response = repair_response
                            repaired = True
            except Exception:
                repaired = False

            if not repaired:
                raise PlannerContractError(
                    "Nemotron output failed canonical JSON schema validation",
                    subtype=ContractFailureSubtype.SCHEMA_VALIDATION,
                    safe_detail=safe_contract_detail(validator_keyword=error.validator, json_pointer=pointer),
                )

        response_id = getattr(response, "id", None)

        usage_data = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage_data = {
                "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "cached_input_tokens": 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }

        return parsed_decision, response_id, usage_data
