"""Pure, SHA-bound exact text edits for bounded native source mutation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_EDIT_COUNT = 32
MAX_OLD_BYTES = 128 * 1024
MAX_NEW_BYTES = 128 * 1024


class ExactEditError(ValueError):
    """Raised when an exact-edit request cannot be proven against its source."""


@dataclass(frozen=True)
class ExactEditEvidence:
    edit_index: int
    start_byte: int
    end_byte: int
    old_sha256: str
    new_sha256: str


@dataclass(frozen=True)
class ExactEditResult:
    result_bytes: bytes
    original_sha256: str
    result_sha256: str
    newline: str
    evidence: Tuple[ExactEditEvidence, ...]


def _newline_style(value: str, *, label: str) -> str | None:
    without_crlf = value.replace("\r\n", "")
    if "\r" in without_crlf:
        raise ExactEditError(f"{label} contains unsupported bare CR newlines")
    has_crlf = "\r\n" in value
    has_lf = "\n" in without_crlf
    if has_crlf and has_lf:
        raise ExactEditError(f"{label} contains mixed LF and CRLF newlines")
    if has_crlf:
        return "\r\n"
    if has_lf:
        return "\n"
    return None


def _normalize_newlines(value: str, source_newline: str | None, *, label: str) -> str:
    requested_newline = _newline_style(value, label=label)
    if requested_newline is None:
        return value
    if source_newline is None:
        raise ExactEditError(
            f"{label} is multiline but the source has no proven newline convention"
        )
    if requested_newline == source_newline:
        return value
    return value.replace(requested_newline, source_newline)


def apply_exact_edits(
    original_bytes: bytes,
    edits: Sequence[Mapping[str, str]],
    expected_sha256: str,
) -> ExactEditResult:
    """Validate all anchors against one snapshot and return the exact new bytes.

    This function performs no filesystem access. It never applies a partial edit:
    every bound, hash, encoding, newline, uniqueness, and overlap check succeeds
    before the immutable source snapshot is transformed.
    """
    if not isinstance(original_bytes, bytes):
        raise ExactEditError("original source must be raw bytes")
    if len(original_bytes) > MAX_FILE_BYTES:
        raise ExactEditError(f"source exceeds maximum size of {MAX_FILE_BYTES} bytes")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_sha256
    ):
        raise ExactEditError("expected SHA must be a full 64-character raw-file SHA256")

    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    if original_sha256 != expected_sha256.lower():
        raise ExactEditError(
            f"source SHA mismatch: expected {expected_sha256.lower()}, actual {original_sha256}"
        )

    try:
        source = original_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExactEditError("source is not valid UTF-8") from exc
    source_newline = _newline_style(source, label="source")

    if isinstance(edits, (str, bytes)) or not isinstance(edits, Sequence):
        raise ExactEditError("edits must be an ordered list of old/new objects")
    if not edits:
        raise ExactEditError("at least one exact edit is required")
    if len(edits) > MAX_EDIT_COUNT:
        raise ExactEditError(f"edit count exceeds maximum of {MAX_EDIT_COUNT}")

    validated: list[tuple[int, int, str, str, int]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, Mapping) or set(edit) != {"old", "new"}:
            raise ExactEditError(f"edit {index} must contain only string old/new values")
        old = edit.get("old")
        new = edit.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ExactEditError(f"edit {index} old/new values must be strings")
        if not old:
            raise ExactEditError(f"edit {index} old anchor must not be empty")
        if len(old.encode("utf-8")) > MAX_OLD_BYTES:
            raise ExactEditError(f"edit {index} old anchor exceeds {MAX_OLD_BYTES} bytes")
        if len(new.encode("utf-8")) > MAX_NEW_BYTES:
            raise ExactEditError(f"edit {index} replacement exceeds {MAX_NEW_BYTES} bytes")

        normalized_old = _normalize_newlines(
            old, source_newline, label=f"edit {index} old anchor"
        )
        normalized_new = _normalize_newlines(
            new, source_newline, label=f"edit {index} replacement"
        )
        if normalized_old == normalized_new:
            raise ExactEditError(f"edit {index} is a no-op")

        occurrence_count = source.count(normalized_old)
        if occurrence_count == 0:
            raise ExactEditError(f"edit {index} old anchor is missing from source")
        if occurrence_count != 1:
            raise ExactEditError(
                f"edit {index} old anchor is ambiguous: found {occurrence_count} occurrences"
            )
        start = source.index(normalized_old)
        end = start + len(normalized_old)
        validated.append((start, end, normalized_old, normalized_new, index))

    ordered = sorted(validated, key=lambda item: (item[0], item[1]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ExactEditError(
                f"edits {previous[4]} and {current[4]} have overlapping source ranges"
            )

    result = source
    for start, end, _old, new, _index in reversed(ordered):
        result = result[:start] + new + result[end:]
    result_bytes = result.encode("utf-8")
    if len(result_bytes) > MAX_FILE_BYTES:
        raise ExactEditError(f"result exceeds maximum size of {MAX_FILE_BYTES} bytes")
    if result_bytes == original_bytes:
        raise ExactEditError("exact edits produced no byte change")

    evidence = tuple(
        ExactEditEvidence(
            edit_index=index,
            start_byte=len(source[:start].encode("utf-8")),
            end_byte=len(source[:end].encode("utf-8")),
            old_sha256=hashlib.sha256(old.encode("utf-8")).hexdigest(),
            new_sha256=hashlib.sha256(new.encode("utf-8")).hexdigest(),
        )
        for start, end, old, new, index in validated
    )
    return ExactEditResult(
        result_bytes=result_bytes,
        original_sha256=original_sha256,
        result_sha256=hashlib.sha256(result_bytes).hexdigest(),
        newline="CRLF" if source_newline == "\r\n" else "LF" if source_newline == "\n" else "NONE",
        evidence=evidence,
    )
