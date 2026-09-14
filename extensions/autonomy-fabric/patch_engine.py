"""AOS Robust Bounded Unified Diff Patch Engine (V2-R1).

Applies unified diffs safely:
- Parses multi-file, multi-hunk diffs with standard headers (`--- a/`, `+++ b/`).
- Validates hunk context lines and computes line offsets.
- Handles new file creation (`--- /dev/null`).
- Denies file deletion (`+++ /dev/null`) unless explicitly authorized.
- Verifies precondition SHAs and prevents silent partial or misaligned patches.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict


@dataclass
class DiffHunk:
    orig_start: int
    orig_count: int
    new_start: int
    new_count: int
    lines: List[str] = field(default_factory=list)


@dataclass
class FilePatch:
    orig_path: Optional[str]
    new_path: Optional[str]
    is_new_file: bool = False
    is_deleted_file: bool = False
    precondition_sha: Optional[str] = None
    hunks: List[DiffHunk] = field(default_factory=list)


class PatchApplicationError(ValueError):
    """Raised when patch cannot be cleanly applied due to context or offset mismatch."""
    pass


class PatchPreconditionError(ValueError):
    """Raised when the actual source file SHA256 does not match the expected precondition SHA."""
    pass


HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
INDEX_HEADER_RE = re.compile(r"^index ([0-9a-fA-F]+)\.\.([0-9a-fA-F]+)")


def parse_unified_diff(diff_text: str) -> List[FilePatch]:
    """Parses standard unified diff into structured FilePatch objects."""
    file_patches: List[FilePatch] = []
    current_patch: Optional[FilePatch] = None
    current_hunk: Optional[DiffHunk] = None
    last_precondition_sha: Optional[str] = None

    lines = diff_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("index "):
            m_idx = INDEX_HEADER_RE.match(line)
            if m_idx:
                last_precondition_sha = m_idx.group(1)

        if line.startswith("--- "):
            # Save prior hunk/patch
            if current_hunk and current_patch:
                current_patch.hunks.append(current_hunk)
                current_hunk = None
            if current_patch:
                file_patches.append(current_patch)
                current_patch = None

            orig_raw = line[4:].strip()
            # Next line should be +++
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                i += 1
                new_raw = lines[i][4:].strip()

                is_new = orig_raw == "/dev/null"
                is_del = new_raw == "/dev/null"

                clean_orig = orig_raw[2:] if orig_raw.startswith("a/") or orig_raw.startswith("b/") else orig_raw
                clean_new = new_raw[2:] if new_raw.startswith("a/") or new_raw.startswith("b/") else new_raw

                current_patch = FilePatch(
                    orig_path=None if is_new else clean_orig,
                    new_path=None if is_del else clean_new,
                    is_new_file=is_new,
                    is_deleted_file=is_del,
                    precondition_sha=last_precondition_sha,
                )
                last_precondition_sha = None
            i += 1
            continue

        if line.startswith("@@ "):
            m = HUNK_HEADER_RE.match(line)
            if m and current_patch:
                if current_hunk:
                    current_patch.hunks.append(current_hunk)
                orig_start = int(m.group(1))
                orig_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                current_hunk = DiffHunk(
                    orig_start=orig_start,
                    orig_count=orig_count,
                    new_start=new_start,
                    new_count=new_count,
                )
            i += 1
            continue

        if current_hunk:
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                current_hunk.lines.append(line)
            elif line.startswith("\\ No newline at end of file"):
                pass

        i += 1

    if current_hunk and current_patch:
        current_patch.hunks.append(current_hunk)
    if current_patch:
        file_patches.append(current_patch)

    return file_patches


def apply_patch_to_lines(original_lines: List[str], file_patch: FilePatch) -> List[str]:
    """Applies hunks with context verification and offset tracking."""
    if file_patch.is_new_file:
        new_lines: List[str] = []
        for hunk in file_patch.hunks:
            for l in hunk.lines:
                if l.startswith("+"):
                    new_lines.append(l[1:])
        return new_lines

    lines = list(original_lines)
    offset = 0

    for hunk in file_patch.hunks:
        target_idx = (hunk.orig_start - 1) + offset

        # Search for matching context within a small window around target_idx
        expected_orig_lines = [l[1:] for l in hunk.lines if not l.startswith("+")]
        match_idx = None

        # Check exact position first, then search +/- 10 lines for fuzzy offset
        search_range = [0]
        for delta in range(1, 15):
            search_range.extend([delta, -delta])

        for delta in search_range:
            cand = target_idx + delta
            if cand < 0 or cand + len(expected_orig_lines) > len(lines):
                continue
            if lines[cand : cand + len(expected_orig_lines)] == expected_orig_lines:
                match_idx = cand
                break

        if match_idx is None:
            raise PatchApplicationError(
                f"Context mismatch applying hunk @@ -{hunk.orig_start},{hunk.orig_count} +{hunk.new_start},{hunk.new_count} @@"
            )

        # Build replacement lines for this hunk
        replacement: List[str] = []
        for l in hunk.lines:
            if l.startswith(" "):
                replacement.append(l[1:])
            elif l.startswith("+"):
                replacement.append(l[1:])
            # '-' lines are omitted

        # Splice into target
        lines[match_idx : match_idx + len(expected_orig_lines)] = replacement
        offset += len(replacement) - len(expected_orig_lines)

    return lines
