"""AOS Controller Relay V1 True Git Data CAS Transport Implementation.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01

Provides:
- CredentialProvider abstract boundary & FakeCredentialProvider
- GitHubRequester abstract boundary & StdlibGitHubRequester
- GitDataCASRelayTransport 11-step CAS pipeline over GitHub REST Git Data API
- Strict endpoint allowlist and fixed repository/branch invariants
- Complete token sanitization and fail-closed CAS race handling
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from aos.controller_relay import ControllerRelayValidationResult

FIXED_RELAY_REPOSITORY: str = "MertSGI/AOS"
FIXED_RELAY_BRANCH: str = "control/controller-relay"
FIXED_RELAY_REF: str = "refs/heads/control/controller-relay"

# Endpoint allowlist regular expressions
ALLOWED_ENDPOINTS: List[Tuple[str, re.Pattern]] = [
    ("GET", re.compile(r"^/repos/MertSGI/AOS/git/ref/heads/control/controller-relay$")),
    ("GET", re.compile(r"^/repos/MertSGI/AOS/git/commits/[0-9a-f]{40}$")),
    ("GET", re.compile(r"^/repos/MertSGI/AOS/git/trees/[0-9a-f]{40}$")),
    ("GET", re.compile(r"^/repos/MertSGI/AOS/git/blobs/[0-9a-f]{40}$")),
    ("POST", re.compile(r"^/repos/MertSGI/AOS/git/blobs$")),
    ("POST", re.compile(r"^/repos/MertSGI/AOS/git/trees$")),
    ("POST", re.compile(r"^/repos/MertSGI/AOS/git/commits$")),
    ("PATCH", re.compile(r"^/repos/MertSGI/AOS/git/refs/heads/control/controller-relay$")),
]


class ControllerRelayTransportError(Exception):
    """Base exception for transport failure with sanitized error output."""
    pass


class CredentialProvider(ABC):
    """Abstract interface for retrieving GitHub installation credentials."""

    @abstractmethod
    def get_token(self) -> str:
        """Retrieve bearer token string."""
        pass


class FakeCredentialProvider(CredentialProvider):
    """Fake credential provider for deterministic offline testing."""

    def __init__(self, token: str = "fake-token-12345"):
        self._token = token

    def get_token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return "FakeCredentialProvider(token='***REDACTED***')"


class GitHubRequester(ABC):
    """Abstract HTTP requester interface for GitHub Git Data REST API."""

    @abstractmethod
    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """Send HTTP request and return (status_code, body_bytes, response_headers)."""
        pass


class StdlibGitHubRequester(GitHubRequester):
    """Production GitHub requester implementation using Python stdlib urllib.

    Enforces endpoint allowlist, repository bounds, and token sanitization.
    """

    def __init__(self, credential_provider: CredentialProvider, base_url: str = "https://api.github.com"):
        self._credential_provider = credential_provider
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"StdlibGitHubRequester(base_url={self._base_url!r}, credential_provider={self._credential_provider!r})"

    def _validate_endpoint(self, method: str, path: str) -> None:
        norm_method = method.upper()
        for allowed_method, pattern in ALLOWED_ENDPOINTS:
            if norm_method == allowed_method and pattern.match(path):
                return
        raise ControllerRelayTransportError(f"Endpoint access prohibited by allowlist: {norm_method} {path}")

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        self._validate_endpoint(method, path)

        token = self._credential_provider.get_token()
        req_headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AOS-Controller-Relay-V1",
            "Authorization": f"Bearer {token}",
        }
        if headers:
            req_headers.update(headers)

        full_url = f"{self._base_url}{path}"
        req = urllib.request.Request(full_url, data=body, headers=req_headers, method=method.upper())

        try:
            with urllib.request.urlopen(req) as resp:
                status_code = resp.status
                resp_bytes = resp.read()
                resp_headers = dict(resp.headers)
                return status_code, resp_bytes, resp_headers
        except urllib.error.HTTPError as http_err:
            err_bytes = http_err.read() if http_err.fp else b""
            # Sanitize token from error string
            sanitized_msg = str(http_err).replace(token, "***REDACTED***")
            sanitized_body = err_bytes.decode("utf-8", errors="replace").replace(token, "***REDACTED***")
            return http_err.code, sanitized_body.encode("utf-8"), dict(http_err.headers)
        except Exception as exc:
            sanitized_msg = str(exc).replace(token, "***REDACTED***")
            raise ControllerRelayTransportError(f"GitHub request failed: {sanitized_msg}") from None


class GitDataCASRelayTransport:
    """True Git Data Compare-and-Swap (CAS) Relay Transport.

    Implements atomic publication of single Relay records to control/controller-relay
    using low-level REST Git Data API primitives (blobs, trees, commits, refs).
    """

    def __init__(self, requester: GitHubRequester):
        self._requester = requester
        self._repository = FIXED_RELAY_REPOSITORY
        self._branch = FIXED_RELAY_BRANCH

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def branch(self) -> str:
        return self._branch

    def _assert_fixed_bounds(self, repository: str, branch_or_ref: str) -> None:
        if repository != FIXED_RELAY_REPOSITORY:
            raise ValueError(f"Repository invariant violation: expected '{FIXED_RELAY_REPOSITORY}', got '{repository}'")
        if branch_or_ref not in (FIXED_RELAY_BRANCH, FIXED_RELAY_REF):
            raise ValueError(
                f"Branch/ref invariant violation: expected '{FIXED_RELAY_BRANCH}' or '{FIXED_RELAY_REF}', got '{branch_or_ref}'"
            )

    def get_branch_head(self, repository: str, branch: str) -> str:
        """Fetch current HEAD commit SHA of target branch."""
        self._assert_fixed_bounds(repository, branch)
        path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/ref/heads/{FIXED_RELAY_BRANCH}"
        status, body, _ = self._requester.request("GET", path)

        if status != 200:
            raise ControllerRelayTransportError(f"Failed to fetch branch head ref (HTTP {status}): {body.decode('utf-8')}")

        data = json.loads(body.decode("utf-8"))
        obj = data.get("object", {})
        sha = obj.get("sha")
        if not sha or not isinstance(sha, str) or len(sha) != 40:
            raise ControllerRelayTransportError(f"Malformed ref response object from GitHub: {data}")
        return sha

    def _get_commit(self, commit_sha: str) -> Dict[str, Any]:
        path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits/{commit_sha}"
        status, body, _ = self._requester.request("GET", path)
        if status != 200:
            raise ControllerRelayTransportError(f"Failed to fetch commit '{commit_sha}' (HTTP {status})")
        return json.loads(body.decode("utf-8"))

    def _get_tree(self, tree_sha: str, recursive: bool = False) -> Dict[str, Any]:
        rec_param = "?recursive=1" if recursive else ""
        path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees/{tree_sha}{rec_param}"
        status, body, _ = self._requester.request("GET", path)
        if status != 200:
            raise ControllerRelayTransportError(f"Failed to fetch tree '{tree_sha}' (HTTP {status})")
        return json.loads(body.decode("utf-8"))

    def read_record_bytes(self, repository: str, ref_or_branch: str, path: str) -> bytes:
        """Read a record's raw bytes at target path from specified ref or commit SHA."""
        # Check repository invariant
        if repository != FIXED_RELAY_REPOSITORY:
            raise ValueError(f"Repository invariant violation: got '{repository}'")

        commit_sha = self.get_branch_head(repository, FIXED_RELAY_BRANCH) if ref_or_branch in (FIXED_RELAY_BRANCH, FIXED_RELAY_REF) else ref_or_branch
        commit_data = self._get_commit(commit_sha)
        tree_sha = commit_data["tree"]["sha"]
        tree_data = self._get_tree(tree_sha, recursive=True)

        target_blob_sha = None
        for item in tree_data.get("tree", []):
            if item.get("path") == path and item.get("type") == "blob":
                target_blob_sha = item.get("sha")
                break

        if not target_blob_sha:
            raise FileNotFoundError(f"Record path '{path}' not found at ref '{ref_or_branch}'")

        blob_path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs/{target_blob_sha}"
        status, body, _ = self._requester.request("GET", blob_path)
        if status != 200:
            raise ControllerRelayTransportError(f"Failed to fetch blob '{target_blob_sha}' (HTTP {status})")

        blob_data = json.loads(body.decode("utf-8"))
        encoding = blob_data.get("encoding")
        content_str = blob_data.get("content", "")

        if encoding == "base64":
            # Remove any whitespace/newlines inserted by Git
            cleaned = content_str.replace("\n", "").replace("\r", "")
            return base64.b64decode(cleaned)
        elif encoding == "utf-8" or not encoding:
            return content_str.encode("utf-8")
        else:
            raise ControllerRelayTransportError(f"Unsupported blob encoding: '{encoding}'")

    def list_records_under_prefix(self, repository: str, ref_or_branch: str, prefix: str) -> List[Tuple[str, bytes]]:
        """List all records and their bytes matching prefix under target ref."""
        if repository != FIXED_RELAY_REPOSITORY:
            raise ValueError(f"Repository invariant violation: got '{repository}'")

        commit_sha = self.get_branch_head(repository, FIXED_RELAY_BRANCH) if ref_or_branch in (FIXED_RELAY_BRANCH, FIXED_RELAY_REF) else ref_or_branch
        commit_data = self._get_commit(commit_sha)
        tree_sha = commit_data["tree"]["sha"]
        tree_data = self._get_tree(tree_sha, recursive=True)

        results: List[Tuple[str, bytes]] = []
        for item in tree_data.get("tree", []):
            item_path = item.get("path", "")
            if item_path.startswith(prefix) and item.get("type") == "blob":
                record_bytes = self.read_record_bytes(repository, commit_sha, item_path)
                results.append((item_path, record_bytes))
        return results

    def publish_record(
        self,
        repository: str,
        branch: str,
        path: str,
        content_bytes: bytes,
        expected_head: str,
        record_type: str = "record",
    ) -> ControllerRelayValidationResult:
        """Publish a single Relay record atomically via Git Data CAS API primitives.

        11-Step Pipeline:
        1. Read current branch HEAD.
        2. Assert current HEAD == expected_head. (If mismatch -> HOLD_CAS_RACE before object creation).
        3. Fetch expected_head commit -> base_tree_sha.
        4. Read tree and verify target path does NOT exist. (If exists -> HOLD_RECORD_ALREADY_EXISTS).
        5. Create Blob (POST /git/blobs).
        6. Create Tree based on base_tree_sha adding single blob item (POST /git/trees).
        7. Create Commit with parent expected_head (POST /git/commits).
        8. Update Ref with force=False (PATCH /git/refs/heads/control/controller-relay).
        9. Re-read branch HEAD and verify HEAD == new_commit_sha.
        10. Assert new_commit.parents[0] == expected_head.
        11. Verify exact record path and content in post-write commit tree.
        """
        self._assert_fixed_bounds(repository, branch)

        # Step 1 & 2: Read head and verify expected_head
        current_head = self.get_branch_head(repository, branch)
        if current_head != expected_head:
            return ControllerRelayValidationResult(
                False,
                "HOLD_CAS_RACE",
                [f"Branch head mismatch before Git object creation: current '{current_head}' vs expected '{expected_head}'"],
                details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0},
            )

        # Step 3: Fetch expected_head commit
        commit_data = self._get_commit(expected_head)
        base_tree_sha = commit_data["tree"]["sha"]

        # Step 4: Prove target path absent in base tree
        tree_data = self._get_tree(base_tree_sha, recursive=True)
        for item in tree_data.get("tree", []):
            if item.get("path") == path:
                return ControllerRelayValidationResult(
                    False,
                    "HOLD_RECORD_ALREADY_EXISTS",
                    [f"Target record path '{path}' already exists in tree at expected_head '{expected_head}'"],
                    details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0},
                )

        # Step 5: Create Blob
        b64_content = base64.b64encode(content_bytes).decode("ascii")
        blob_req = {"content": b64_content, "encoding": "base64"}
        blob_path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs"
        status, body, _ = self._requester.request("POST", blob_path, body=json.dumps(blob_req).encode("utf-8"))
        if status not in (200, 201):
            return ControllerRelayValidationResult(
                False, "FAIL", [f"Create blob failed (HTTP {status}): {body.decode('utf-8')}"]
            )
        blob_sha = json.loads(body.decode("utf-8"))["sha"]

        # Step 6: Create Tree
        tree_req = {
            "base_tree": base_tree_sha,
            "tree": [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ],
        }
        tree_path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees"
        status, body, _ = self._requester.request("POST", tree_path, body=json.dumps(tree_req).encode("utf-8"))
        if status not in (200, 201):
            return ControllerRelayValidationResult(
                False, "FAIL", [f"Create tree failed (HTTP {status}): {body.decode('utf-8')}"]
            )
        new_tree_sha = json.loads(body.decode("utf-8"))["sha"]

        # Step 7: Create Commit with EXACT parent expected_head
        commit_req = {
            "message": f"relay({record_type}): publish {path}",
            "tree": new_tree_sha,
            "parents": [expected_head],
        }
        commit_path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits"
        status, body, _ = self._requester.request("POST", commit_path, body=json.dumps(commit_req).encode("utf-8"))
        if status not in (200, 201):
            return ControllerRelayValidationResult(
                False, "FAIL", [f"Create commit failed (HTTP {status}): {body.decode('utf-8')}"]
            )
        new_commit_sha = json.loads(body.decode("utf-8"))["sha"]

        # Step 8: Update Ref with force=false
        ref_req = {"sha": new_commit_sha, "force": False}
        ref_patch_path = f"/repos/{FIXED_RELAY_REPOSITORY}/git/refs/heads/{FIXED_RELAY_BRANCH}"
        status, body, _ = self._requester.request("PATCH", ref_patch_path, body=json.dumps(ref_req).encode("utf-8"))
        if status != 200:
            return ControllerRelayValidationResult(
                False,
                "HOLD_CAS_RACE",
                [f"CAS ref patch update failed (HTTP {status}): concurrent head movement detected"],
                details={
                    "new_commit_sha": new_commit_sha,
                    "expected_head": expected_head,
                    "REF_UPDATE_COUNT": 0,
                },
            )

        # Step 9: Re-read branch head and require new HEAD == new_commit_sha
        post_head = self.get_branch_head(repository, branch)
        if post_head != new_commit_sha:
            return ControllerRelayValidationResult(
                False,
                "HOLD_CAS_RACE",
                [f"Post-write branch head mismatch: expected '{new_commit_sha}', got '{post_head}'"],
            )

        # Step 10: Require new commit parent == expected_head
        new_commit_data = self._get_commit(new_commit_sha)
        parents = new_commit_data.get("parents", [])
        if not parents or parents[0].get("sha") != expected_head:
            return ControllerRelayValidationResult(
                False,
                "FAIL",
                [f"Post-write commit parent mismatch: expected '{expected_head}', got {parents}"],
            )

        # Step 11: Verify exact record path and content
        verified_bytes = self.read_record_bytes(repository, new_commit_sha, path)
        if verified_bytes != content_bytes:
            return ControllerRelayValidationResult(
                False,
                "FAIL",
                [f"Post-write content verification failed for path '{path}'"],
            )

        return ControllerRelayValidationResult(
            True,
            "PASS",
            details={
                "commit_sha": new_commit_sha,
                "path": path,
                "expected_head": expected_head,
                "blob_sha": blob_sha,
                "tree_sha": new_tree_sha,
            },
        )
