"""Core Freeze Auditor & Verification Script (R20).

Verifies that zero pre-existing tracked files at base commit 7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e
have been modified, deleted, renamed, or mode-changed, and checks that all added files reside in authorized path prefixes.
"""

from dataclasses import dataclass
from typing import List, Tuple
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


@dataclass
class CoreFreezeReport:
    base_sha: str
    preexisting_tracked_file_mutation_count: int
    core_freeze_verified: bool
    added_files: List[str]
    unauthorized_added_paths: List[str]


def audit_core_freeze(repo_root: str) -> CoreFreezeReport:
    # 1. Get diff of modified tracked files relative to base commit
    cmd_diff = ["git", "diff", "--name-status", BASE_SHA]
    proc = subprocess.run(cmd_diff, cwd=repo_root, capture_output=True, text=True, check=True)

    mutations = []
    added_files = []
    unauthorized = []

    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(maxsplit=1)
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1].replace("\\", "/")

        if status.startswith("M") or status.startswith("D") or status.startswith("R"):
            mutations.append((status, path))
        elif status.startswith("A"):
            added_files.append(path)
            # Check prefix
            if not any(path.startswith(prefix) for prefix in AUTHORIZED_PREFIXES):
                unauthorized.append(path)

    # Check uncommitted status for modified tracked files
    cmd_status = ["git", "status", "--porcelain"]
    proc_st = subprocess.run(cmd_status, cwd=repo_root, capture_output=True, text=True, check=True)
    for line in proc_st.stdout.strip().splitlines():
        if not line.strip():
            continue
        st = line[:2]
        path = line[3:].strip().replace("\\", "/")
        if st[0] in ("M", "D", "R") or st[1] in ("M", "D", "R"):
            if path not in [m[1] for m in mutations]:
                mutations.append((st, path))

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
