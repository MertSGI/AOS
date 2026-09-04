"""Core Freeze Auditor & Verification Script (R20 / Correction R1).

Verifies that zero pre-existing tracked files at base commit 7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e
have been modified, deleted, renamed, or mode-changed, and checks that all added files reside in authorized path prefixes.
"""

from dataclasses import dataclass
from typing import List, Set
import subprocess
import os
import sys

BASE_SHA = "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e"

AUTHORIZED_PREFIXES = [
    "extensions/autonomy-fabric/",
    "extensions/design-intelligence/",
    "schemas/autonomy-fabric/",
    "schemas/design-intelligence/",
    "benchmarks/autonomy-fabric/",
    "benchmarks/design-intelligence/",
    "docs/autonomy-fabric/",
    "docs/design-intelligence/",
    "docs/aos-v1-selfdev/",
]

AUTHORIZED_EXACT_FILES = [
    "extensions/__init__.py",
]


@dataclass
class CoreFreezeReport:
    base_sha: str
    preexisting_tracked_file_mutation_count: int
    core_freeze_verified: bool
    added_files: List[str]
    unauthorized_added_paths: List[str]


def audit_core_freeze(repo_root: str) -> CoreFreezeReport:
    # Get all tracked files existing at BASE_SHA
    cmd_base_files = ["git", "ls-tree", "-r", "--name-only", BASE_SHA]
    proc_base = subprocess.run(cmd_base_files, cwd=repo_root, capture_output=True, text=True, check=True)
    base_tracked_files: Set[str] = set(proc_base.stdout.strip().splitlines())

    # Get diff of current tree/commit against BASE_SHA
    cmd_diff = ["git", "diff", "--name-status", BASE_SHA]
    proc_diff = subprocess.run(cmd_diff, cwd=repo_root, capture_output=True, text=True, check=True)

    mutations = []
    added_files = []
    unauthorized = []

    for line in proc_diff.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(maxsplit=1)
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1].replace("\\", "/")

        # Check if the path was a pre-existing tracked file at BASE_SHA
        if path in base_tracked_files:
            mutations.append((status, path))
        elif status.startswith("A"):
            added_files.append(path)
            is_auth_prefix = any(path.startswith(prefix) for prefix in AUTHORIZED_PREFIXES)
            is_auth_file = path in AUTHORIZED_EXACT_FILES
            if not (is_auth_prefix or is_auth_file):
                unauthorized.append(path)

    mutation_count = len(mutations)
    core_verified = (mutation_count == 0) and (len(unauthorized) == 0)

    return CoreFreezeReport(
        base_sha=BASE_SHA,
        preexisting_tracked_file_mutation_count=mutation_count,
        core_freeze_verified=core_verified,
        added_files=added_files,
        unauthorized_added_paths=unauthorized,
    )


if __name__ == "__main__":
    report = audit_core_freeze(os.getcwd())
    print(f"PREEXISTING_TRACKED_FILE_MUTATION_COUNT={report.preexisting_tracked_file_mutation_count}")
    print(f"CORE_FREEZE_VERIFIED={'YES' if report.core_freeze_verified else 'NO'}")
    if report.unauthorized_added_paths:
        print(f"UNAUTHORIZED_PATHS={report.unauthorized_added_paths}")
    sys.exit(0 if report.core_freeze_verified else 1)
