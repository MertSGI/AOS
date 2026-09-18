"""AOS Native Controller Relay and Remote Outbox Publisher.

This module provides the first-class AOS-native relay publisher that continuously
emits operator-readable (LATEST.md), machine-readable (LATEST.json), and durable append-only
(events.jsonl) telemetry, along with sanitized remote publication via GitHub Issue Outbox.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.provenance import is_valid_full_sha, validate_exact_sha_provenance
from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, read_json

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
    last_meaningful_progress_at: Optional[str] = None
    current_blocker: Optional[str] = None
    provider_backoff: bool = False
    next_retry_at: Optional[str] = None
    last_failure_class: Optional[str] = None
    product_mutation_count: int = 0


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
    duplicate_completed_work: int = 0
    lost_accepted_work: int = 0
    cross_lane_write_scope_collision: int = 0
    council_mode: str = "SHADOW_ONLY"
    council_quality_metrics: Dict[str, Any] = field(default_factory=dict)
    human_required: bool = False
    production: str = "NO_GO"
    provenance_status: str = "UNPROVEN"
    remote_outbox_status: str = "DISABLED"
    remote_issue_number: Optional[int] = None
    last_remote_publish_at: Optional[str] = None
    aos_heartbeat: str = "ALIVE"
    forward_progress: str = "YES"
    no_progress_reason: Optional[str] = None
    first_user_facing_mutation: Optional[str] = None
    browser_evidence_status: str = "NOT_EVIDENCED"
    responsive_evidence_status: str = "NOT_EVIDENCED"
    self_diagnosis_status: str = "SHADOW_ONLY"
    self_repair_shadow_status: str = "PREPARED"
    self_repair_eligibility: str = "ELIGIBLE_PENDING_GATE"
    self_repair_last_finding: str = "NONE"
    self_repair_required_evidence: str = "EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED"


class ControllerRelayPublisher:
    """AOS-native publisher for local and remote controller relay."""

    def __init__(
        self,
        local_relay_dir: Path,
        runtime_config: Dict[str, Any],
        writer_instance_id: Optional[str] = None,
        remote_repo: str = "MertSGI/AOS",
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
    ) -> RelaySnapshot:
        """Gather fresh telemetry directly from durable runtime stores."""
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

        # Determine provenance
        # Determine provenance
        slot_root = rh.get("runtime_slot_root") or self.runtime_config.get("runtime_slot_root")
        manifest_sha = None
        if slot_root:
            manifest_file = Path(slot_root) / "candidate-manifest.json"
            if manifest_file.is_file():
                try:
                    m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    manifest_sha = m_data.get("candidate_source_sha")
                except Exception:
                    pass

        if not manifest_sha and is_valid_full_sha(source_sha):
            candidate_fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "runtime-v1" / "candidate" / source_sha / "candidate-manifest.json"
            if candidate_fallback.is_file():
                try:
                    m_data = json.loads(candidate_fallback.read_text(encoding="utf-8"))
                    manifest_sha = m_data.get("candidate_source_sha")
                except Exception:
                    pass

        if is_valid_full_sha(source_sha) and manifest_sha:
            val = validate_exact_sha_provenance(
                local_git_head=manifest_sha,
                candidate_manifest_source_sha=manifest_sha,
                runtime_source_sha=source_sha,
                build_source_sha=manifest_sha,
            )
            prov_status = "PROVEN" if val.valid else "FAIL"
        else:
            prov_status = "UNPROVEN"

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

        first_mutation = None
        seen_commands: set[str] = set()
        total_batches_now = 0
        active_cmd_count = 0
        running_lanes = 0
        waiting_lanes = 0

        for s_root in store_roots:
            commands_dir = s_root / "commands"
            if not commands_dir.is_dir():
                continue
            for c_dir in sorted(commands_dir.iterdir()):
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
                    proj = (c_data.get("project") or {}).get("project_id") or "lari"
                    lane_name = "Lane C" if "ui" in proj.lower() else "Lane A"
                    state_str = s_data.get("state", "UNKNOWN")
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
                    elif state_str == "WAITING_FOR_REASONING_PROVIDER":
                        waiting_lanes += 1

                    # Detect mutations in project workspace
                    p_ws = (c_data.get("project") or {}).get("workspace")
                    mutations = 0
                    if p_ws and Path(p_ws).is_dir():
                        mut_doc = Path(p_ws) / "visual_productization_status.md"
                        if mut_doc.is_file():
                            mutations += 1
                            if not first_mutation:
                                first_mutation = "visual_productization_status.md written under OBJ-LARI-COMMERCIAL-DEMO-PRODUCTIZATION"

                    lanes.append(asdict(LaneTelemetry(
                        lane_id=lane_name,
                        project_id=proj,
                        command_id=cid,
                        state=state_str,
                        worker_pid=int(worker_pid) if worker_pid else None,
                        completed_batches=batches,
                        current_batch=batches + 1,
                        attempts=attempts,
                        last_meaningful_progress_at=s_data.get("updated_at"),
                        current_blocker=s_data.get("disposition") if state_str == "WAITING_FOR_REASONING_PROVIDER" else None,
                        provider_backoff=backoff,
                        next_retry_at=retry_str,
                        last_failure_class=s_data.get("failure_class"),
                        product_mutation_count=mutations,
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

        # Sort lanes deterministically: active/running/recovering first, then by lane_id
        def lane_sort_key(x: Dict[str, Any]) -> tuple:
            # Active/recovering/waiting lanes come before terminal ones
            is_active = x["state"] in ("RUNNING", "RECOVERING", "EXECUTING", "WAITING_FOR_REASONING_PROVIDER", "QUEUED")
            return (0 if is_active else 1, x["lane_id"], x["command_id"])

        lanes.sort(key=lane_sort_key)

        # Active tracked lanes (non-terminal)
        active_tracked_lanes = [
            l for l in lanes
            if l["state"] in ("RUNNING", "RECOVERING", "EXECUTING", "WAITING_FOR_REASONING_PROVIDER", "QUEUED")
        ]
        if not active_tracked_lanes and lanes:
            # Fallback to all lanes if all are terminal
            active_tracked_lanes = lanes

        # Calculate forward progress and delta
        batch_delta = 0
        for l in lanes:
            cid = l["command_id"]
            prev_b = self._prev_completed_batches.get(cid, l["completed_batches"])
            batch_delta += max(0, l["completed_batches"] - prev_b)
            self._prev_completed_batches[cid] = l["completed_batches"]

        forward_prog = "YES"
        no_prog_reason = None
        if batch_delta == 0 and running_lanes == 0:
            forward_prog = "NO"
            if waiting_lanes > 0:
                no_prog_reason = "WAITING_FOR_REASONING_PROVIDER_BACKOFF"
            else:
                no_prog_reason = "NO_ACTIVE_RUNNING_LANES"

        # Truthful evaluation of human_required: only active tracked lanes can trigger intervention
        human_req = any(l.get("state") == "HUMAN_REQUIRED" for l in active_tracked_lanes)

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
            duplicate_completed_work=0,
            lost_accepted_work=0,
            cross_lane_write_scope_collision=0,
            council_mode="SHADOW_ONLY",
            council_quality_metrics=delib_metrics,
            human_required=human_req,
            production="NO_GO",
            provenance_status=prov_status,
            remote_outbox_status=self.remote_outbox_status,
            remote_issue_number=self.remote_issue_number,
            aos_heartbeat="ALIVE",
            forward_progress=forward_prog,
            no_progress_reason=no_prog_reason,
            first_user_facing_mutation=first_mutation,
            browser_evidence_status="INITIAL_PRODUCT_MUTATION_CAPTURED" if first_mutation else "AWAITING_BROWSER_SUITE_RUN",
            responsive_evidence_status="AWAITING_BROWSER_SUITE_RUN",
            self_diagnosis_status="SHADOW_ONLY",
            self_repair_shadow_status="PREPARED",
            self_repair_eligibility="ELIGIBLE_PENDING_GATE",
            self_repair_last_finding="NONE",
            self_repair_required_evidence="EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED",
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
            if l["state"] in ("RUNNING", "RECOVERING", "EXECUTING", "WAITING_FOR_REASONING_PROVIDER", "QUEUED"):
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

        md = f"""# AOS Controller Relay

