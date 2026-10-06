"""Structured acceptance receipt for canonical control-plane transitions."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

_RECEIPT_RE = re.compile(r"^acceptance-receipt-.*\.json$")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(data), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclasses.dataclass
class AcceptanceReceipt:
    receipt_id: str
    project_id: str
    lane: str
    slice_id: str
    execution_base_sha: str
    candidate_sha: str
    ci_workflow_name: str
    ci_run_id: int
    ci_conclusion: str
    acceptance_result: str
    controller_authority: str
    control_sha_before: Optional[str] = None
    canonical_control_transition_sha: Optional[str] = None
    created_at: str = dataclasses.field(default_factory=_utc_now)
    production: str = "NO_GO"

    def __post_init__(self) -> None:
        if self.production != "NO_GO":
            raise ValueError("production must always be NO_GO")
        if self.acceptance_result not in ("ACCEPTED", "REJECTED"):
            raise ValueError(f"Invalid acceptance_result: {self.acceptance_result}")
        before = self.control_sha_before or self.canonical_control_transition_sha
        if not before:
            raise ValueError("acceptance receipt requires control_sha_before")
        if (
            self.control_sha_before
            and self.canonical_control_transition_sha
            and self.control_sha_before != self.canonical_control_transition_sha
        ):
            raise ValueError("legacy transition SHA must equal control_sha_before")
        self.control_sha_before = before
        # Retain the legacy field as a pre-transition alias for old consumers.
        self.canonical_control_transition_sha = before

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AcceptanceReceipt:
        allowed = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


def write_acceptance_receipt(control_dir: Path, receipt: AcceptanceReceipt) -> Path:
    target_dir = control_dir / "docs" / "project-control"
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = receipt.slice_id.replace("/", "-").replace(" ", "-").lower()
    target_path = target_dir / f"acceptance-receipt-{slug}.json"
    if target_path.exists():
        raise FileExistsError(f"acceptance receipt is immutable: {target_path.name}")
    _atomic_json(target_path, receipt.to_dict())
    return target_path


def read_all_acceptance_receipts(
    control_dir: Path,
    project_id: Optional[str] = None,
    lane: Optional[str] = None,
    require_accepted: bool = True,
) -> List[AcceptanceReceipt]:
    receipts_dir = control_dir / "docs" / "project-control"
    if not receipts_dir.is_dir():
        return []

    receipt_files = [p for p in receipts_dir.glob("acceptance-receipt-*.json") if _RECEIPT_RE.match(p.name)]
    if not receipt_files:
        return []

    parsed: List[AcceptanceReceipt] = []
    for p in receipt_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "receipt_id" in data and "candidate_sha" in data:
                r = AcceptanceReceipt.from_dict(data)
                if project_id is not None and r.project_id != project_id:
                    continue
                if lane is not None and r.lane != lane:
                    continue
                if require_accepted and r.acceptance_result != "ACCEPTED":
                    continue
                parsed.append(r)
        except Exception:
            continue

    return parsed


def read_latest_acceptance_receipt(
    control_dir: Path,
    project_id: Optional[str] = None,
    lane: Optional[str] = None,
    require_accepted: bool = True,
) -> Optional[AcceptanceReceipt]:
    receipts = read_all_acceptance_receipts(
        control_dir,
        project_id=project_id,
        lane=lane,
        require_accepted=require_accepted,
    )
    if not receipts:
        return None

    receipts.sort(key=lambda r: r.created_at, reverse=True)
    return receipts[0]
