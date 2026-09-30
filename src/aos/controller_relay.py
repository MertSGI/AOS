"""AOS Native Controller Relay and Remote Outbox Publisher.

This module provides the first-class AOS-native relay publisher that continuously
emits operator-readable (LATEST.md), machine-readable (LATEST.json), and durable append-only
(events.jsonl) telemetry, along with sanitized remote publication via GitHub Issue Outbox.
"""
from __future__ import annotations

import json
import itertools
import os
import re
import ssl
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from aos.process_utils import run_headless
from aos.provenance import (
    ProvenanceError,
    get_authoritative_git_head,
    is_valid_full_sha,
    validate_materialized_runtime_provenance,
)
from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, read_json
from aos.self_diagnosis import SelfDiagnosisEngine
from aos.self_repair import BoundedSelfRepairEngine
from aos.platform_recovery import PlatformRecoveryCoordinator, SourceRepairExecutor
from aos.knowledge.hooks import ledger_from_runtime_config
from aos.knowledge.ledger import KnowledgeLedger
from aos.integrity_reconciler import IntegrityReconciler, OUTCOME_BUCKETS
from aos.lineage_truth import (
    CURRENT,
    durable_lineage_status,
    resolve_current_lineages,
)

try:
    import truststore
    def _create_tls_context() -> ssl.SSLContext:
        try:
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            return ssl.create_default_context()
except Exception:
    def _create_tls_context() -> ssl.SSLContext:
        return ssl.create_default_context()

RELAY_SCHEMA_VERSION = "1.0.0"

_SECRET_PATTERNS = [
    re.compile(r'(ghp_[a-zA-Z0-9]{30,40})'),
    re.compile(r'(github_pat_[a-zA-Z0-9_]{60,100})'),
    re.compile(r'(AIza[0-9A-Za-z-_]{30,45})'),
    re.compile(r'(gsk_[a-zA-Z0-9]{40,60})'),
    re.compile(r'(nvapi-[a-zA-Z0-9_-]{40,80})'),
    re.compile(r'Bearer\s+[A-Za-z0-9_\-\.]{20,}', re.IGNORECASE),
    re.compile(r'(token\s+[A-Za-z0-9_\-\.]{20,})', re.IGNORECASE),
]


def sanitize_text(text: str) -> str:
    """Strip secrets and credentials from relay texts."""
    if not text:
        return ""
    res = str(text)
    for pat in _SECRET_PATTERNS:
        res = pat.sub("[REDACTED_SECRET]", res)
    return res


def _writer_priority(writer_instance_id: str) -> int:
    value = str(writer_instance_id or "").casefold()
    if value.startswith("aos-supervisor-"):
        return 30
    if value.startswith("aos-api-"):
        return 20
    return 10


@dataclass
class LaneTelemetry:
    lane_id: str
    project_id: str
    command_id: str
    state: str
    worker_pid: Optional[int] = None
    completed_batches: int = 0
    current_batch: int = 0
    attempts: int = 0
    worker_execution_attempt_count: int = 0
    last_meaningful_progress_at: Optional[str] = None
    current_blocker: Optional[str] = None
    provider_backoff: bool = False
    next_retry_at: Optional[str] = None
    last_failure_class: Optional[str] = None
    product_mutation_count: int = 0
    lineage_status: str = "UNKNOWN"
    lineage_status_basis: str = "LEGACY_UNCLASSIFIED"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    superseded_by_command_id: Optional[str] = None


@dataclass
class RelaySnapshot:
    schema_version: str
    timestamp_utc: str
    writer: str
    writer_instance_id: str
    sequence_number: int
    runtime_slot: str
    runtime_source_sha: str
    runtime_health: str
    supervisor_pid: Optional[int]
    api_pid: Optional[int]
    lanes: List[Dict[str, Any]]
    active_command_count: int = 0
    running_lane_count: int = 0
    waiting_lane_count: int = 0
    completed_batch_delta: int = 0
    duplicate_completed_work: Union[int, str] = "UNKNOWN"
    lost_accepted_work: Union[int, str] = "UNKNOWN"
    cross_lane_write_scope_collision: Union[int, str] = "UNKNOWN"
    execution_outcomes: Dict[str, int] = field(default_factory=dict)
    total_executed: int = 0
    outcome_partition_valid: bool = False
    council_mode: str = "SHADOW_ONLY"
    council_quality_metrics: Dict[str, Any] = field(default_factory=dict)
    human_required: bool = False
    production: str = "NO_GO"
    provenance_status: str = "UNPROVEN"
    provenance_basis: str = "UNAVAILABLE"
    provenance_errors: List[str] = field(default_factory=list)
    remote_outbox_status: str = "DISABLED"
    remote_issue_number: Optional[int] = None
    last_remote_publish_at: Optional[str] = None
    aos_heartbeat: str = "ALIVE"
    forward_progress: str = "YES"
    no_progress_reason: Optional[str] = None
    first_workspace_productization_artifact: Optional[str] = None
    first_user_facing_ui_mutation: Optional[str] = None
    first_user_facing_mutation: Optional[str] = None
    browser_evidence_status: str = "AWAITING_BROWSER_SUITE_RUN"
    responsive_evidence_status: str = "AWAITING_BROWSER_SUITE_RUN"
    self_diagnosis_status: str = "SHADOW_ONLY"
    self_repair_shadow_status: str = "PREPARED"
    self_repair_eligibility: str = "ELIGIBLE_PENDING_GATE"
    self_repair_last_finding: str = "NONE"
    self_repair_required_evidence: str = "EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED"
    active_finding_count: int = 0
    blocking_finding_count: int = 0
    last_failure_class: str = "NONE"
    last_finding_severity: str = "NONE"
    last_autonomy_impact: str = "NONE"
    shadow_repair_proposal_status: str = "NONE"
    healthy_reasoning_provider_count: int = 0
    probe_eligible_reasoning_provider_count: int = 0
    unknown_reasoning_provider_count: int = 0
    provider_circuits_open: int = 0
    next_provider_probe_at: Optional[float] = None
    last_provider_success: Optional[str] = None
    all_reasoning_providers_unavailable: str = "NO"
    provider_probe_count: int = 0
    provider_failover_count: int = 0
    provider_details: List[Dict[str, Any]] = field(default_factory=list)
    current_selected_reasoning_provider: Optional[str] = None
    provider_discovery_status: str = "HEALTHY"
    provider_discovery_errors: Dict[str, Any] = field(default_factory=dict)
    enabled_reasoning_providers: List[str] = field(default_factory=list)
    provider_circuit_registry_paths: List[str] = field(default_factory=list)
    ag_backend_registered: bool = False
    ag_backend_availability: str = "UNKNOWN"
    ag_backend_enabled: bool = False
    ag_invocation_count: int = 0
    ag_direct_attempt_count: int = 0
    ag_planning_bridge_attempt_count: int = 0
    ag_total_attempt_count: int = 0
    ag_success_count: int = 0
    ag_degraded_count: int = 0
    ag_failure_count: int = 0
    ag_final_selection_count: int = 0
    backend_attempt_metrics: Dict[str, Dict[str, int]] = field(default_factory=dict)


