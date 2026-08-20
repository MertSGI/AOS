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
