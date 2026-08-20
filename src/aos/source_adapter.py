"""Read-only GitHub project source adapter for AOS."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Dict, Tuple

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
        file_hashes: Dict[str, str]
    ) -> Dict[str, Any]:
        """Extract and normalize external canonical control state into a canonical project snapshot."""
        state_raw = raw_contents.get("state")
        if not state_raw:
            raise ValueError("Canonical state file missing from fetched context")

        try:
            state_data = json.loads(state_raw)
        except Exception as e:
            raise ValueError(f"Failed to parse canonical STATE.json: {e}") from e

        current_milestone = state_data.get("current_milestone")
        canonical_next_action = state_data.get("next_action")

        ambiguity_reasons = []
        if not current_milestone:
            ambiguity_reasons.append("Missing required 'current_milestone' in canonical state")
        if not canonical_next_action:
            ambiguity_reasons.append("Missing required 'next_action' in canonical state")

        # Extract target_base_sha if present (e.g., LARI core_rc4)
        target_base_sha = None
        if "canonical_refs" in state_data and isinstance(state_data["canonical_refs"], dict):
            core_rc4 = state_data["canonical_refs"].get("core_rc4")
            if isinstance(core_rc4, dict) and "sha" in core_rc4:
                target_base_sha = core_rc4["sha"]

        # Fallback to lari_pilot core_rc4_sha if present
        if not target_base_sha and "lari_pilot" in state_data and isinstance(state_data["lari_pilot"], dict):
            target_base_sha = state_data["lari_pilot"].get("core_rc4_sha")

        snapshot = {
            "schema_version": "0.1.0",
            "project_id": project_id,
            "repository": self.repository,
            "source_ref": self.control_ref,
            "source_sha": exact_sha,
            "current_status": state_data.get("current_status") or state_data.get("status"),
            "current_milestone": current_milestone or "UNKNOWN_MILESTONE",
            "canonical_next_action": canonical_next_action or "UNKNOWN_NEXT_ACTION",
            "target_base_sha": target_base_sha,
            "has_ambiguity": len(ambiguity_reasons) > 0,
            "ambiguity_reasons": ambiguity_reasons,
            "input_file_hashes": file_hashes
        }
        return snapshot
