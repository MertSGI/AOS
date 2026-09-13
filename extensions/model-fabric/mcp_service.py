"""AOS Nemotron MCP Service for Workspace Antigravity Integration.

Exposes bounded advisory tools over MCP standard protocol.
NO ACTUATORS: strictly no shell execution, file write, Git mutation, or deploy authorities.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from extensions.model_fabric.specialist_fabric import (
    NemotronSpecialistFabric,
    SpecialistRequest,
    SpecialistRole,
)


class NemotronMcpServer:
    """Bounded MCP Service exposing specialist advisory capabilities to Antigravity."""

    BOUNDED_TOOLS = [
        {
            "name": "nemotron_plan",
            "description": "Advisory critique on project execution plans and task sequences",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The plan details to review"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_architecture_review",
            "description": "Architectural critique and pattern assessment",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Architecture specification or design"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_code_review",
            "description": "Advisory static code review for quality and structural correctness",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Code snippet or diff to review"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_security_review",
            "description": "Advisory security evaluation for RLS, tenant isolation, and privilege escalation risks",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Security boundaries, RLS policies, or auth logic"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_sql_schema_review",
            "description": "Advisory database schema, foreign key, and index analysis",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "SQL DDL, migration, or schema definition"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_long_context_analysis",
            "description": "Large-context synthesis across multi-file documents and evidence logs",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Document text or log aggregations"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_design_text_review",
            "description": "Advisory text-based design critique (DOM semantics, CSS architecture, copy)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "DOM, CSS, or Design DNA text"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "nemotron_second_opinion",
            "description": "Independent second opinion on complex engineering or design trade-offs",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Decision statement and alternatives"},
                    "data_classification": {"type": "string", "default": "PUBLIC"},
                },
                "required": ["prompt"],
            },
        },
    ]

    TOOL_ROLE_MAP = {
        "nemotron_plan": SpecialistRole.PLAN_CRITIC,
        "nemotron_architecture_review": SpecialistRole.ARCHITECTURE_REVIEW,
        "nemotron_code_review": SpecialistRole.CODE_REVIEW,
        "nemotron_security_review": SpecialistRole.SECURITY_REVIEW,
        "nemotron_sql_schema_review": SpecialistRole.SQL_SCHEMA_REVIEW,
        "nemotron_long_context_analysis": SpecialistRole.LONG_CONTEXT_ANALYSIS,
        "nemotron_design_text_review": SpecialistRole.DESIGN_TEXT_CRITIC,
        "nemotron_second_opinion": SpecialistRole.SECOND_OPINION,
    }

    def __init__(self, fabric: Optional[NemotronSpecialistFabric] = None):
        self.fabric = fabric or NemotronSpecialistFabric()

    def list_tools(self) -> List[Dict[str, Any]]:
        return self.BOUNDED_TOOLS

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self.TOOL_ROLE_MAP:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Tool '{name}' is not in the allowed Nemotron tool registry"}],
            }

        role = self.TOOL_ROLE_MAP[name]
        prompt = arguments.get("prompt", "")
        data_classification = arguments.get("data_classification", "PUBLIC")

        req = SpecialistRequest(
            role=role,
            prompt=prompt,
            data_classification=data_classification,
        )

        resp = self.fabric.evaluate(req)

        if resp.status == "DENIED":
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"ACCESS_DENIED: {resp.rejection_reason}"}],
            }
        elif resp.status == "FAILED":
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"PROVIDER_FAILURE: {resp.rejection_reason}"}],
            }

        safe_result = {
            "advisory_role": role.value,
            "status": resp.status,
            "answer": resp.answer,
            "structured_data": resp.structured_data,
            "model": resp.model,
            "response_id": resp.response_id,
            "usage": resp.usage,
            "is_advisory_only": True,
        }

        return {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(safe_result, indent=2)}],
        }
