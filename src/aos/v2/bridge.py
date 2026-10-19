"""Worker and Git bridge for AOS V2.

Implements bounded exact-edit mutation, private Git index staging,
CAS commit publication, atomic materialization, and the Section 5 recovery decision table.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from aos.process_utils import run_headless
from extensions.autonomy_fabric.exact_edit_engine import (
    ExactEditError,
    ExactEditResult,
    apply_exact_edits,
)


class RecoveryDecision(str, Enum):
    NO_EFFECT_PROVEN = "NO_EFFECT_PROVEN"
    CONTINUE_PREPARED = "CONTINUE_PREPARED"
    FINISH_MATERIALIZATION = "FINISH_MATERIALIZATION"
    ACCEPT_EXISTING = "ACCEPT_EXISTING"
    SKIP_ACCEPTED = "SKIP_ACCEPTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class GitBridgeError(Exception):
    """Raised on Git bridge or exact edit failure."""


@dataclass(frozen=True)
class TaskIntent:
    command_id: str
    task_id: str
    owner_epoch: int
    repo_root: Path
    owned_branch: str
    expected_parent: str
    target_rel_path: str
    pre_sha256: str
    edits: Sequence[Mapping[str, str]]


@dataclass(frozen=True)
class PreparedCandidate:
    intent: TaskIntent
    post_sha256: str
    new_bytes: bytes
    blob_sha: str
    tree_sha: str
    candidate_dir: Path


@dataclass(frozen=True)
class PublishedCommit:
    prepared: PreparedCandidate
    commit_sha: str


def compute_file_sha256(path: Path) -> str:
    """Return SHA256 hex digest of file contents."""
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_bytes_sha256(data: bytes) -> str:
    """Return SHA256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _git_run(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_data: bytes | str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute Git CLI command securely without hooks or interactive prompts."""
    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    if env:
        git_env.update(env)

    # Base git args: disable hooks
    full_cmd = ["git", "-c", "core.hooksPath=", *cmd]
    is_bytes = isinstance(input_data, bytes)

    try:
        res = run_headless(
            full_cmd,
            cwd=str(cwd),
            env=git_env,
            input=input_data if is_bytes else (input_data.encode("utf-8") if input_data else None),
            text=False,
            timeout=30.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitBridgeError(f"Git command timed out ({' '.join(cmd)})") from exc

    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace") if isinstance(res.stderr, bytes) else str(res.stderr)
        raise GitBridgeError(f"Git command failed ({' '.join(cmd)}): {err.strip()}")

    stdout_str = res.stdout.decode("utf-8", errors="replace").strip() if isinstance(res.stdout, bytes) else str(res.stdout).strip()
    stderr_str = res.stderr.decode("utf-8", errors="replace").strip() if isinstance(res.stderr, bytes) else str(res.stderr).strip()

    return subprocess.CompletedProcess(
        args=full_cmd,
        returncode=res.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
    )


class WorkerGitBridge:
    """Controls bounded exact edits, private Git staging, and CAS publishing."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        if not (self.repo_root / ".git").exists():
            raise GitBridgeError(f"Not a git repository: {self.repo_root}")

    def get_ref_sha(self, ref_name: str) -> str | None:
        """Get exact commit SHA for a ref, or None if ref does not exist."""
        try:
            res = _git_run(["rev-parse", "--verify", ref_name], cwd=self.repo_root)
            return res.stdout.strip()
        except GitBridgeError:
            return None

    def get_current_branch(self) -> str:
        """Get current branch name, or 'HEAD' if detached."""
        try:
            res = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root)
            return res.stdout.strip()
        except GitBridgeError:
            return "HEAD"

    def prepare_candidate(
        self,
        intent: TaskIntent,
        scratch_dir: Path,
    ) -> PreparedCandidate:
        """Apply exact edits and stage blobs into private index without touching user worktree."""
        scratch_path = Path(scratch_dir).resolve()
        scratch_path.mkdir(parents=True, exist_ok=True)
        private_index = scratch_path / "private.index"

        # 1. Read original source bytes and verify pre_sha256
        target_file = self.repo_root / intent.target_rel_path
        if not target_file.is_file():
            raise GitBridgeError(f"Target file does not exist: {target_file}")
        orig_bytes = target_file.read_bytes()
        actual_pre_sha = compute_bytes_sha256(orig_bytes)
        if actual_pre_sha.lower() != intent.pre_sha256.lower():
            raise GitBridgeError(
                f"Source pre-hash mismatch for {intent.target_rel_path}: expected {intent.pre_sha256}, got {actual_pre_sha}"
            )

        # 2. Apply pure exact edits
        try:
            edit_res: ExactEditResult = apply_exact_edits(orig_bytes, intent.edits, intent.pre_sha256)
        except ExactEditError as e:
            raise GitBridgeError(f"Exact edit application failed: {e}") from e

        post_sha = edit_res.result_sha256
        new_bytes = edit_res.result_bytes

        # 3. Write new blob into Git object store
        blob_res = _git_run(["hash-object", "-w", "--stdin"], cwd=self.repo_root, input_data=new_bytes)
        blob_sha = blob_res.stdout.strip()

        # 4. Prepare private index from expected parent
        env_index = {"GIT_INDEX_FILE": str(private_index)}
        if private_index.exists():
            private_index.unlink()

        _git_run(["read-tree", intent.expected_parent], cwd=self.repo_root, env=env_index)

        # 5. Update index with new blob
        _git_run(
            ["update-index", "--add", "--cacheinfo", "100644", blob_sha, intent.target_rel_path],
            cwd=self.repo_root,
            env=env_index,
        )

        # 6. Write tree from private index
        tree_res = _git_run(["write-tree"], cwd=self.repo_root, env=env_index)
        tree_sha = tree_res.stdout.strip()

        # 7. Materialize candidate directory for sandboxed testing
        candidate_dir = scratch_path / "candidate"
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        # Copy repo root excluding .git and scratch
        candidate_dir.mkdir(parents=True, exist_ok=True)
        for item in self.repo_root.iterdir():
            if item.name in (".git", ".venv", "scratch"):
                continue
            dest = candidate_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            else:
                shutil.copy2(item, dest)

        # Write the mutated file into candidate_dir
        mutated_target = candidate_dir / intent.target_rel_path
        mutated_target.parent.mkdir(parents=True, exist_ok=True)
        mutated_target.write_bytes(new_bytes)

        return PreparedCandidate(
            intent=intent,
            post_sha256=post_sha,
            new_bytes=new_bytes,
            blob_sha=blob_sha,
            tree_sha=tree_sha,
            candidate_dir=candidate_dir,
        )

    def publish_candidate(
        self,
        prepared: PreparedCandidate,
        message: str = "AOS V2 bounded mutation",
    ) -> PublishedCommit:
        """Create commit object and update branch using atomic CAS git update-ref."""
        intent = prepared.intent

        # 1. Check for unrelated staged or dirty changes before publish
        status_res = _git_run(["status", "--porcelain"], cwd=self.repo_root)
        lines = [line.strip() for line in status_res.stdout.splitlines() if line.strip()]
        bad_lines = [
            line for line in lines
            if not (line.startswith("?? ") and ("__pycache__" in line or ".pytest_cache" in line))
        ]
        if bad_lines:
            raise GitBridgeError(
                f"Unrelated staged or modified files exist in working tree before publish:\n" + "\n".join(bad_lines)
            )

        # 2. Format commit message with required trailers
        trailers = (
            f"\n\nAOS-Command: {intent.command_id}\n"
            f"AOS-Task: {intent.task_id}\n"
            f"AOS-Epoch: {intent.owner_epoch}\n"
            f"AOS-PreHash: {intent.pre_sha256}\n"
            f"AOS-PostHash: {prepared.post_sha256}"
        )
        full_msg = message.strip() + trailers

        # 3. Create commit object
        commit_res = _git_run(
            ["commit-tree", prepared.tree_sha, "-p", intent.expected_parent, "-m", full_msg],
            cwd=self.repo_root,
        )
        commit_sha = commit_res.stdout.strip()

        # 4. CAS update-ref: update ref only if it still points to expected_parent
        if intent.owned_branch == "HEAD":
            ref_name = "HEAD"
        elif intent.owned_branch.startswith("refs/"):
            ref_name = intent.owned_branch
        else:
            ref_name = f"refs/heads/{intent.owned_branch}"
        _git_run(
            ["update-ref", ref_name, commit_sha, intent.expected_parent],
            cwd=self.repo_root,
        )

        return PublishedCommit(prepared=prepared, commit_sha=commit_sha)

    def materialize_source(self, prepared: PreparedCandidate) -> None:
        """Atomically materialize intended source file into working tree and verify post-hash."""
        target_path = self.repo_root / prepared.intent.target_rel_path
        if target_path.exists():
            current_sha = compute_file_sha256(target_path).lower()
            if current_sha != prepared.intent.pre_sha256.lower() and current_sha != prepared.post_sha256.lower():
                raise GitBridgeError(
                    f"Materialize refuses to overwrite drifted file: disk hash ({current_sha}) "
                    f"matches neither pre_sha ({prepared.intent.pre_sha256}) nor post_sha ({prepared.post_sha256})"
                )

        temp_path = target_path.with_suffix(".aos_tmp")
        temp_path.write_bytes(prepared.new_bytes)
        # Atomic replace
        temp_path.replace(target_path)

        # Verify hash on disk
        actual_post_sha = compute_file_sha256(target_path)
        if actual_post_sha.lower() != prepared.post_sha256.lower():
            raise GitBridgeError(
                f"Materialization post-hash verification failed: expected {prepared.post_sha256}, got {actual_post_sha}"
            )

        # Update standard git index to match
        _git_run(
            ["update-index", "--add", "--cacheinfo", "100644", prepared.blob_sha, prepared.intent.target_rel_path],
            cwd=self.repo_root,
        )

    def reconcile_task(
        self,
        intent: TaskIntent,
        *,
        current_task_status: str | None,
        stored_commit_sha: str | None,
        stored_prepared_tree: str | None,
    ) -> RecoveryDecision:
        """Implement the 6 rows of the contract recovery decision table (Section 5, Table 110-116)."""
        ref_name = f"refs/heads/{intent.owned_branch}" if not intent.owned_branch.startswith("refs/") else intent.owned_branch
        current_ref_sha = self.get_ref_sha(ref_name)
        target_file = self.repo_root / intent.target_rel_path

        # If file does not exist, check if unexpected drift
        file_sha: str | None = None
        if target_file.is_file():
            file_sha = compute_file_sha256(target_file).lower()

        # Extract expected post-hash from commit if stored_commit_sha exists
        expected_post_sha: str | None = None
        if stored_commit_sha:
            try:
                trailers_res = _git_run(["log", "-1", "--format=%B", stored_commit_sha], cwd=self.repo_root)
                for line in trailers_res.stdout.splitlines():
                    if line.startswith("AOS-PostHash:"):
                        expected_post_sha = line.split(":", 1)[1].strip().lower()
                if not expected_post_sha:
                    blob_content_res = _git_run(["show", f"{stored_commit_sha}:{intent.target_rel_path}"], cwd=self.repo_root)
                    expected_post_sha = hashlib.sha256(blob_content_res.stdout.encode("utf-8")).hexdigest().lower()
            except Exception:
                expected_post_sha = None

        # Row 5: Already ACCEPTED and all post-hashes/commit/test identities agree
        if current_task_status == "ACCEPTED":
            if (
                stored_commit_sha
                and current_ref_sha == stored_commit_sha
                and file_sha
                and expected_post_sha
                and file_sha == expected_post_sha
            ):
                return RecoveryDecision.SKIP_ACCEPTED

        # Row 3 & 4: Exact intended commit is published on branch
        if stored_commit_sha and current_ref_sha == stored_commit_sha:
            if file_sha and expected_post_sha and file_sha == expected_post_sha:
                if current_task_status == "ACCEPTED":
                    return RecoveryDecision.SKIP_ACCEPTED
                return RecoveryDecision.ACCEPT_EXISTING
            elif file_sha and file_sha == intent.pre_sha256.lower():
                # File is still pre_hash, finish missing materialization
                return RecoveryDecision.FINISH_MATERIALIZATION
            else:
                # Drift detected!
                return RecoveryDecision.HUMAN_REQUIRED

        # Row 2: Prepared candidate/test proof exists, parent/source pre-hashes
        if (
            stored_prepared_tree
            and current_ref_sha == intent.expected_parent
            and file_sha == intent.pre_sha256.lower()
        ):
            return RecoveryDecision.CONTINUE_PREPARED

        # Row 1: No published commit, all source/index pre-hashes, no candidate
        if (
            not stored_commit_sha
            and not stored_prepared_tree
            and current_ref_sha == intent.expected_parent
            and file_sha == intent.pre_sha256.lower()
        ):
            return RecoveryDecision.NO_EFFECT_PROVEN

        # Row 6: Unknown lock, unexpected HEAD/ref/index/hash/path/test drift
        return RecoveryDecision.HUMAN_REQUIRED
