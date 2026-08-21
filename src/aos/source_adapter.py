"""Read-only GitHub project source adapter for AOS."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any, Dict, Optional, Tuple

def resolve_json_pointer(doc: Any, pointer: str) -> Any:
    """Resolve standard JSON Pointer (RFC 6901) against parsed JSON data."""
    if not pointer or pointer == "/":
        return doc
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON Pointer '{pointer}': must start with '/'")

    parts = pointer[1:].split("/")
    curr = doc
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(curr, dict):
            if part not in curr:
                return None
            curr = curr[part]
        elif isinstance(curr, list):
            try:
                idx = int(part)
                curr = curr[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return curr

class ProjectSourceAdapter:
    """Read-only GitHub canonical project source adapter."""

    def __init__(self, repository: str, control_ref: str, user_agent: str = "AOS-Source-Adapter"):
        self.repository = repository
        self.control_ref = control_ref
        self.user_agent = user_agent

    def resolve_ref_to_sha(self) -> str:
        """Resolve configured control ref to exact 40-character commit SHA."""
        url = f"https://api.github.com/repos/{self.repository}/branches/{self.control_ref}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sha = data["commit"]["sha"]
                if not isinstance(sha, str) or len(sha) != 40:
                    raise ValueError(f"Invalid SHA format resolved from ref {self.control_ref}: {sha}")
                return sha
        except Exception as e:
            raise RuntimeError(f"Failed to resolve ref '{self.control_ref}' for repository '{self.repository}': {e}") from e

    def fetch_file_at_sha(self, path: str, exact_sha: str) -> str:
        """Fetch raw content of a file at exact commit SHA without using branch ref."""
        url = f"https://raw.githubusercontent.com/{self.repository}/{exact_sha}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Failed to fetch file '{path}' at SHA '{exact_sha}' from '{self.repository}': {e}") from e

    def fetch_canonical_context(self, exact_sha: str, paths: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Fetch all canonical files at exact SHA and compute SHA-256 hashes of each content."""
        contents: Dict[str, str] = {}
        hashes: Dict[str, str] = {}
        for key, path in paths.items():
            content = self.fetch_file_at_sha(path, exact_sha)
            contents[key] = content
            hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return contents, hashes

    def build_normalized_snapshot(
        self,
        project_id: str,
        exact_sha: str,
        raw_contents: Dict[str, str],
        file_hashes: Dict[str, str],
        projection_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Declaratively extract and normalize external control state using JSON Pointers."""
        state_raw = raw_contents.get("state")
        if not state_raw:
            raise ValueError("Canonical state file missing from fetched context")

        try:
            state_data = json.loads(state_raw)
        except Exception as e:
            raise ValueError(f"Failed to parse canonical STATE.json: {e}") from e

        # Default pointers if no projection config provided in descriptor
        p_config = projection_config or {
            "current_status_pointer": "/current_status",
            "current_milestone_pointer": "/current_milestone",
            "canonical_next_action_pointer": "/next_action",
            "target_base_sha_pointer": None,
            "target_base_sha_required": False
        }

        status_ptr = p_config.get("current_status_pointer", "/current_status")
        milestone_ptr = p_config.get("current_milestone_pointer", "/current_milestone")
        next_action_ptr = p_config.get("canonical_next_action_pointer", "/next_action")
        target_sha_ptr = p_config.get("target_base_sha_pointer")
        target_sha_required = p_config.get("target_base_sha_required", False)

        current_status = resolve_json_pointer(state_data, status_ptr) or resolve_json_pointer(state_data, "/status")
        current_milestone = resolve_json_pointer(state_data, milestone_ptr)
        canonical_next_action = resolve_json_pointer(state_data, next_action_ptr)

        ambiguity_reasons = []

        target_base_sha = None
        if target_sha_ptr:
            target_base_sha = resolve_json_pointer(state_data, target_sha_ptr)

        exec_base_sha_ptr = p_config.get("next_action_execution_base_sha_pointer")
        exec_base_sha_required = p_config.get("next_action_execution_base_sha_required", False)

        next_action_execution_base_sha = None
        if exec_base_sha_ptr:
            raw_exec_sha = resolve_json_pointer(state_data, exec_base_sha_ptr)
            if raw_exec_sha is not None:
                import re
                if not isinstance(raw_exec_sha, str) or not re.match(r"^[0-9a-f]{40}$", raw_exec_sha):
                    ambiguity_reasons.append(f"Malformed execution base SHA at pointer '{exec_base_sha_ptr}': {raw_exec_sha}")
                else:
                    next_action_execution_base_sha = raw_exec_sha
            elif exec_base_sha_required:
                ambiguity_reasons.append(f"Missing required execution base SHA at pointer '{exec_base_sha_ptr}'")
        elif exec_base_sha_required:
            ambiguity_reasons.append("Missing required execution base SHA pointer configuration")
        if not current_milestone:
            ambiguity_reasons.append(f"Missing required milestone at pointer '{milestone_ptr}'")
        if not canonical_next_action:
            ambiguity_reasons.append(f"Missing required next action at pointer '{next_action_ptr}'")
        if target_sha_required and not target_base_sha:
            ambiguity_reasons.append(f"Missing required target base SHA at pointer '{target_sha_ptr}'")

        # Check target base in next action requirement if configured
        if p_config.get("require_target_base_in_next_action") and target_base_sha and canonical_next_action:
            if target_base_sha not in str(canonical_next_action):
                ambiguity_reasons.append(f"Canonical next action does not contain target base SHA '{target_base_sha}'")

        snapshot = {
            "schema_version": "0.1.0",
            "project_id": project_id,
            "repository": self.repository,
            "source_ref": self.control_ref,
            "source_sha": exact_sha,
            "current_status": str(current_status) if current_status else None,
            "current_milestone": str(current_milestone) if current_milestone else "UNKNOWN_MILESTONE",
            "canonical_next_action": str(canonical_next_action) if canonical_next_action else "UNKNOWN_NEXT_ACTION",
            "target_base_sha": str(target_base_sha) if target_base_sha else None,
            "next_action_execution_base_sha": str(next_action_execution_base_sha) if next_action_execution_base_sha else None,
            "has_ambiguity": len(ambiguity_reasons) > 0,
            "ambiguity_reasons": ambiguity_reasons,
            "input_file_hashes": file_hashes
        }
        return snapshot
