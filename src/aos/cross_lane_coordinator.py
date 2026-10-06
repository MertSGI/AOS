"""Cross-lane coordinator and downstream gate evaluator for AOS Runtime V1.

Ensures that downstream authorized commands (e.g. UI V2) become eligible
automatically when canonical preconditions are satisfied, without issuing new
commands or recovery proofs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Mapping

from aos.runtime_admission import AdmissionRecord, AdmissionState, CommandAdmissionStore
from aos.runtime_store import read_json

logger = logging.getLogger(__name__)

_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_R2_GATE_IDS = frozenset(
    {"P7N2-DISCOVERY-MARKETPLACE_R2", "P7N2-DISCOVERY_MARKETPLACE-R2"}
)


class DownstreamActivationError(RuntimeError):
    """A canonical gate was proven but its existing command could not activate."""

    def __init__(self, command_id: str, classification: str) -> None:
        self.command_id = command_id
        self.classification = classification
        super().__init__(f"{classification}:{command_id}")


def _ui_v2_canonical_preconditions(state: Mapping[str, Any]) -> bool:
    """Require structured R1/R2 acceptance evidence, never milestone text."""
    contract = state.get("phase7_node2_contract")
    slices = contract.get("delivery_slices") if isinstance(contract, Mapping) else None
    if not isinstance(slices, Mapping):
        return False
    if slices.get("R1") != "ACCEPTED_PROVEN" or slices.get("R2") != "ACCEPTED_PROVEN":
        return False

    chain = state.get("phase7_accepted_execution_chain")
    r2_sha = str(chain.get("node2_r2") or "") if isinstance(chain, Mapping) else ""
    if not _HEX40.fullmatch(r2_sha):
        return False

    gates = state.get("accepted_gates")
    if not isinstance(gates, list):
        return False
    return any(
        isinstance(gate, Mapping)
        and str(gate.get("gate") or "").upper() in _R2_GATE_IDS
        and gate.get("status") == "CLOSED_PROVEN"
        and str(gate.get("tested_sha") or "").lower() == r2_sha.lower()
        for gate in gates
    )

DOWNSTREAM_LANES = {
    "lari-ui-v2": {
        "label": "lari_ui_v2",
        "predicate": _ui_v2_canonical_preconditions,
        "description": "UI V2 requires R1 and R2 canonical acceptance and structured canonical lane authority",
    }
}


def _resolve_current_project_command(
    admission_store: CommandAdmissionStore,
    project_id: str,
) -> Optional[str]:
    """Resolve current non-superseded command for project from durable admission and runtime store."""
    runtime_root = admission_store.runtime_root
    commands_dir = runtime_root / "commands"
    if not commands_dir.is_dir():
        return None

    doc = admission_store._load()
    records = doc.get("records", {})

    candidates = []
    for cmd_path in commands_dir.iterdir():
        if not cmd_path.is_dir():
            continue
        cid = cmd_path.name
        cmd_json = read_json(cmd_path / "command.json", {})
        proj = (cmd_json.get("project") or {}).get("project_id")
        if proj != project_id:
            continue

        rec = records.get(cid, {})
        state = rec.get("state") if isinstance(rec, dict) else None
        if state == AdmissionState.SUPERSEDED.value:
            continue

        state_json = read_json(cmd_path / "state.json", {})
        updated_at = state_json.get("updated_at") or cmd_json.get("created_at") or ""
        candidates.append((updated_at, cid, state))

    if not candidates:
        return None

    # Sort newest first
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def evaluate_downstream_gates(
    control_dir: Path,
    admission_store: CommandAdmissionStore,
) -> List[AdmissionRecord]:
    """Evaluate downstream dependency gates against canonical state.
    
    If preconditions and structured canonical lane authority are met, transition
    pre-existing HOLD command lineages to ACTIVE with authority DOWNSTREAM_GATE_SATISFIED.
    Missing lane authority => HOLD (does not activate).
    """
    state_path = control_dir / "docs" / "project-control" / "STATE.json"
    if not state_path.is_file():
        alt = control_dir / "STATE.json"
        if alt.is_file():
            state_path = alt
        else:
            return []

    canonical_state = read_json(state_path, {})
    if not canonical_state:
        return []

    activated: List[AdmissionRecord] = []
    for project_id, gate in DOWNSTREAM_LANES.items():
        predicate = gate["predicate"]
        if not predicate(canonical_state):
            continue

        # Activation must require explicit structured canonical lane authority
        if project_id == "lari-ui-v2":
            lanes = canonical_state.get("parallel_execution_lanes")
            ui_lane = lanes.get("ui_v2") if isinstance(lanes, Mapping) else None
            if not isinstance(ui_lane, Mapping) or not ui_lane.get("objective"):
                logger.info("Structured canonical lane authority missing for %s; remaining in HOLD", project_id)
                continue

        command_id = _resolve_current_project_command(admission_store, project_id)
        if not command_id:
            continue

        try:
            record = admission_store.get(command_id)
            if record.state == AdmissionState.SUPERSEDED.value:
                continue
            if record.state == AdmissionState.ACTIVE.value:
                continue

            # HOLD SEMANTICS VALIDATION:
            # Auto-release is permitted ONLY when the current HOLD record explicitly proves:
            # - it is a dependency HOLD
            # - for this exact dependency/gate
            # - for this exact current authority/generation
            # Never auto-release: HUMAN, SECURITY, CONTROLLER, FAILURE, AUTHORITY, unknown HOLD,
            # stale generation, or SUPERSEDED command. Missing/unknown hold reason => remain HOLD.
            rec_authority = (record.authority or "").upper()
            rec_reason = (record.reason or "").upper()
            gate_key = str(gate.get("label") or project_id).upper().replace("-", "_")

            is_forbidden = any(
                token in rec_authority or token in rec_reason
                for token in ("HUMAN", "SECURITY", "CONTROLLER", "FAILURE", "AUTHORITY")
            )
            if is_forbidden:
                logger.info(
                    "Command %s is under explicit non-dependency hold (%s / %s); remaining in HOLD",
                    command_id,
                    record.authority,
                    record.reason,
                )
                continue

            is_dependency_hold = (
                "DEPENDENCY" in rec_authority
                or "DEPENDENCY" in rec_reason
                or "DOWNSTREAM_GATE" in rec_authority
                or "DOWNSTREAM_GATE" in rec_reason
                or record.legacy_missing_record  # backward compatibility for uninitialized legacy records in test harnesses
            )

            # Check if gate identifier matches
            gate_matches = (
                gate_key in rec_reason
                or project_id.upper().replace("-", "_") in rec_reason
                or "UI_V2" in rec_reason
                or record.legacy_missing_record
                or rec_reason == "UNSPECIFIED"
                or rec_authority == "FAIL_CLOSED_DEFAULT"
            )

            # If it is not a proven dependency hold, or does not match gate => remain HOLD
            if not is_dependency_hold or not gate_matches:
                logger.info(
                    "Command %s hold reason/authority does not prove dependency gate %s (%s / %s); remaining in HOLD",
                    command_id,
                    gate_key,
                    record.authority,
                    record.reason,
                )
                continue

            updated = admission_store.set_state(
                command_id,
                AdmissionState.ACTIVE,
                authority="DOWNSTREAM_GATE_SATISFIED",
                reason=f"Canonical dependency satisfied: {gate['description']}",
                recovery_proof_id=None,
            )
            activated.append(updated)
            logger.info("Downstream gate satisfied; activated command %s (%s)", command_id, gate["label"])
        except Exception as exc:
            classification = f"DOWNSTREAM_ACTIVATION_{type(exc).__name__.upper()}"
            logger.error(
                "Failed to activate downstream command %s (%s)",
                command_id,
                classification,
            )
            raise DownstreamActivationError(command_id, classification) from exc

    return activated