class ControllerRelayPublisher:
    """AOS-native publisher for local and remote controller relay."""

    def __init__(
        self,
        local_relay_dir: Path,
        runtime_config: Dict[str, Any],
        writer_instance_id: Optional[str] = None,
        remote_repo: str = "MertSGI/AOS",
        source_repair_executor: Optional[SourceRepairExecutor] = None,
        knowledge_ledger: Optional[KnowledgeLedger] = None,
    ) -> None:
        self.local_relay_dir = local_relay_dir.expanduser().resolve()
        self.local_relay_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_config = runtime_config
        self.writer_instance_id = writer_instance_id or f"aos-supervisor-{os.getpid()}"
        self.remote_repo = remote_repo
        self.sequence_number = self._init_sequence()
        self.last_routine_remote_publish: float = 0.0
        self.last_checkpoint_epoch: float = time.time()
        self.remote_issue_number: Optional[int] = self._load_remote_issue_number()
        self.remote_outbox_status: str = "INITIALIZING"
        self._prev_completed_batches: Dict[str, int] = {}
        self._last_diagnosis_epoch: float = 0.0
        self._last_diagnosis_signature: Dict[str, Any] = {}
        self.diagnostics = SelfDiagnosisEngine(self.local_relay_dir / "self-diagnosis", self.runtime_config)
        self.repair_engine = BoundedSelfRepairEngine(
            self.local_relay_dir / "self-repair", self.diagnostics, self.runtime_config
        )
        self.platform_recovery = PlatformRecoveryCoordinator(
            self.local_relay_dir / "platform-recovery",
            self.repair_engine,
            source_base_sha=str(self.runtime_config.get("candidate_source_sha") or "UNKNOWN"),
            source_repair_executor=source_repair_executor,
            knowledge_ledger=(
                knowledge_ledger or ledger_from_runtime_config(self.runtime_config)
            ),
        )

    def _init_sequence(self) -> int:
        seq_path = self.local_relay_dir / "sequence.json"
        data = read_json(seq_path, {})
        return int(data.get("last_sequence", 0))

    def _save_sequence(self) -> None:
        seq_path = self.local_relay_dir / "sequence.json"
        atomic_json(seq_path, {"last_sequence": self.sequence_number, "updated_at": utc_now()})

    def _load_remote_issue_number(self) -> Optional[int]:
        state_file = self.local_relay_dir / "remote-outbox-state.json"
        data = read_json(state_file, {})
        num = data.get("issue_number")
        return int(num) if num else None

    def _save_remote_issue_number(self, issue_number: int) -> None:
        self.remote_issue_number = issue_number
        state_file = self.local_relay_dir / "remote-outbox-state.json"
        atomic_json(state_file, {
            "issue_number": issue_number,
            "repository": self.remote_repo,
            "updated_at": utc_now(),
        })

    def collect_snapshot(
        self,
        runtime_health_dict: Optional[Dict[str, Any]] = None,
        supervisor_pid: Optional[int] = None,
        force_diagnosis: bool = False,
    ) -> RelaySnapshot:
        """Gather fresh telemetry directly from durable runtime stores."""
        enrichment_deadline = time.monotonic() + 20.0
        self.sequence_number += 1
        self._save_sequence()

        rh = runtime_health_dict or {}
        runtime_slot = str(rh.get("runtime_slot_id") or self.runtime_config.get("runtime_slot_id") or "UNKNOWN")
        source_sha = str(rh.get("runtime_source_sha") or self.runtime_config.get("candidate_source_sha") or "UNKNOWN")
        runtime_health = str(rh.get("runtime_state") or "HEALTHY")
        api_pid = rh.get("pid")
        try:
            api_pid = int(api_pid) if api_pid else None
        except Exception:
            api_pid = None

        # An accepted immutable runtime is bound to materialization evidence,
        # not to a development checkout HEAD that can legitimately advance.
        slot_root = rh.get("runtime_slot_root") or self.runtime_config.get("runtime_slot_root")
        manifest_sha = None
        build_source_sha = None
        manifest_data: Dict[str, Any] = {}
        if slot_root:
            manifest_file = Path(slot_root) / "candidate-manifest.json"
            if manifest_file.is_file():
                try:
                    manifest_data = json.loads(
                        manifest_file.read_text(
                            encoding="utf-8"
                        )
                    )
                    manifest_sha = manifest_data.get("candidate_source_sha")
                    build_source_sha = manifest_data.get("build_source_sha")
                except Exception:
                    pass
            build_record = Path(slot_root) / "build-record.json"
            if not build_source_sha and build_record.is_file():
                try:
                    build_record_data = json.loads(build_record.read_text(encoding="utf-8"))
                    build_source_sha = build_record_data.get("build_source_sha") or build_record_data.get("source_sha")
                except Exception:
                    pass

        if not manifest_sha and is_valid_full_sha(source_sha):
            candidate_fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "runtime-v1" / "candidate" / source_sha / "candidate-manifest.json"
            if candidate_fallback.is_file():
                try:
                    manifest_data = json.loads(
                        candidate_fallback.read_text(
                            encoding="utf-8"
                        )
                    )
                    manifest_sha = manifest_data.get("candidate_source_sha")
                    if not build_source_sha:
                        build_source_sha = manifest_data.get("build_source_sha")
                except Exception:
                    pass

        local_git_head = None
        if manifest_data:
            provenance_basis = "IMMUTABLE_RUNTIME_SLOT"
            deployment_provenance = validate_materialized_runtime_provenance(
                manifest=manifest_data,
                runtime_source_sha=source_sha,
                runtime_asset_tree_sha256=(
                    rh.get("runtime_asset_tree_sha256")
                    or self.runtime_config.get("runtime_asset_tree_sha256")
                ),
            )
            prov_status = str(deployment_provenance.get("status", "UNPROVEN"))
            provenance_errors = list(deployment_provenance.get("errors", []) or [])
        else:
            provenance_basis = "LIVE_DEVELOPMENT_CHECKOUT"
            provenance_errors = ["CANDIDATE_MANIFEST_MISSING"]
            prov_status = "UNPROVEN"
            auth_repo_candidates = []
            if "authoritative_repo_path" in self.runtime_config:
                if self.runtime_config["authoritative_repo_path"]:
                    auth_repo_candidates.append(Path(self.runtime_config["authoritative_repo_path"]))
            else:
                for auth_root in self.runtime_config.get("authorized_roots", []):
                    auth_repo_candidates.append(Path(auth_root))
                auth_repo_candidates.extend([Path("C:/Projects/AOS-lane-b"), Path("C:/Projects/AOS")])
            for cand in auth_repo_candidates:
                try:
                    resolved_cand = cand.expanduser().resolve()
                    if (resolved_cand / ".git").exists():
                        local_git_head = get_authoritative_git_head(resolved_cand)
                        if local_git_head:
                            break
                except Exception:
                    continue

        # Scan lanes across commands
        runtime_root_str = self.runtime_config.get("runtime_root")
        store_roots = []
        if runtime_root_str:
            base_p = Path(runtime_root_str).expanduser().resolve()
            if (base_p / "commands").is_dir():
                store_roots.append(base_p)
            elif (base_p / "state" / "commands").is_dir():
                store_roots.append(base_p / "state")
            else:
                store_roots.append(base_p)
        if not store_roots:
            local_app_state = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "runtime-v1" / "state"
            if (local_app_state / "commands").is_dir():
                store_roots.append(local_app_state)

        lanes: List[Dict[str, Any]] = []
        deliberation_samples = 0
        delib_metrics: Dict[str, Any] = {
            "COUNCIL_TRIGGER_COUNT": 0,
            "COUNCIL_REAL_SAMPLE_COUNT": 0,
            "COUNCIL_REAL_SAMPLE_RATE": 0.0,
            "COUNCIL_AGREEMENT_RATE": 1.0,
            "COUNCIL_DISAGREEMENT_RATE": 0.0,
            "COUNCIL_CORRELATED_CONSENSUS_RATE": 0.0,
            "COUNCIL_POLICY_VIOLATIONS_CAUGHT": 0,
            "COUNCIL_PRIMARY_EXECUTION_INTERFERENCE_COUNT": 0,
        }

        first_workspace_artifact = None
        first_ui_mutation = None
        inspected_workspaces: set[str] = set()
        seen_commands: set[str] = set()
        total_batches_now = 0
        active_cmd_count = 0
        running_lanes = 0
        waiting_lanes = 0
        ag_registered = False
        ag_enabled = False
        ag_availability = "UNKNOWN"
        ag_invocations = 0
        backend_attempt_metrics: Dict[str, Dict[str, int]] = {}

        for s_root in store_roots:
            commands_dir = s_root / "commands"
            if not commands_dir.is_dir():
                continue
            for c_dir in itertools.islice(commands_dir.iterdir(), 200):
                if time.monotonic() >= enrichment_deadline:
                    break
                if not c_dir.is_dir():
                    continue
                cid = c_dir.name
                if cid in seen_commands:
                    continue
                seen_commands.add(cid)
                c_json = c_dir / "command.json"
                s_json = c_dir / "state.json"
                if not s_json.is_file():
                    continue
                try:
                    c_data = read_json(c_json, {})
                    s_data = read_json(s_json, {})
                    kernel_checkpoint = read_json(
                        c_dir / "project-runtime" / "planning-kernel-checkpoint.json", {}
                    )
                    last_receipt = (
                        kernel_checkpoint.get("last_receipt", {})
                        if isinstance(kernel_checkpoint, dict)
                        else {}
                    )
                    if isinstance(last_receipt, dict):
                        ag_registered = ag_registered or bool(last_receipt.get("ag_backend_registered"))
                        ag_enabled = ag_enabled or bool(last_receipt.get("ag_backend_enabled"))
                        if last_receipt.get("ag_backend_availability"):
                            ag_availability = str(last_receipt["ag_backend_availability"])
                        receipt_metrics = last_receipt.get("backend_attempt_metrics", {})
                        if isinstance(receipt_metrics, dict):
                            for backend_id, metrics in receipt_metrics.items():
                                if not isinstance(metrics, dict):
                                    continue
                                aggregate = backend_attempt_metrics.setdefault(str(backend_id), {})
                                for metric, value in metrics.items():
                                    if isinstance(value, int) and value >= 0:
                                        aggregate[str(metric)] = int(
                                            aggregate.get(str(metric), 0) or 0
                                        ) + value
                        ag_invocations += int(last_receipt.get("ag_invocation_count", 0) or 0)
                    proj = (c_data.get("project") or {}).get("project_id") or "lari"
                    lane_name = "Lane C" if "ui" in proj.lower() else "Lane A"
                    state_str = s_data.get("state", "UNKNOWN")
                    lineage_status, lineage_basis = durable_lineage_status(c_data, s_data)
                    worker_pid = s_data.get("worker_pid")
                    batches = int(s_data.get("completed_batch_count", 0) or 0)
                    total_batches_now += batches
                    attempts = int(s_data.get("attempts", 0) or 0)
                    retry_epoch = s_data.get("retry_after_epoch")
                    retry_str = None
                    backoff = False
                    if retry_epoch:
                        try:
                            if time.time() < float(retry_epoch):
                                backoff = True
                                retry_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(retry_epoch)))
                        except Exception:
                            pass

                    if state_str in ("RUNNING", "RECOVERING", "EXECUTING"):
                        active_cmd_count += 1
                        running_lanes += 1
                    elif state_str.startswith("WAITING_"):
                        waiting_lanes += 1

                    # Detect mutations in project workspace
                    # Markdown status files are workspace productization artifacts, NOT user-facing UI mutations.
                    p_ws = (c_data.get("project") or {}).get("workspace")
                    mutations = 0
                    if p_ws:
                        workspace_key = str(Path(p_ws).expanduser().resolve())
                        if workspace_key in inspected_workspaces:
                            p_ws = None
                        else:
                            inspected_workspaces.add(workspace_key)
                    if p_ws and Path(p_ws).is_dir():
                        ws_path = Path(p_ws)
                        mut_doc = ws_path / "visual_productization_status.md"
                        if mut_doc.is_file():
                            mutations += 1
                            if not first_workspace_artifact:
                                first_workspace_artifact = "visual_productization_status.md"

                        # Check for actual user-facing browser code mutations relative to baseline (TSX/JSX/HTML/CSS/JS)
                        # Touchless preexisting workspace files do not count as mutations.
                        try:
                            if time.monotonic() + 5.0 >= enrichment_deadline:
                                raise TimeoutError("relay enrichment budget exhausted")
                            git_proc = run_headless(
                                ["git", "-C", str(ws_path), "status", "--porcelain"],
                                timeout=5,
                                check=False,
                            )
                            if git_proc.returncode == 0:
                                ui_exts = (".tsx", ".jsx", ".html", ".css", ".vue", ".svelte")
                                for line in git_proc.stdout.splitlines():
                                    line_clean = line.strip()
                                    if not line_clean or len(line_clean) < 3:
                                        continue
                                    rel_file = line_clean[2:].strip()
                                    if any(rel_file.endswith(ext) for ext in ui_exts):
                                        if "node_modules" not in rel_file and ".git" not in rel_file:
                                            mutations += 1
                                            if not first_ui_mutation:
                                                first_ui_mutation = Path(rel_file).name
                        except Exception:
                            pass

                    lanes.append(asdict(LaneTelemetry(
                        lane_id=lane_name,
                        project_id=proj,
                        command_id=cid,
                        state=state_str,
                        worker_pid=int(worker_pid) if worker_pid else None,
                        completed_batches=batches,
                        current_batch=batches + 1,
                        attempts=attempts,
                        worker_execution_attempt_count=attempts,
                        last_meaningful_progress_at=s_data.get("updated_at"),
                        current_blocker=s_data.get("disposition") if state_str.startswith("WAITING_") else None,
                        provider_backoff=backoff,
                        next_retry_at=retry_str,
                        last_failure_class=s_data.get("failure_class"),
                        product_mutation_count=mutations,
                        lineage_status=lineage_status,
                        lineage_status_basis=lineage_basis,
                        created_at=c_data.get("created_at") or s_data.get("created_at"),
                        updated_at=s_data.get("updated_at"),
                        superseded_by_command_id=(
                            s_data.get("superseded_by_command_id")
                            or c_data.get("superseded_by_command_id")
                        ),
                    )))
                except Exception:
                    pass

                # Scan ledger for deliberation metrics
                ledger = c_dir / "project-runtime" / "deliberation" / "deliberation-shadow-ledger.jsonl"
                if ledger.is_file():
                    try:
                        lines = [json.loads(line) for line in ledger.read_text("utf-8").strip().splitlines() if line.strip()]
                        deliberation_samples += len(lines)
                        delib_metrics["COUNCIL_TRIGGER_COUNT"] += len(lines)
                        for item in lines:
                            if item.get("is_real_execution") and item.get("quorum_obtained"):
                                delib_metrics["COUNCIL_REAL_SAMPLE_COUNT"] += 1
                            if item.get("policy_violation_caught"):
                                delib_metrics["COUNCIL_POLICY_VIOLATIONS_CAUGHT"] += 1
                    except Exception:
                        pass

        lineage_projection = resolve_current_lineages(lanes)
        for lane in lanes:
            projected = lineage_projection.get(lane["command_id"], {})
            lane["lineage_status"] = projected.get("status", lane["lineage_status"])
            lane["lineage_status_basis"] = projected.get(
                "basis", lane["lineage_status_basis"]
            )

        # Sort lanes deterministically: current active/recovering first, then by lane_id.
        def lane_sort_key(x: Dict[str, Any]) -> tuple:
            is_active = x["state"] in (
                "RUNNING",
                "RECOVERING",
                "EXECUTING",
                "WAITING_FOR_REASONING_PROVIDER",
                "WAITING_FOR_SOURCE_TRANSPORT",
                "QUEUED",
            )
            is_current = x.get("lineage_status") == CURRENT
            return (0 if is_current and is_active else 1, x["lane_id"], x["command_id"])

        lanes.sort(key=lane_sort_key)

        # Active tracked lanes (non-terminal)
        active_tracked_lanes = [
            l for l in lanes
            if l.get("lineage_status") == CURRENT
            and l["state"] in (
                "RUNNING",
                "RECOVERING",
                "EXECUTING",
                "WAITING_FOR_REASONING_PROVIDER",
                "WAITING_FOR_SOURCE_TRANSPORT",
                "QUEUED",
            )
        ]
        if not active_tracked_lanes and lanes:
            active_tracked_lanes = [
                lane for lane in lanes if lane.get("lineage_status") == CURRENT
            ]

        # Calculate forward progress and delta
        batch_delta = 0
        for l in lanes:
            cid = l["command_id"]
            prev_b = self._prev_completed_batches.get(cid, l["completed_batches"])
            batch_delta += max(0, l["completed_batches"] - prev_b)
            self._prev_completed_batches[cid] = l["completed_batches"]

        # Forward progress semantics:
        # RUNNING != meaningful progress. FORWARD_PROGRESS=YES requires an observed delta (e.g. batch_delta > 0).
        # If workers are alive/running but no meaningful delta occurred: FORWARD_PROGRESS=NO, ACTIVE_WITHOUT_MEANINGFUL_DELTA.
        if batch_delta > 0:
            forward_prog = "YES"
            no_prog_reason = None
        else:
            forward_prog = "NO"
            if running_lanes > 0:
                no_prog_reason = "ACTIVE_WITHOUT_MEANINGFUL_DELTA"
            elif waiting_lanes > 0:
                no_prog_reason = "WAITING_FOR_REASONING_PROVIDER_BACKOFF"
            else:
                no_prog_reason = "NO_ACTIVE_RUNNING_LANES"

        # Aggregate from generic current-lineage truth. Protected command IDs
        # remain safety identities elsewhere, never telemetry authority here.
        human_req = any(
            l.get("state") == "HUMAN_REQUIRED"
            for l in lanes
            if l.get("lineage_status") == CURRENT
        )

        # Integrity telemetry is calculated only from durable, explicitly
        # instrumented stores. Missing evidence remains UNKNOWN.
        duplicate_work: Union[int, str] = "UNKNOWN"
        lost_work: Union[int, str] = "UNKNOWN"
        scope_collision: Union[int, str] = "UNKNOWN"
        outcome_counts = {bucket: 0 for bucket in OUTCOME_BUCKETS}
        integrity_reports = []
        for store_root in store_roots:
            integrity_root = store_root / "integrity"
            if not (integrity_root / "instrumentation.json").is_file():
                continue
            report = IntegrityReconciler(integrity_root, initialize=False).reconcile()
            integrity_reports.append(report)
        if integrity_reports:
            sufficient_reports = [
                report for report in integrity_reports
                if report.evidence_status == "SUFFICIENT"
            ]
            if len(sufficient_reports) == len(integrity_reports):
                duplicate_work = sum(int(report.duplicate_completed_work) for report in sufficient_reports)
                lost_work = sum(int(report.lost_accepted_work) for report in sufficient_reports)
                scope_collision = sum(
                    int(report.cross_lane_write_scope_collision) for report in sufficient_reports
                )
            for report in integrity_reports:
                for bucket in OUTCOME_BUCKETS:
                    outcome_counts[bucket] += int(report.outcome_counts.get(bucket, 0))
        total_executed = sum(report.total_executed for report in integrity_reports)
        outcome_partition_valid = bool(integrity_reports) and all(
            report.outcome_partition_valid for report in integrity_reports
        )

        # Diagnostic lifecycle scheduling: event-driven plus bounded periodic fallback (<= every 300s)
        current_diag_sig = {
            "runtime_health": runtime_health,
            "provenance_status": prov_status,
            "lane_states": {l.get("lane_id"): l.get("state") for l in active_tracked_lanes},
            "lane_backoffs": {l.get("lane_id"): bool(l.get("provider_backoff")) for l in active_tracked_lanes},
            "completed_batches": total_batches_now,
            "batch_delta": batch_delta,
            "outbox_status": self.remote_outbox_status,
        }

        now_epoch = time.time()
        time_since_diag = now_epoch - self._last_diagnosis_epoch
        is_periodic_fallback = (time_since_diag >= 300.0) or (self._last_diagnosis_epoch == 0.0)
        has_meaningful_event = force_diagnosis or (current_diag_sig != self._last_diagnosis_signature)

        if has_meaningful_event or is_periodic_fallback:
            try:
                # Active tracked lanes only for lane-specific diagnostics to prevent historical stopped command false-positives
                diag_findings = self.diagnostics.diagnose_runtime(
                    runtime_health=rh,
                    lanes=active_tracked_lanes,
                    relay_snapshot={
                        "forward_progress": forward_prog,
                        "no_progress_reason": no_prog_reason,
                        "running_lane_count": running_lanes,
                    },
                    provenance_status=prov_status,
                    build_source_sha=build_source_sha,
                    candidate_manifest_sha=manifest_sha,
                    runtime_source_sha=source_sha,
                    local_git_head=local_git_head,
                    outbox_status=self.remote_outbox_status,
                )
                # Diagnosis is continuously connected to the platform recovery
                # plane, but relay publication only classifies/persists jobs.
                # Actuation remains a separate authenticated, governed action.
                for finding in diag_findings:
                    try:
                        self.platform_recovery.observe_finding(finding.finding_id)
                    except Exception:
                        continue
                # Reconcile active findings against currently observed fingerprints
                observed_fps = {f.fingerprint for f in diag_findings}
                self.diagnostics.reconcile_active_findings(observed_fps)
                self._last_diagnosis_epoch = now_epoch
                self._last_diagnosis_signature = current_diag_sig
                diag_summary = self.diagnostics.summarize_status()
            except Exception:
                diag_summary = {
                    "self_diagnosis_status": "SHADOW_ONLY",
                    "self_repair_eligibility": "ELIGIBLE_PENDING_GATE",
                    "last_finding_id": "NONE",
                    "active_finding_count": 0,
                    "blocking_finding_count": 0,
                    "last_failure_class": "NONE",
                    "last_finding_severity": "NONE",
                    "last_autonomy_impact": "NONE",
                    "shadow_repair_proposal_status": "NONE",
                }
        else:
            # Heartbeat tick between events: decoupled, do not run diagnosis, retrieve cached durable summary
            try:
                diag_summary = self.diagnostics.summarize_status()
            except Exception:
                diag_summary = {
                    "self_diagnosis_status": "SHADOW_ONLY",
                    "self_repair_eligibility": "ELIGIBLE_PENDING_GATE",
                    "last_finding_id": "NONE",
                    "active_finding_count": 0,
                    "blocking_finding_count": 0,
                    "last_failure_class": "NONE",
                    "last_finding_severity": "NONE",
                    "last_autonomy_impact": "NONE",
                    "shadow_repair_proposal_status": "NONE",
                }

        ag_metrics = backend_attempt_metrics.get("antigravity", {})
        ag_total_attempts = int(ag_metrics.get("attempt_count", 0) or 0)
        return RelaySnapshot(
            schema_version=RELAY_SCHEMA_VERSION,
            timestamp_utc=utc_now(),
            writer="AOS",
            writer_instance_id=self.writer_instance_id,
            sequence_number=self.sequence_number,
            runtime_slot=runtime_slot,
            runtime_source_sha=source_sha,
            runtime_health=runtime_health,
            supervisor_pid=supervisor_pid,
            api_pid=api_pid,
            lanes=lanes,
            active_command_count=active_cmd_count,
            running_lane_count=running_lanes,
            waiting_lane_count=waiting_lanes,
            completed_batch_delta=batch_delta,
            duplicate_completed_work=duplicate_work,
            lost_accepted_work=lost_work,
            cross_lane_write_scope_collision=scope_collision,
            execution_outcomes=outcome_counts,
            total_executed=total_executed,
            outcome_partition_valid=outcome_partition_valid,
            council_mode="SHADOW_ONLY",
            council_quality_metrics=delib_metrics,
            human_required=human_req,
            production="NO_GO",
            provenance_status=prov_status,
            provenance_basis=provenance_basis,
            provenance_errors=provenance_errors,
            remote_outbox_status=self.remote_outbox_status,
            remote_issue_number=self.remote_issue_number,
            aos_heartbeat="ALIVE",
            forward_progress=forward_prog,
            no_progress_reason=no_prog_reason,
            first_workspace_productization_artifact=first_workspace_artifact,
            first_user_facing_ui_mutation=first_ui_mutation,
            first_user_facing_mutation=first_ui_mutation,
            browser_evidence_status="AWAITING_BROWSER_SUITE_RUN",
            responsive_evidence_status="AWAITING_BROWSER_SUITE_RUN",
            self_diagnosis_status=diag_summary.get("self_diagnosis_status", "SHADOW_ONLY"),
            self_repair_shadow_status="PREPARED",
            self_repair_eligibility=diag_summary.get("self_repair_eligibility", "ELIGIBLE_PENDING_GATE"),
            self_repair_last_finding=diag_summary.get("last_finding_id", "NONE"),
            self_repair_required_evidence="EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED",
            active_finding_count=diag_summary.get("active_finding_count", 0),
            blocking_finding_count=diag_summary.get("blocking_finding_count", 0),
            last_failure_class=diag_summary.get("last_failure_class", "NONE"),
            last_finding_severity=diag_summary.get("last_finding_severity", "NONE"),
            last_autonomy_impact=diag_summary.get("last_autonomy_impact", "NONE"),
            shadow_repair_proposal_status=diag_summary.get("shadow_repair_proposal_status", "NONE"),
            healthy_reasoning_provider_count=int(rh.get("healthy_reasoning_provider_count", 0) or 0),
            probe_eligible_reasoning_provider_count=int(rh.get("probe_eligible_reasoning_provider_count", 0) or 0),
            unknown_reasoning_provider_count=int(rh.get("unknown_reasoning_provider_count", 0) or 0),
            provider_circuits_open=int(rh.get("provider_circuits_open", 0) or 0),
            next_provider_probe_at=rh.get("next_provider_probe_at"),
            last_provider_success=rh.get("last_provider_success"),
            all_reasoning_providers_unavailable="YES" if rh.get("all_reasoning_providers_unavailable") else "NO",
            provider_probe_count=int(rh.get("provider_probe_count", 0) or 0),
            provider_failover_count=int(rh.get("provider_failover_count", 0) or 0),
            provider_details=list(rh.get("provider_details", []) or []),
            current_selected_reasoning_provider=rh.get("current_selected_reasoning_provider"),
            provider_discovery_status=str(rh.get("provider_discovery_status") or "HEALTHY"),
            provider_discovery_errors=dict(rh.get("provider_discovery_errors") or {}),
            enabled_reasoning_providers=list(rh.get("enabled_reasoning_providers", []) or []),
            provider_circuit_registry_paths=list(rh.get("provider_circuit_registry_paths", []) or []),
            ag_backend_registered=ag_registered,
            ag_backend_availability=ag_availability,
            ag_backend_enabled=ag_enabled,
            ag_invocation_count=ag_total_attempts or ag_invocations,
            ag_direct_attempt_count=int(
                ag_metrics.get("direct_agentic_attempt_count", 0) or 0
            ),
            ag_planning_bridge_attempt_count=int(
                ag_metrics.get("planning_bridge_attempt_count", 0) or 0
            ),
            ag_total_attempt_count=ag_total_attempts,
            ag_success_count=int(ag_metrics.get("success_count", 0) or 0),
            ag_degraded_count=int(ag_metrics.get("degraded_count", 0) or 0),
            ag_failure_count=int(ag_metrics.get("failure_count", 0) or 0),
            ag_final_selection_count=int(
                ag_metrics.get("final_selection_count", 0) or 0
            ),
            backend_attempt_metrics=backend_attempt_metrics,
        )

    def render_markdown(self, snapshot: RelaySnapshot) -> str:
        """Render operator-readable LATEST.md strictly from durable snapshot."""
        # Highlight active/tracked lanes prominently
        active_lines = []
        history_lines = []
        for l in snapshot.lanes:
            line = (
                f"- **{l['lane_id']}** (`{l['project_id']}`): State `{l['state']}`, "
                f"Batches `{l['completed_batches']}`, Attempts `{l['attempts']}`, "
                f"Worker PID `{l['worker_pid'] or 'NONE'}`, Command `{l['command_id']}`"
            )
            if l.get("lineage_status") == CURRENT and l["state"] in (
                "RUNNING",
                "RECOVERING",
                "EXECUTING",
                "WAITING_FOR_REASONING_PROVIDER",
                "WAITING_FOR_SOURCE_TRANSPORT",
                "QUEUED",
                "HUMAN_REQUIRED",
                "TECHNICAL_HOLD",
            ):
                active_lines.append(line)
            else:
                history_lines.append(line)

        if active_lines:
            lanes_text = "\n".join(active_lines)
            if history_lines:
                lanes_text += f"\n\n<details><summary>Historical Completed/Stopped Commands ({len(history_lines)})</summary>\n\n"
                lanes_text += "\n".join(history_lines)
                lanes_text += "\n\n</details>"
        elif history_lines:
            lanes_text = "\n".join(history_lines)
        else:
            lanes_text = "No active lanes detected."

        lost_desc = f"Lost accepted work: {snapshot.lost_accepted_work}" if snapshot.lost_accepted_work != "UNKNOWN" else "Lost accepted work: UNKNOWN"
        dup_desc = f"duplicate completed work: {snapshot.duplicate_completed_work}" if snapshot.duplicate_completed_work != "UNKNOWN" else "duplicate completed work: UNKNOWN"

        md = f"""# AOS Controller Relay

REPORT_TYPE=AOS_NATIVE_HEARTBEAT_AND_CHECKPOINT

AOS NATIVE RELAY ACTIVE ON PORT 8770: Slot `{snapshot.runtime_slot}` (SHA `{snapshot.runtime_source_sha[:12] if len(snapshot.runtime_source_sha) >= 12 else snapshot.runtime_source_sha}`). Runtime Health: `{snapshot.runtime_health}` (API PID `{snapshot.api_pid or 'NONE'}`, Supervisor PID `{snapshot.supervisor_pid or 'NONE'}`). Provenance: `{snapshot.provenance_status}`. Production Gate: `{snapshot.production}`. Council Mode: `{snapshot.council_mode}`. {lost_desc}; {dup_desc}; autonomous continuation active.

=== AOS CONTROLLER RELAY ===
TIMESTAMP_UTC={snapshot.timestamp_utc}
WRITER={snapshot.writer}
WRITER_INSTANCE_ID={snapshot.writer_instance_id}
SEQUENCE_NUMBER={snapshot.sequence_number}
STATUS=ACTIVE

CURRENT_RUNTIME_SLOT={snapshot.runtime_slot}
CURRENT_RUNTIME_SHA={snapshot.runtime_source_sha}
RUNTIME_HEALTH={snapshot.runtime_health}
SUPERVISOR_PID={snapshot.supervisor_pid or 'NONE'}
RUNTIME_API_PID={snapshot.api_pid or 'NONE'}
PROVENANCE_STATUS={snapshot.provenance_status}
PROVENANCE_BASIS={snapshot.provenance_basis}
PROVENANCE_ERRORS_JSON={json.dumps(snapshot.provenance_errors)}

### HEARTBEAT & FORWARD PROGRESS
AOS_HEARTBEAT={snapshot.aos_heartbeat}
FORWARD_PROGRESS={snapshot.forward_progress}
NO_PROGRESS_REASON={snapshot.no_progress_reason or 'NONE'}
ACTIVE_COMMAND_COUNT={snapshot.active_command_count}
RUNNING_LANE_COUNT={snapshot.running_lane_count}
WAITING_LANE_COUNT={snapshot.waiting_lane_count}
COMPLETED_BATCH_DELTA={snapshot.completed_batch_delta}

### LANES TELEMETRY
{lanes_text}

### PRODUCT & VERIFICATION EVIDENCE
FIRST_WORKSPACE_PRODUCTIZATION_ARTIFACT={snapshot.first_workspace_productization_artifact or 'NONE'}
FIRST_USER_FACING_UI_MUTATION={snapshot.first_user_facing_ui_mutation or 'NONE'}
FIRST_USER_FACING_MUTATION={snapshot.first_user_facing_mutation or 'NONE'}
BROWSER_EVIDENCE_STATUS={snapshot.browser_evidence_status}
RESPONSIVE_EVIDENCE_STATUS={snapshot.responsive_evidence_status}

### DELIBERATION COUNCIL METRICS
COUNCIL_MODE={snapshot.council_mode}
COUNCIL_TRIGGER_COUNT={snapshot.council_quality_metrics.get('COUNCIL_TRIGGER_COUNT', 0)}
COUNCIL_REAL_SAMPLE_COUNT={snapshot.council_quality_metrics.get('COUNCIL_REAL_SAMPLE_COUNT', 0)}
COUNCIL_POLICY_VIOLATIONS_CAUGHT={snapshot.council_quality_metrics.get('COUNCIL_POLICY_VIOLATIONS_CAUGHT', 0)}
COUNCIL_PRIMARY_INTERFERENCE={snapshot.council_quality_metrics.get('COUNCIL_PRIMARY_EXECUTION_INTERFERENCE_COUNT', 0)}

### INTEGRITY INVARIANTS
DUPLICATE_COMPLETED_WORK={snapshot.duplicate_completed_work}
LOST_ACCEPTED_WORK={snapshot.lost_accepted_work}
CROSS_LANE_WRITE_SCOPE_COLLISION={snapshot.cross_lane_write_scope_collision}
HUMAN_REQUIRED={'YES' if snapshot.human_required else 'NO'}
PRODUCTION={snapshot.production}
EXECUTION_OUTCOMES_JSON={json.dumps(snapshot.execution_outcomes, sort_keys=True)}
TOTAL_EXECUTED={snapshot.total_executed}
OUTCOME_PARTITION_VALID={'YES' if snapshot.outcome_partition_valid else 'NO'}

### AGENTIC EXECUTOR TRUTH
AG_BACKEND_REGISTERED={'YES' if snapshot.ag_backend_registered else 'NO'}
AG_BACKEND_AVAILABILITY={snapshot.ag_backend_availability}
AG_BACKEND_ENABLED={'YES' if snapshot.ag_backend_enabled else 'NO'}
AG_INVOCATION_COUNT={snapshot.ag_invocation_count}
AG_DIRECT_ATTEMPT_COUNT={snapshot.ag_direct_attempt_count}
AG_PLANNING_BRIDGE_ATTEMPT_COUNT={snapshot.ag_planning_bridge_attempt_count}
AG_TOTAL_ATTEMPT_COUNT={snapshot.ag_total_attempt_count}
AG_SUCCESS_COUNT={snapshot.ag_success_count}
AG_DEGRADED_COUNT={snapshot.ag_degraded_count}
AG_FAILURE_COUNT={snapshot.ag_failure_count}
AG_FINAL_SELECTION_COUNT={snapshot.ag_final_selection_count}
BACKEND_ATTEMPT_METRICS_JSON={json.dumps(snapshot.backend_attempt_metrics, sort_keys=True)}

### REMOTE OUTBOX
REMOTE_OUTBOX_TRANSPORT=GITHUB_ISSUE
REMOTE_OUTBOX_STATUS={snapshot.remote_outbox_status}
REMOTE_ISSUE_NUMBER={snapshot.remote_issue_number or 'NONE'}
LAST_REMOTE_PUBLISH_AT={snapshot.last_remote_publish_at or 'NONE'}

### REASONING PROVIDERS & CIRCUIT BREAKERS
PROVIDER_DISCOVERY_STATUS={snapshot.provider_discovery_status}
PROVIDER_DISCOVERY_ERRORS={json.dumps(snapshot.provider_discovery_errors, sort_keys=True)}
ENABLED_REASONING_PROVIDERS={json.dumps(snapshot.enabled_reasoning_providers)}
PROVIDER_CIRCUIT_REGISTRY_PATHS={json.dumps(snapshot.provider_circuit_registry_paths)}
HEALTHY_REASONING_PROVIDER_COUNT={snapshot.healthy_reasoning_provider_count}
PROBE_ELIGIBLE_REASONING_PROVIDER_COUNT={snapshot.probe_eligible_reasoning_provider_count}
UNKNOWN_REASONING_PROVIDER_COUNT={snapshot.unknown_reasoning_provider_count}
PROVIDER_CIRCUITS_OPEN={snapshot.provider_circuits_open}
ALL_REASONING_PROVIDERS_UNAVAILABLE={snapshot.all_reasoning_providers_unavailable}
NEXT_PROVIDER_PROBE_AT={snapshot.next_provider_probe_at or 'NONE'}
LAST_PROVIDER_SUCCESS={snapshot.last_provider_success or 'NONE'}
PROVIDER_PROBE_COUNT={snapshot.provider_probe_count}
PROVIDER_FAILOVER_COUNT={snapshot.provider_failover_count}
CURRENT_SELECTED_REASONING_PROVIDER={snapshot.current_selected_reasoning_provider or 'NONE'}
PROVIDER_DETAILS_JSON={json.dumps(snapshot.provider_details, sort_keys=True)}

### SELF-REPAIR OBSERVABILITY (SHADOW ONLY)
SELF_DIAGNOSIS_STATUS={snapshot.self_diagnosis_status}
SELF_REPAIR_SHADOW_STATUS={snapshot.self_repair_shadow_status}
SELF_REPAIR_ELIGIBILITY={snapshot.self_repair_eligibility}
SELF_REPAIR_LAST_FINDING={snapshot.self_repair_last_finding}
SELF_REPAIR_REQUIRED_EVIDENCE={snapshot.self_repair_required_evidence}
ACTIVE_FINDING_COUNT={snapshot.active_finding_count}
BLOCKING_FINDING_COUNT={snapshot.blocking_finding_count}
LAST_FAILURE_CLASS={snapshot.last_failure_class}
LAST_FINDING_SEVERITY={snapshot.last_finding_severity}
LAST_AUTONOMY_IMPACT={snapshot.last_autonomy_impact}
SHADOW_REPAIR_PROPOSAL_STATUS={snapshot.shadow_repair_proposal_status}
"""
        return sanitize_text(md)

    def publish_local(self, snapshot: RelaySnapshot, is_checkpoint: bool = False) -> None:
        """Atomically replace LATEST.md and LATEST.json, and append to events.jsonl."""
        md_content = self.render_markdown(snapshot)
        json_content = json.dumps(asdict(snapshot), indent=2, ensure_ascii=False)

        # Atomic write LATEST.md
        latest_md = self.local_relay_dir / "LATEST.md"
        latest_json = self.local_relay_dir / "LATEST.json"

        # Concurrency safety: check sequence in existing json if present
        existing_json = read_json(latest_json, {})
        existing_writer = existing_json.get("writer", "")
        existing_writer_instance = str(existing_json.get("writer_instance_id") or "")
        existing_seq = int(existing_json.get("sequence_number", 0) or 0)

        # Reject out-of-order writes from fallback writers if native AOS already wrote newer
        if existing_writer == "AOS" and snapshot.writer != "AOS" and snapshot.sequence_number <= existing_seq:
            return
        if (
            existing_writer == "AOS"
            and _writer_priority(existing_writer_instance) > _writer_priority(snapshot.writer_instance_id)
        ):
            return

        # 1. Atomic LATEST.json
        data_dict = asdict(snapshot)
        atomic_json(latest_json, data_dict)

        # 2. Atomic LATEST.md
        tmp_md = latest_md.with_suffix(".tmp")
        with open(tmp_md, "w", encoding="utf-8", newline="\n") as f:
            f.write(md_content)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp_md, latest_md)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(min(0.05 * (2 ** attempt), 0.5))

        # Append structured event log
        events_file = self.local_relay_dir / "events.jsonl"
        event_entry = {
            "timestamp": snapshot.timestamp_utc,
            "sequence_number": snapshot.sequence_number,
            "event_type": "CHECKPOINT" if is_checkpoint else "HEARTBEAT",
            "writer": snapshot.writer,
            "runtime_health": snapshot.runtime_health,
            "provenance_status": snapshot.provenance_status,
            "active_command_count": snapshot.active_command_count,
            "running_lane_count": snapshot.running_lane_count,
            "human_required": snapshot.human_required,
            "remote_outbox_status": snapshot.remote_outbox_status,
            "self_diagnosis_status": snapshot.self_diagnosis_status,
            "active_finding_count": snapshot.active_finding_count,
            "blocking_finding_count": snapshot.blocking_finding_count,
        }
        with open(events_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event_entry, ensure_ascii=False) + "\n")
            f.flush()

    def _get_github_token(self) -> Optional[str]:
        """Obtain sanitized GitHub token from environment or secure store."""
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            return token.strip()
        try:
            from aos.secure_store import read_provider_secret
            sec = read_provider_secret("GITHUB")
            if sec:
                return sec.strip()
        except Exception:
            pass
        return None

    def publish_remote(self, snapshot: RelaySnapshot, major_gate_reason: Optional[str] = None) -> bool:
        """Publish sanitized status to remote GitHub Issue Outbox.

        Follows update policy:
        - Major gate event publishes immediately.
        - Routine heartbeat throttled to at most once every 15 minutes.
        - Fails open without blocking runtime if token is unavailable.
        """
        now = time.time()
        is_major_gate = bool(major_gate_reason)
        if not is_major_gate and (now - self.last_routine_remote_publish < 900):
            return False

        token = self._get_github_token()
        gh_available = False
        if not token:
            # Check if GitHub CLI is authenticated
            try:
                proc = run_headless(["gh", "auth", "status"], timeout=5, check=False)
                if proc.returncode == 0:
                    gh_available = True
            except Exception:
                gh_available = False

        if not token and not gh_available:
            self.remote_outbox_status = "HUMAN_REQUIRED_AUTH_SETUP"
            snapshot.remote_outbox_status = self.remote_outbox_status
            return False

        # Build issue body
        body_text = self.render_markdown(snapshot)
        if major_gate_reason:
            body_text = f"> [!IMPORTANT]\n> **MAJOR-GATE EVENT**: {sanitize_text(major_gate_reason)}\n\n" + body_text

        # 1. Transport using gh CLI if available and no direct token
        if gh_available and not token:
            try:
                if not self.remote_issue_number:
                    proc_list = run_headless(
                        ["gh", "issue", "list", "--repo", self.remote_repo, "--state", "open", "--json", "number,title", "--limit", "30"],
                        timeout=10,
                        check=False,
                    )
                    if proc_list.returncode == 0:
                        try:
                            items = json.loads(proc_list.stdout)
                            for it in items:
                                if it.get("title") == "AOS Controller Relay":
                                    self._save_remote_issue_number(int(it["number"]))
                                    break
                        except Exception:
                            pass

                if not self.remote_issue_number:
                    # Create without requiring labels
                    proc_create = run_headless(
                        ["gh", "issue", "create", "--repo", self.remote_repo, "--title", "AOS Controller Relay", "--body", body_text],
                        timeout=10,
                        check=False,
                    )
                    if proc_create.returncode == 0:
                        # Parse URL from output to get issue number
                        out_str = proc_create.stdout.strip()
                        # Output format: https://github.com/MertSGI/AOS/issues/123
                        match = re.search(r"/issues/(\d+)", out_str)
                        if match:
                            self._save_remote_issue_number(int(match.group(1)))
                else:
                    # Update issue body
                    # gh issue edit does not accept body on stdin directly in older versions without --body
                    run_headless(
                        ["gh", "issue", "edit", str(self.remote_issue_number), "--repo", self.remote_repo, "--body", body_text],
                        timeout=10,
                        check=False,
                    )

                if major_gate_reason and self.remote_issue_number:
                    comment_text = f"### Major Gate Event: {sanitize_text(major_gate_reason)}\n- Timestamp: `{snapshot.timestamp_utc}`\n- Sequence: `{snapshot.sequence_number}`\n- Runtime Slot: `{snapshot.runtime_slot}`"
                    run_headless(
                        ["gh", "issue", "comment", str(self.remote_issue_number), "--repo", self.remote_repo, "--body", comment_text],
                        timeout=10,
                        check=False,
                    )

                self.remote_outbox_status = "PUBLISHED"
                snapshot.remote_outbox_status = "PUBLISHED"
                snapshot.remote_issue_number = self.remote_issue_number
                snapshot.last_remote_publish_at = snapshot.timestamp_utc
                self.last_routine_remote_publish = now
                return True
            except Exception as exc:
                self.remote_outbox_status = f"DEGRADED_GH_CLI_ERROR:{exc.__class__.__name__}"
                snapshot.remote_outbox_status = self.remote_outbox_status
                return False

        # 2. Transport using GitHub REST API with sanitized token
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AOS-Controller-Relay/1.0",
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        }
        tls_ctx = _create_tls_context()

        try:
            # 1. Find or create the issue if not known
            if not self.remote_issue_number:
                search_url = f"https://api.github.com/repos/{self.remote_repo}/issues?state=open"
                req = urllib.request.Request(search_url, headers=headers, method="GET")
                try:
                    with urllib.request.urlopen(req, context=tls_ctx, timeout=15) as resp:
                        items = json.loads(resp.read().decode("utf-8"))
                        for it in items:
                            if it.get("title") == "AOS Controller Relay":
                                self._save_remote_issue_number(int(it["number"]))
                                break
                except Exception:
                    pass

            if not self.remote_issue_number:
                create_url = f"https://api.github.com/repos/{self.remote_repo}/issues"
                # Do NOT require labels for initial creation
                create_payload = {
                    "title": "AOS Controller Relay",
                    "body": body_text,
                }
                req = urllib.request.Request(
                    create_url,
                    data=json.dumps(create_payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, context=tls_ctx, timeout=15) as resp:
                    created_data = json.loads(resp.read().decode("utf-8"))
                    self._save_remote_issue_number(int(created_data["number"]))
            else:
                update_url = f"https://api.github.com/repos/{self.remote_repo}/issues/{self.remote_issue_number}"
                update_payload = {
                    "body": body_text,
                }
                req = urllib.request.Request(
                    update_url,
                    data=json.dumps(update_payload).encode("utf-8"),
                    headers=headers,
                    method="PATCH",
                )
                with urllib.request.urlopen(req, context=tls_ctx, timeout=15) as resp:
                    pass

                # If major gate milestone, also create comment
                if major_gate_reason:
                    comment_url = f"https://api.github.com/repos/{self.remote_repo}/issues/{self.remote_issue_number}/comments"
                    comment_payload = {
                        "body": f"### Major Gate Event: {sanitize_text(major_gate_reason)}\n- Timestamp: `{snapshot.timestamp_utc}`\n- Sequence: `{snapshot.sequence_number}`\n- Runtime Slot: `{snapshot.runtime_slot}`",
                    }
                    req_c = urllib.request.Request(
                        comment_url,
                        data=json.dumps(comment_payload).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(req_c, context=tls_ctx, timeout=15) as resp_c:
                            pass
                    except Exception:
                        pass

            self.remote_outbox_status = "PUBLISHED"
            snapshot.remote_outbox_status = "PUBLISHED"
            snapshot.remote_issue_number = self.remote_issue_number
            snapshot.last_remote_publish_at = snapshot.timestamp_utc
            self.last_routine_remote_publish = now
            return True
        except Exception as exc:
            self.remote_outbox_status = f"DEGRADED_HTTP_ERROR:{exc.__class__.__name__}"
            snapshot.remote_outbox_status = self.remote_outbox_status
            return False

    def emit_cycle(
        self,
        runtime_health_dict: Optional[Dict[str, Any]] = None,
        supervisor_pid: Optional[int] = None,
        major_gate_reason: Optional[str] = None,
        force_checkpoint: bool = False,
        force_diagnosis: bool = False,
    ) -> RelaySnapshot:
        """Single controller relay loop execution."""
        now = time.time()
        is_checkpoint = force_checkpoint or (now - self.last_checkpoint_epoch >= 1800) or bool(major_gate_reason)
        if is_checkpoint:
            self.last_checkpoint_epoch = now

        snapshot = self.collect_snapshot(
            runtime_health_dict,
            supervisor_pid,
            force_diagnosis=force_diagnosis or bool(major_gate_reason),
        )
        self.publish_remote(snapshot, major_gate_reason=major_gate_reason)
        self.publish_local(snapshot, is_checkpoint=is_checkpoint)
        return snapshot


class AsyncControllerRelay:
    """Non-blocking bridge between lifecycle loops and slow relay enrichment."""

    def __init__(self, publisher: ControllerRelayPublisher, *, cycle_budget_seconds: float = 45.0) -> None:
        self.publisher = publisher
        self.cycle_budget_seconds = float(cycle_budget_seconds)
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[RelaySnapshot] = None
        self._last_error: Optional[str] = None

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="aos-relay-enrichment", daemon=True)
        self._thread.start()

    def submit(self, *, maintenance: bool = False, **kwargs: Any) -> bool:
        """Queue one cycle without waiting; maintenance mode disables enrichment."""
        if maintenance or self._stop.is_set():
            return False
        self._ensure_started()
        try:
            self._queue.put_nowait(dict(kwargs))
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            started = time.monotonic()
            try:
                snapshot = self.publisher.emit_cycle(**item)
                elapsed = time.monotonic() - started
                with self._lock:
                    self._latest = snapshot
                    self._last_error = (
                        f"CYCLE_BUDGET_EXCEEDED:{elapsed:.3f}s"
                        if elapsed > self.cycle_budget_seconds else None
                    )
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queued": not self._queue.empty(),
                "latest_sequence_number": self._latest.sequence_number if self._latest else None,
                "last_error": self._last_error,
            }

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
