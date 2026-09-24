"""Transport boundary for optional decision models."""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Protocol


class DecisionTransport(Protocol):
    def evaluate(self, payload: Dict[str, Any], *, deadline_ms: int) -> Dict[str, Any]: ...


class VercelJevTransport:
    """One-shot Vercel evaluation transport; policy must authorize before construction/use."""

    def __init__(self, api_key: str, base_url: str = "https://ai-gateway.vercel.sh/v1/evaluate") -> None:
        if not api_key:
            raise ValueError("Vercel Jev credential required")
        self._api_key = api_key
        self._base_url = base_url

    def evaluate(self, payload: Dict[str, Any], *, deadline_ms: int) -> Dict[str, Any]:
        request = urllib.request.Request(
            self._base_url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(0.1, deadline_ms / 1000)) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(body) > 2 * 1024 * 1024:
                raise RuntimeError("Jev transport contract failure")
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Jev response must be an object")
        return value