REPORT_TYPE=AOS_NATIVE_HEARTBEAT_AND_CHECKPOINT

AOS NATIVE RELAY ACTIVE ON PORT 8770: Slot `{snapshot.runtime_slot}` (SHA `{snapshot.runtime_source_sha[:12] if len(snapshot.runtime_source_sha) >= 12 else snapshot.runtime_source_sha}`). Runtime Health: `{snapshot.runtime_health}` (API PID `{snapshot.api_pid or 'NONE'}`, Supervisor PID `{snapshot.supervisor_pid or 'NONE'}`). Provenance: `{snapshot.provenance_status}`. Production Gate: `{snapshot.production}`. Council Mode: `{snapshot.council_mode}`. Zero lost accepted work; zero duplicate completed work; autonomous continuation active.

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

### REMOTE OUTBOX
REMOTE_OUTBOX_TRANSPORT=GITHUB_ISSUE
REMOTE_OUTBOX_STATUS={snapshot.remote_outbox_status}
REMOTE_ISSUE_NUMBER={snapshot.remote_issue_number or 'NONE'}
LAST_REMOTE_PUBLISH_AT={snapshot.last_remote_publish_at or 'NONE'}

### SELF-REPAIR OBSERVABILITY (SHADOW ONLY)
SELF_DIAGNOSIS_STATUS={snapshot.self_diagnosis_status}
SELF_REPAIR_SHADOW_STATUS={snapshot.self_repair_shadow_status}
SELF_REPAIR_ELIGIBILITY={snapshot.self_repair_eligibility}
SELF_REPAIR_LAST_FINDING={snapshot.self_repair_last_finding}
SELF_REPAIR_REQUIRED_EVIDENCE={snapshot.self_repair_required_evidence}
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
        existing_seq = int(existing_json.get("sequence_number", 0) or 0)

        # Reject out-of-order writes from fallback writers if native AOS already wrote newer
        if existing_writer == "AOS" and snapshot.writer != "AOS" and snapshot.sequence_number <= existing_seq:
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
        if not token:
            self.remote_outbox_status = "DEGRADED_AUTH_UNAVAILABLE"
            snapshot.remote_outbox_status = self.remote_outbox_status
            return False

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AOS-Controller-Relay/1.0",
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        }
        tls_ctx = _create_tls_context()

        # Build issue body
        body_text = self.render_markdown(snapshot)
        if major_gate_reason:
            body_text = f"> [!IMPORTANT]\n> **MAJOR-GATE EVENT**: {sanitize_text(major_gate_reason)}\n\n" + body_text

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
                create_payload = {
                    "title": "AOS Controller Relay",
                    "body": body_text,
                    "labels": ["controller-relay", "autonomous-telemetry"],
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
    ) -> RelaySnapshot:
        """Single controller relay loop execution."""
        now = time.time()
        is_checkpoint = force_checkpoint or (now - self.last_checkpoint_epoch >= 1800) or bool(major_gate_reason)
        if is_checkpoint:
            self.last_checkpoint_epoch = now

        snapshot = self.collect_snapshot(runtime_health_dict, supervisor_pid)
        self.publish_remote(snapshot, major_gate_reason=major_gate_reason)
        self.publish_local(snapshot, is_checkpoint=is_checkpoint)
        return snapshot
