"""AOS Direct local control panel.

Loopback-only HTTP surface for the AOS Local Autonomous Host. This is deliberately
independent of Antigravity's agent/model quota. It does not grant authority; submitted
jobs are revalidated by aos.local_host and then fresh-bound by Autonomous Host V1.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from aos.local_host import _atomic_json, load_config, validate_job
from aos.secure_store import delete_provider_secret, provider_presence, write_provider_secret
from aos.runtime_panel_bridge import runtime_configured, runtime_status, submit_goal_to_runtime, execute_command_on_runtime
from aos.provenance import get_authoritative_git_head, is_valid_full_sha, validate_exact_sha_provenance, ProvenanceError
from aos.self_diagnosis import SelfDiagnosisEngine

MAX_BODY_BYTES = 256 * 1024

_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AOS Direct</title>
<style>
:root { color-scheme: dark; font-family: Inter, Segoe UI, sans-serif; }
body { margin:0; background:#111418; color:#e8edf2; }
main { max-width:1100px; margin:32px auto; padding:0 20px; }
h1 { margin-bottom:4px; }
.sub { color:#9eabb7; margin-top:0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin:20px 0; }
.card { background:#191e24; border:1px solid #2b333c; border-radius:10px; padding:14px; }
.label { color:#9eabb7; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.value { font-size:18px; margin-top:6px; word-break:break-word; }
.ok { color:#74d99f; } .hold { color:#f0b66c; }
textarea { width:100%; min-height:260px; box-sizing:border-box; background:#0d1014; color:#e8edf2; border:1px solid #36414c; border-radius:8px; padding:12px; font:13px Consolas,monospace; }
button { background:#e8edf2; color:#111418; border:0; border-radius:7px; padding:10px 16px; font-weight:700; cursor:pointer; }
button.secondary { background:#27313b; color:#e8edf2; margin-left:8px; }
#message { margin-top:10px; white-space:pre-wrap; }
small { color:#9eabb7; }
a { color:#8ab4f8; }
.providers-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; margin-top:12px; }
.provider-box { border:1px solid #2b333c; border-radius:8px; padding:12px; background:#14191f; }
input[type=password], input[type=text], input[type=number] { width:100%; box-sizing:border-box; background:#0d1014; color:#e8edf2; border:1px solid #36414c; border-radius:7px; padding:9px; margin:8px 0; }
.goal-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.goal-grid .wide { grid-column:1 / -1; }
textarea.goal { min-height:110px; font-family:Inter,Segoe UI,sans-serif; }
.danger { background:#5b2b32; color:#fff; margin-left:6px; }
</style>
</head>
<body>
<main>
<h1>AOS Direct</h1>
<p class="sub">AG-independent local AOS control surface · loopback only · production NO_GO</p>
<div class="grid">
  <div class="card"><div class="label">Host</div><div id="host" class="value">Loading…</div></div>
  <div class="card"><div class="label">Active Slot</div><div id="active-slot" class="value">Loading…</div></div>
  <div class="card"><div class="label">Runtime SHA</div><div id="active-sha" class="value">Loading…</div></div>
  <div class="card"><div class="label">SHA Format</div><div id="sha-format-status" class="value">Loading…</div></div>
  <div class="card"><div class="label">Local Git Head</div><div id="local-git-head" class="value">Loading…</div></div>
  <div class="card"><div class="label">Candidate Manifest SHA</div><div id="manifest-sha" class="value">Loading…</div></div>
  <div class="card"><div class="label">Build Source SHA</div><div id="build-sha" class="value">Loading…</div></div>
  <div class="card"><div class="label">CI Head SHA</div><div id="ci-head-sha" class="value">Loading…</div></div>
  <div class="card"><div class="label">Provenance Status</div><div id="provenance-status" class="value">Loading…</div></div>
  <div class="card"><div class="label">Production</div><div id="production" class="value">NO_GO</div></div>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Autonomous Multi-Lane Telemetry</div>
  <div id="lanes-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.6;">Loading lanes…</div>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Council Deliberation Shadow Metrics (Real Production Sampling)</div>
  <div id="deliberation-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.6;">Loading deliberation metrics…</div>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Reasoning providers</div>
  <div id="providers" class="value"></div>
  <small>AOS Direct does not consume Antigravity model quota. A reasoning provider is still required for autonomous free-text planning.</small>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Provider settings · Windows Credential Manager</div>
  <p><small>Secrets are stored only in your Windows user credential vault. They are never returned by this page, written to Git, or placed in AOS job JSON.</small></p>
  <div class="providers-grid">
    <div class="provider-box">
      <strong>NVIDIA / Nemotron</strong> · <a href="https://ngc.nvidia.com/" target="_blank" rel="noreferrer">provider console</a>
      <input id="key-NVIDIA" type="password" autocomplete="off" placeholder="Paste NVIDIA API key">
      <button onclick="saveProvider('NVIDIA')">Save securely</button><button class="danger" onclick="clearProvider('NVIDIA')">Clear</button>
    </div>
    <div class="provider-box">
      <strong>Gemini</strong> · <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">API keys</a>
      <input id="key-GEMINI" type="password" autocomplete="off" placeholder="Paste Gemini API key">
      <button onclick="saveProvider('GEMINI')">Save securely</button><button class="danger" onclick="clearProvider('GEMINI')">Clear</button>
    </div>
    <div class="provider-box">
      <strong>Groq</strong> · <a href="https://console.groq.com/keys" target="_blank" rel="noreferrer">API keys</a>
      <input id="key-GROQ" type="password" autocomplete="off" placeholder="Paste Groq API key">
      <button onclick="saveProvider('GROQ')">Save securely</button><button class="danger" onclick="clearProvider('GROQ')">Clear</button>
    </div>
    <div class="provider-box">
      <strong>OpenAI (optional)</strong> · <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">API keys</a>
      <input id="key-OPENAI" type="password" autocomplete="off" placeholder="Paste OpenAI API key">
      <button onclick="saveProvider('OPENAI')">Save securely</button><button class="danger" onclick="clearProvider('OPENAI')">Clear</button>
    </div>
  </div>
  <div id="provider-message"></div>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Autonomous Goal Mode</div>
  <p><small>Normal mode requires no human-authored run plan. AOS fresh-reads canonical state, selects the next objective, generates its own DAG, executes, tests, fresh-reads and replans. Production remains NO_GO.</small></p>
  <div class="goal-grid">
    <div><small>Project descriptor</small><input id="goal-descriptor" type="text" placeholder="C:\\Projects\\AOS\\descriptors\\lari.autonomous-host.descriptor.json"></div>
    <div><small>Workspace</small><input id="goal-workspace" type="text" placeholder="Local project workspace"></div>
    <div class="wide"><small>Routing policy</small><input id="goal-policy" type="text" placeholder="C:\\Projects\\AOS\\descriptors\\nemotron.planner-policy.json"></div>
    <div class="wide"><small>Goal</small><textarea class="goal" id="goal-text" spellcheck="true">Continue this project to completion under standing authority.</textarea></div>
    <div><small>Constraints · one per line</small><textarea class="goal" id="goal-constraints" spellcheck="true"></textarea></div>
    <div><small>Additional red lines · one per line</small><textarea class="goal" id="goal-redlines" spellcheck="true"></textarea></div>
    <div><small>Max autonomous batches this invocation</small><input id="goal-batches" type="number" min="1" max="50" value="12"></div>
  </div>
  <div style="margin-top:10px">
    <button onclick="submitGoal()">Start / Continue Autonomous Project</button>
    <button class="secondary" onclick="refreshStatus()">Refresh</button>
  </div>
  <div id="goal-message"></div>
</div>

<div class="card" style="margin-top:12px">
  <div class="label">Operations Command Surface · Bounded Control</div>
  <p><small>Bounded operator actions invoke runtime IPC endpoints directly. No direct mutation of state files.</small></p>
  <div class="command-bar" style="margin-top:8px;">
    <button onclick="runOpCommand('continue')">Continue</button>
    <button class="secondary" onclick="runOpCommand('pause-safe')">Pause-Safe</button>
    <button class="secondary" onclick="runOpCommand('resume')">Resume</button>
    <button class="secondary" onclick="runOpCommand('heartbeat-now')">Heartbeat-Now</button>
    <button class="secondary" onclick="runOpCommand('checkpoint-now')">Checkpoint-Now</button>
    <button class="secondary" onclick="runOpCommand('publish-relay-now')">Publish-Relay-Now</button>
    <button class="secondary" onclick="runOpCommand('restart-worker')">Restart-Worker</button>
    <button class="secondary" onclick="refreshStatus()">Refresh-Status</button>
  </div>
  <div id="op-command-msg" style="margin-top:8px; font-family:Consolas,monospace; font-size:12px; color:#9eabb7;"></div>
</div>

<div class="grid" style="margin-top:12px;">
  <div class="card">
    <div class="label">Controller Relay & Remote Outbox</div>
    <div id="relay-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.5;">Loading relay…</div>
  </div>
  <div class="card">
    <div class="label">Product Mutation & Acceptance Evidence</div>
    <div id="product-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.5;">Loading product evidence…</div>
  </div>
</div>

<div class="grid" style="margin-top:12px;">
  <div class="card">
    <div class="label">Active Runtime Alerts</div>
    <div id="alerts-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.5;">Loading alerts…</div>
  </div>
  <div class="card">
    <div class="label">Autonomous Self-Repair Observability (Shadow Only)</div>
    <div id="self-repair-view" style="margin-top:8px; font-family:Consolas,monospace; font-size:13px; line-height:1.5;">Loading self-repair hooks…</div>
  </div>
</div>

<details class="card" style="margin-top:12px">
  <summary>Manual bounded run-plan override · debug/replay only</summary>
  <p><small>This is not required for normal autonomous mode.</small></p>
  <textarea id="job" spellcheck="false" placeholder='Paste a validated *.aosjob.json envelope here'></textarea>
  <div style="margin-top:10px"><button onclick="submitJob()">Submit manual override</button></div>
  <div id="message"></div>
</details>
</main>
<script>
const TOKEN = __AOS_TOKEN_JSON__;
async function refreshStatus() {
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const s = await r.json();
    document.getElementById('host').textContent = s.host_state || 'UNKNOWN';
    document.getElementById('host').className = 'value ' + ((s.host_state||'').includes('HOLD') ? 'hold' : 'ok');
    document.getElementById('active-slot').textContent = (s.active_slot || 'NONE').slice(0, 32);
    document.getElementById('active-sha').textContent = (s.active_sha || 'NONE').slice(0, 12);
    const shaFormat = s.sha_format_status || 'INVALID';
    const shaFormatEl = document.getElementById('sha-format-status');
    shaFormatEl.textContent = shaFormat;
    shaFormatEl.className = 'value ' + (shaFormat === 'VALID' ? 'ok' : 'danger');

    document.getElementById('local-git-head').textContent = (s.local_git_head || 'UNAVAILABLE').slice(0, 12);
    document.getElementById('manifest-sha').textContent = (s.candidate_manifest_sha || 'UNAVAILABLE').slice(0, 12);
    document.getElementById('build-sha').textContent = (s.build_source_sha || 'UNAVAILABLE').slice(0, 12);
    document.getElementById('ci-head-sha').textContent = (s.ci_head_sha || 'UNAVAILABLE').slice(0, 12);

    const provStatus = s.provenance_status || 'UNPROVEN';
    const provEl = document.getElementById('provenance-status');
    provEl.textContent = provStatus;
    provEl.className = 'value ' + (provStatus === 'PROVEN' ? 'ok' : (provStatus === 'UNPROVEN' ? 'hold' : 'danger'));
    document.getElementById('production').textContent = s.production || 'NO_GO';
    
    // Render detailed lanes telemetry (state, batch count, attempts, backoff, timestamps)
    const lanes = s.lanes || {};
    const laneKeys = Object.keys(lanes);
    if (laneKeys.length === 0) {
      document.getElementById('lanes-view').innerHTML = '<em>No active autonomous lanes currently running.</em>';
    } else {
      let html = '<table style="width:100%; border-collapse:collapse; text-align:left;">';
      html += '<tr style="color:#9eabb7; border-bottom:1px solid #2b333c;"><th style="padding:6px;">Lane / Project</th><th>Command ID</th><th>State / Disposition</th><th>Batches</th><th>Attempts</th><th>Updated</th></tr>';
      for (const k of laneKeys) {
        const item = lanes[k];
        const stateColor = (item.state === 'RUNNING' || item.state === 'EXECUTING') ? '#74d99f' : ((item.state||'').includes('WAITING') ? '#f0b66c' : '#e8edf2');
        html += `<tr style="border-bottom:1px solid #1f252d;">
          <td style="padding:6px; font-weight:bold;">${k.toUpperCase()}</td>
          <td><small>${(item.command_id||'').slice(0, 24)}</small></td>
          <td style="color:${stateColor}">${item.state || 'UNKNOWN'}</td>
          <td>${item.completed_batches ?? 0}</td>
          <td>${item.attempts ?? 0}</td>
          <td><small>${(item.updated_at||'').slice(11, 19)}Z</small></td>
        </tr>`;
      }
      html += '</table>';
      document.getElementById('lanes-view').innerHTML = html;
    }

    // Render Council Deliberation Shadow Metrics
    const delib = s.deliberation || {};
    const totalSamples = delib.total_samples || 0;
    if (totalSamples === 0) {
      document.getElementById('deliberation-view').innerHTML = '<em>No deliberation shadow samples recorded yet.</em>';
    } else {
      const reasons = delib.trigger_reasons || {};
      const reasonEntries = Object.entries(reasons).map(([k, v]) => `<span style="background:#27313b; border-radius:4px; padding:2px 6px; margin-right:6px;">${k}: <strong>${v}</strong></span>`).join(' ');
      let dHtml = `<div style="display:flex; gap:18px; flex-wrap:wrap; margin-bottom:8px;">
        <div>Total Real Shadow Samples: <strong class="ok" style="font-size:16px;">${totalSamples}</strong></div>
        <div>Quorum Obtained: <strong>${delib.quorum_count || 0}</strong></div>
        <div>Council Agreement: <strong>${delib.agreement_count || 0}</strong></div>
      </div>
      <div style="margin-top:6px; color:#9eabb7;">Trigger Reasons: ${reasonEntries || 'None'}</div>`;
      document.getElementById('deliberation-view').innerHTML = dHtml;
    }

    // Render Controller Relay & Remote Outbox
    const relay = s.relay || {};
    if (!relay.writer) {
      document.getElementById('relay-view').innerHTML = '<em>No native controller relay data loaded yet.</em>';
    } else {
      const outboxColor = (relay.remote_outbox_status === 'PUBLISHED') ? '#74d99f' : ((relay.remote_outbox_status||'').includes('DEGRADED') ? '#f0b66c' : '#e8edf2');
      let rHtml = `<div style="display:flex; flex-direction:column; gap:4px;">
        <div>Writer: <strong>${relay.writer || 'AOS'}</strong> (Seq: <strong>${relay.sequence_number ?? 0}</strong>)</div>
        <div>Heartbeat: <strong class="${relay.aos_heartbeat === 'ALIVE' ? 'ok' : 'danger'}">${relay.aos_heartbeat || 'ALIVE'}</strong> · Progress: <strong>${relay.forward_progress || 'YES'}</strong></div>
        <div>Remote Outbox: <strong style="color:${outboxColor}">${relay.remote_outbox_status || 'DISABLED'}</strong> (Issue: <strong>#${relay.remote_issue_number || 'NONE'}</strong>)</div>
        <div>Last Remote Publish: <small>${(relay.last_remote_publish_at||'NONE').slice(11, 19)}Z</small></div>
      </div>`;
      document.getElementById('relay-view').innerHTML = rHtml;
    }

    // Render Product Evidence
    const pe = s.product_evidence || {};
    let pHtml = `<div style="display:flex; flex-direction:column; gap:4px;">
      <div>Workspace Productization Artifact: <strong>${pe.first_workspace_productization_artifact || 'NONE'}</strong></div>
      <div>First User-Facing UI Mutation: <strong>${pe.first_user_facing_ui_mutation || 'NONE'}</strong></div>
      <div>Browser Evidence: <strong>${pe.browser_evidence_status || 'AWAITING_BROWSER_SUITE_RUN'}</strong></div>
      <div>Responsive Evidence: <strong>${pe.responsive_evidence_status || 'AWAITING_BROWSER_SUITE_RUN'}</strong></div>
    </div>`;
    document.getElementById('product-view').innerHTML = pHtml;

    // Render Alerts
    const alerts = s.alerts || [];
    if (alerts.length === 0) {
      document.getElementById('alerts-view').innerHTML = '<span class="ok">✓ All health invariants normal. Zero alerts.</span>';
    } else {
      document.getElementById('alerts-view').innerHTML = alerts.map(a => `<div style="color:#f0b66c; margin-bottom:4px;">⚠ ${a}</div>`).join('');
    }

    // Render Self-Repair Observability
    const sr = s.self_repair || {};
    let srHtml = `<div style="display:flex; flex-direction:column; gap:4px;">
      <div>Diagnosis Mode: <strong class="ok">SHADOW_ONLY</strong> · Status: <strong>${sr.self_diagnosis_status || 'HEALTHY_NO_ACTION'}</strong></div>
      <div>Active Findings: <strong>${sr.active_finding_count ?? 0}</strong> (Blocking: <strong style="color:${(sr.blocking_finding_count || 0) > 0 ? '#e06c75' : '#74d99f'}">${sr.blocking_finding_count ?? 0}</strong>)</div>
      <div>Last Finding: <strong>${sr.self_repair_last_finding || 'NONE'}</strong> (Class: <code>${sr.last_failure_class || 'NONE'}</code>)</div>
      <div>Severity: <strong>${sr.last_finding_severity || 'NONE'}</strong> · Autonomy Impact: <strong>${sr.last_autonomy_impact || 'NONE'}</strong></div>
      <div>Proposal Status: <strong>${sr.shadow_repair_proposal_status || 'NONE'}</strong> · Live Active: <strong>NO</strong></div>
      <div>Eligibility: <strong>${sr.self_repair_eligibility || 'PENDING_GATE'}</strong></div>
    </div>`;
    const findingsList = sr.findings || [];
    if (findingsList.length > 0) {
      srHtml += `<div style="margin-top:8px; border-top:1px solid #2b333c; padding-top:6px;">
        <div style="font-weight:bold; margin-bottom:4px; color:#9eabb7;">Recent Shadow Findings (Read-Only Inspection):</div>`;
      for (const f of findingsList.slice(0, 5)) {
        const sevColor = (f.severity === 'CRITICAL' || f.severity === 'HIGH') ? '#e06c75' : ((f.severity === 'MEDIUM') ? '#f0b66c' : '#74d99f');
        srHtml += `<details style="margin-bottom:6px; background:#14191f; border-radius:6px; padding:6px;">
          <summary style="cursor:pointer; font-size:12px;">
            <strong style="color:${sevColor}">[${f.severity}]</strong> <code>${f.failure_class}</code>: ${f.symptom.slice(0, 60)}…
          </summary>
          <div style="margin-top:6px; font-size:11px; font-family:Consolas,monospace; line-height:1.4;">
            <div>Finding ID: <strong>${f.finding_id}</strong></div>
            <div>Component: ${f.component} · Recurrence: ${f.recurrence_count}</div>
            <div>Impact: ${f.autonomy_impact} · Authority: ${f.repair_authority}</div>
            <div>Root Cause: ${f.suspected_root_cause || 'NONE'}</div>
            ${f.proposed_repair ? `<div style="margin-top:4px; color:#74d99f;">Proposed Repair (Shadow Only): ${f.proposed_repair.minimal_change || 'NONE'}</div>` : ''}
          </div>
        </details>`;
      }
      srHtml += `</div>`;
    }
    document.getElementById('self-repair-view').innerHTML = srHtml;

    const p = s.providers || {};
    document.getElementById('providers').textContent =
      ['NVIDIA','GEMINI','GROQ','OPENAI','OLLAMA'].map(k => `${k}: ${p[k] ? 'ready' : 'not ready'}`).join(' · ');
    const d = s.default_project || {};
    if (!document.getElementById('goal-descriptor').value && d.descriptor_path) document.getElementById('goal-descriptor').value = d.descriptor_path;
    if (!document.getElementById('goal-workspace').value && d.workspace) document.getElementById('goal-workspace').value = d.workspace;
    if (!document.getElementById('goal-policy').value && d.routing_policy_path) document.getElementById('goal-policy').value = d.routing_policy_path;
  } catch (e) {
    document.getElementById('host').textContent = 'PANEL_ERROR';
    document.getElementById('host').className = 'value hold';
  }
}

async function runOpCommand(cmd) {
  const out = document.getElementById('op-command-msg');
  out.textContent = 'Executing command: ' + cmd + '…';
  let payload = {};
  if (cmd === 'restart-worker') {
    const cid = prompt('Enter command_id to restart worker:');
    if (!cid) { out.textContent = 'restart-worker cancelled.'; return; }
    payload.command_id = cid.trim();
  }
  try {
    const r = await fetch('/api/commands/' + encodeURIComponent(cmd), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-AOS-Panel-Token': TOKEN},
      body: JSON.stringify(payload)
    });
    const res = await r.json();
    out.textContent = JSON.stringify(res);
    await refreshStatus();
  } catch (e) {
    out.textContent = 'Command ' + cmd + ' failed: ' + e;
  }
}

async function saveProvider(provider) {
  const input = document.getElementById('key-' + provider);
  const out = document.getElementById('provider-message');
  const secret = input.value.trim();
  if (!secret) { out.textContent = 'Enter a key for ' + provider; return; }
  try {
    const r = await fetch('/api/providers', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify({provider, secret, action:'save'})
    });
    const data = await r.json();
    input.value = '';
    out.textContent = data.ready ? provider + ' saved securely and is ready.' : JSON.stringify(data);
    await refreshStatus();
  } catch (e) { out.textContent = 'Provider save failed: ' + e; }
}
async function clearProvider(provider) {
  const out = document.getElementById('provider-message');
  try {
    const r = await fetch('/api/providers', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify({provider, action:'delete'})
    });
    const data = await r.json();
    out.textContent = provider + (data.deleted ? ' removed.' : ' had no stored credential.');
    await refreshStatus();
  } catch (e) { out.textContent = 'Provider clear failed: ' + e; }
}

async function submitGoal() {
  const out = document.getElementById('goal-message');
  const lines = id => document.getElementById(id).value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  const payload = {
    descriptor_path: document.getElementById('goal-descriptor').value.trim(),
    workspace: document.getElementById('goal-workspace').value.trim(),
    routing_policy_path: document.getElementById('goal-policy').value.trim(),
    goal: document.getElementById('goal-text').value.trim(),
    constraints: lines('goal-constraints'),
    red_lines: lines('goal-redlines'),
    max_batches: Number(document.getElementById('goal-batches').value || 12)
  };
  if (!payload.goal) {
    out.textContent = 'Goal is required.'; return;
  }
  try {
    const r = await fetch('/api/goals', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify(payload)
    });
    const data = await r.json();
    out.textContent = JSON.stringify(data, null, 2);
    await refreshStatus();
  } catch (e) { out.textContent = 'Goal submission failed: ' + e; }
}

async function submitJob() {
  const out = document.getElementById('message');
  let payload;
  try { payload = JSON.parse(document.getElementById('job').value); }
  catch (e) { out.textContent = 'Invalid JSON: ' + e; return; }
  try {
    const r = await fetch('/api/jobs', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify(payload)
    });
    const data = await r.json();
    out.textContent = JSON.stringify(data, null, 2);
    await refreshStatus();
  } catch (e) { out.textContent = 'Submit failed: ' + e; }
}
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _provider_presence() -> Dict[str, bool]:
    result = provider_presence()
    result["OLLAMA"] = False
    return result


def configure_provider(payload: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(payload.get("provider", "")).strip().upper()
    action = str(payload.get("action", "save")).strip().lower()
    if action == "save":
        secret = payload.get("secret")
        if not isinstance(secret, str):
            raise ValueError("Provider secret is required")
        write_provider_secret(provider, secret)
        return {
            "schema_version": "1.0.0",
            "provider": provider,
            "ready": True,
            "stored": "WINDOWS_CREDENTIAL_MANAGER",
            "secret_returned": False,
        }
    if action == "delete":
        deleted = delete_provider_secret(provider)
        return {
            "schema_version": "1.0.0",
            "provider": provider,
            "deleted": bool(deleted),
            "ready": False,
            "secret_returned": False,
        }
    raise ValueError("Unsupported provider action")


def build_status(config: Dict[str, Any]) -> Dict[str, Any]:
    if runtime_configured(config):
        bridge = runtime_status(config)
        providers = _provider_presence()
        runtime_v1 = bridge.get("runtime_v1", {})
        
        # Extract rich live telemetry for lanes, slot, commands, backoff, and work units
        lanes_detail = {}
        active_slot = str(runtime_v1.get("runtime_slot_id") or "UNKNOWN")
        active_sha = str(runtime_v1.get("runtime_source_sha") or "UNKNOWN")
        active_cmds = list(runtime_v1.get("active_commands") or [])
        waiting_cmds = list(runtime_v1.get("waiting_commands") or [])
        latest_cmd = dict(runtime_v1.get("latest_command") or {})

        # Scan active and waiting commands from runtime store across known store roots
        store_roots = []
        runtime_root_str = config.get("runtime_root")
        if runtime_root_str:
            base_p = Path(runtime_root_str).expanduser().resolve()
            if (base_p / "state" / "commands").is_dir():
                store_roots.append(base_p / "state")
            elif (base_p / "commands").is_dir():
                store_roots.append(base_p)
            else:
                store_roots.append(base_p)
        local_app_state = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "runtime-v1" / "state"
        if (local_app_state / "commands").is_dir() and local_app_state not in store_roots:
            store_roots.append(local_app_state)

        deliberation_metrics = {
            "total_samples": 0,
            "trigger_reasons": {},
            "quorum_count": 0,
            "agreement_count": 0,
        }

        try:
            from aos.runtime_store import RuntimeStore
            cids_to_scan = list(dict.fromkeys((active_cmds + waiting_cmds)[-10:]))
            for root in store_roots:
                if not (root / "commands").is_dir():
                    continue
                store = RuntimeStore(root)
                for cid in cids_to_scan:
                    cmd_data = store.read_command(cid)
                    cmd_state = store.read_state(cid)
                    if cmd_state and cmd_state.get("state"):
                        proj = (cmd_data.get("project") or {}).get("project_id") or "unknown"
                        lanes_detail[proj] = {
                            "command_id": cid,
                            "state": cmd_state.get("state"),
                            "disposition": cmd_state.get("disposition"),
                            "completed_batches": int(cmd_state.get("completed_batch_count", 0) or 0),
                            "attempts": int(cmd_state.get("attempts", 0) or 0),
                            "retry_after_epoch": cmd_state.get("retry_after_epoch"),
                            "updated_at": cmd_state.get("updated_at"),
                            "canonical_source_sha": cmd_state.get("canonical_source_sha"),
                        }
                # Aggregate deliberation shadow metrics from store commands
                for ledger in (root / "commands").glob("*/project-runtime/deliberation/deliberation-shadow-ledger.jsonl"):
                    try:
                        lines = [json.loads(line) for line in ledger.read_text("utf-8").strip().splitlines() if line.strip()]
                        deliberation_metrics["total_samples"] += len(lines)
                        for entry in lines:
                            reason = entry.get("council_trigger_reason", "UNKNOWN")
                            deliberation_metrics["trigger_reasons"][reason] = deliberation_metrics["trigger_reasons"].get(reason, 0) + 1
                            if entry.get("quorum_obtained") is True:
                                deliberation_metrics["quorum_count"] += 1
                            if entry.get("council_agreement") is True:
                                deliberation_metrics["agreement_count"] += 1
                    except Exception:
                        pass
        except Exception:
            pass

        sha_format_valid = is_valid_full_sha(active_sha)
        sha_format_status = "VALID" if sha_format_valid else "INVALID"

        # Check candidate manifest and build provenance if slot_root is known
        candidate_manifest_sha = None
        build_source_sha = None
        slot_root_str = runtime_v1.get("runtime_slot_root")
        if slot_root_str:
            try:
                manifest_path = Path(slot_root_str) / "candidate-manifest.json"
                if manifest_path.is_file():
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                        candidate_manifest_sha = m_data.get("candidate_source_sha")
                        build_source_sha = m_data.get("build_source_sha")
                build_rec = Path(slot_root_str) / "build-record.json"
                if not build_source_sha and build_rec.is_file():
                    with open(build_rec, "r", encoding="utf-8") as f:
                        b_data = json.load(f)
                        build_source_sha = b_data.get("build_source_sha") or b_data.get("source_sha")
            except Exception:
                pass

        # Discover authoritative git checkout explicitly (never alias manifest_sha or candidate_manifest_sha)
        local_git_head = None
        auth_repo_candidates = []
        if "authoritative_repo_path" in config:
            if config["authoritative_repo_path"]:
                auth_repo_candidates.append(Path(config["authoritative_repo_path"]))
        else:
            for auth_root in config.get("authorized_roots", []):
                auth_repo_candidates.append(Path(auth_root))
            auth_repo_candidates.extend([
                Path("C:/Projects/AOS-lane-b"),
                Path("C:/Projects/AOS"),
            ])
        for cand in auth_repo_candidates:
            try:
                resolved_cand = cand.expanduser().resolve()
                if (resolved_cand / ".git").exists():
                    local_git_head = get_authoritative_git_head(resolved_cand)
                    if local_git_head:
                        break
            except Exception:
                continue

        # PROVEN requires actual successful validation across available chain, not merely format
        if sha_format_valid and candidate_manifest_sha and local_git_head and build_source_sha:
            validation = validate_exact_sha_provenance(
                local_git_head=local_git_head,
                candidate_manifest_source_sha=candidate_manifest_sha,
                runtime_source_sha=active_sha,
                build_source_sha=build_source_sha,
            )
            provenance_status = "PROVEN" if validation.valid else "FAIL"
        else:
            provenance_status = "UNPROVEN"

        # Load native relay snapshot if available
        relay_info = {}
        relay_file = Path("C:/Projects/AOS/.aos-runtime/controller-relay/LATEST.json")
        if relay_file.is_file():
            try:
                relay_info = json.loads(relay_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Self-diagnosis summary from durable findings
        diag_root = Path("C:/Projects/AOS/.aos-runtime/controller-relay/self-diagnosis")
        diag_engine = SelfDiagnosisEngine(diag_root, config)
        diag_summary = diag_engine.summarize_status()

        # Product mutations and evidence
        first_workspace_artifact = relay_info.get("first_workspace_productization_artifact") or relay_info.get("first_product_mutation")
        first_ui_mutation = relay_info.get("first_user_facing_ui_mutation")
        browser_evidence = relay_info.get("browser_evidence_status") or "AWAITING_BROWSER_SUITE_RUN"
        responsive_evidence = relay_info.get("responsive_evidence_status") or "AWAITING_BROWSER_SUITE_RUN"

        # Alerts assessment
        alerts = []
        if any(l.get("state") == "HUMAN_REQUIRED" for l in lanes_detail.values()):
            alerts.append("HUMAN_REQUIRED: One or more lanes require intervention")
        if any(l.get("state") == "FAILED" for l in lanes_detail.values()):
            alerts.append("LANE_FAILED: An execution lane entered FAILED state")
        if provenance_status == "FAIL":
            alerts.append("PROVENANCE_FAILURE: Exact SHA provenance check failed")
        if any(l.get("provider_backoff") for l in lanes_detail.values()):
            alerts.append("PROVIDER_DEGRADATION: Lane currently in provider backoff")
        if diag_summary.get("blocking_finding_count", 0) > 0:
            alerts.append(f"SELF_DIAGNOSIS_BLOCKING: {diag_summary['blocking_finding_count']} blocking finding(s) detected")

        return {
            "schema_version": "1.0.0",
            "host_state": bridge.get("host_state", "UNKNOWN"),
            "pending_jobs": int(bridge.get("pending_jobs", 0)),
            "last_job": None,
            "production": "NO_GO",
            "ag_backend_enabled": False,
            "providers": providers,
            "default_project": config.get("default_project", {}),
            "runtime_v1": runtime_v1,
            "active_slot": active_slot,
            "active_sha": active_sha,
            "sha_format_status": sha_format_status,
            "provenance_status": provenance_status,
            "candidate_manifest_sha": candidate_manifest_sha,
            "build_source_sha": build_source_sha,
            "local_git_head": local_git_head,
            "ci_head_sha": None,  # Not fabricated when unavailable
            "provenance_valid": (provenance_status == "PROVEN"),
            "active_commands": active_cmds,
            "waiting_commands": waiting_cmds,
            "latest_command": latest_cmd,
            "lanes": lanes_detail,
            "deliberation": deliberation_metrics,
            "relay": relay_info,
            "product_evidence": {
                "first_mutation": first_ui_mutation or first_workspace_artifact,
                "first_workspace_productization_artifact": first_workspace_artifact,
                "first_user_facing_ui_mutation": first_ui_mutation,
                "browser_evidence_status": browser_evidence,
                "responsive_evidence_status": responsive_evidence,
            },
            "alerts": alerts,
            "self_repair": {
                "self_diagnosis_status": diag_summary.get("self_diagnosis_status", "SHADOW_ONLY"),
                "self_repair_shadow_status": "PREPARED",
                "self_repair_eligibility": diag_summary.get("self_repair_eligibility", "ELIGIBLE_PENDING_GATE"),
                "self_repair_last_finding": diag_summary.get("last_finding_id", "NONE"),
                "self_repair_required_evidence": "EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED",
                "self_repair_live_active": False,
                "active_finding_count": diag_summary.get("active_finding_count", 0),
                "blocking_finding_count": diag_summary.get("blocking_finding_count", 0),
                "last_failure_class": diag_summary.get("last_failure_class", "NONE"),
                "last_finding_severity": diag_summary.get("last_finding_severity", "NONE"),
                "last_autonomy_impact": diag_summary.get("last_autonomy_impact", "NONE"),
                "shadow_repair_proposal_status": diag_summary.get("shadow_repair_proposal_status", "NONE"),
                "findings": diag_summary.get("findings", []),
            },
        }

    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    host_status = _read_json(runtime_root / "host-status.json", {})
    pending = len(list((runtime_root / "inbox").glob("*.aosjob.json"))) if (runtime_root / "inbox").is_dir() else 0
    providers = _provider_presence()
    ollama_probe = host_status.get("ollama_probe")
    if isinstance(ollama_probe, dict):
        providers["OLLAMA"] = bool(ollama_probe.get("available"))
    last_job = host_status.get("last_job") if isinstance(host_status.get("last_job"), dict) else None
    return {
        "schema_version": "1.0.0",
        "host_state": host_status.get("state", "UNKNOWN"),
        "pending_jobs": pending,
        "last_job": last_job,
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "providers": providers,
        "default_project": config.get("default_project", {}),
        "runtime_v1": {"runtime_state": "NOT_CONFIGURED"},
        "active_slot": "NONE",
        "active_sha": "NONE",
        "sha_format_status": "INVALID",
        "provenance_status": "UNPROVEN",
        "candidate_manifest_sha": None,
        "build_source_sha": None,
        "local_git_head": None,
        "ci_head_sha": None,
        "provenance_valid": False,
        "active_commands": [],
        "waiting_commands": [],
        "latest_command": {},
        "lanes": {},
        "deliberation": {
            "total_samples": 0,
            "trigger_reasons": {},
            "quorum_count": 0,
            "agreement_count": 0,
        },
        "relay": {},
        "product_evidence": {
            "first_mutation": None,
            "first_workspace_productization_artifact": None,
            "first_user_facing_ui_mutation": None,
            "browser_evidence_status": "AWAITING_BROWSER_SUITE_RUN",
            "responsive_evidence_status": "AWAITING_BROWSER_SUITE_RUN",
        },
        "alerts": [],
        "self_repair": {
            "self_diagnosis_status": "SHADOW_ONLY",
            "self_repair_shadow_status": "PREPARED",
            "self_repair_eligibility": "ELIGIBLE_PENDING_GATE",
            "self_repair_last_finding": "NONE",
            "self_repair_required_evidence": "EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED",
            "self_repair_live_active": False,
        },
    }

def submit_job(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = validate_job(payload, config)
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    inbox = runtime_root / "inbox"
    processed = runtime_root / "processed"
    failed = runtime_root / "failed"
    for directory in (inbox, processed, failed):
        directory.mkdir(parents=True, exist_ok=True)
    name = f"{normalized['job_id']}.aosjob.json"
    for directory in (inbox, processed, failed):
        if (directory / name).exists():
            raise ValueError(f"Job id already exists in local host state: {normalized['job_id']}")
    target = inbox / name
    _atomic_json(target, payload)
    return {
        "schema_version": "1.0.0",
        "accepted": True,
        "job_id": normalized["job_id"],
        "state": "QUEUED",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def submit_goal(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    if runtime_configured(config):
        return submit_goal_to_runtime(payload, config)

    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("Goal is required")
    if len(goal.strip()) > 8000:
        raise ValueError("Goal is too long")
    job = {
        "schema_version": "1.0.0",
        "job_id": "goal-" + secrets.token_hex(12),
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "descriptor_path": payload.get("descriptor_path"),
        "workspace": payload.get("workspace"),
        "routing_policy_path": payload.get("routing_policy_path"),
        "goal": goal.strip(),
        "constraints": payload.get("constraints", []),
        "red_lines": payload.get("red_lines", []),
        "max_batches": int(payload.get("max_batches", 12)),
        "max_iterations": int(payload.get("max_iterations", 30)),
    }
    if not (1 <= job["max_batches"] <= 50):
        raise ValueError("max_batches must be between 1 and 50")
    result = submit_job(job, config)
    result["mode"] = "AUTONOMOUS_GOAL_LEGACY_COMPATIBILITY"
    result["run_plan_required"] = False
    return result

class _Handler(BaseHTTPRequestHandler):
    server_version = "AOSDirect/1.0"

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, value: str) -> None:
        raw = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    @property
    def config(self) -> Dict[str, Any]:
        return self.server.aos_config  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.aos_token  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = _HTML.replace("__AOS_TOKEN_JSON__", json.dumps(self.token))
            self._html(html)
            return
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, build_status(self.config))
            return
        if parsed.path.startswith("/api/findings/"):
            finding_id = parsed.path[len("/api/findings/"):].strip()
            diag_root = Path("C:/Projects/AOS/.aos-runtime/controller-relay/self-diagnosis")
            diag_engine = SelfDiagnosisEngine(diag_root, self.config)
            finding = diag_engine.get_finding(finding_id)
            if finding:
                self._json(HTTPStatus.OK, finding)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "FINDING_NOT_FOUND", "finding_id": finding_id})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_OPTIONS(self) -> None:
        # No CORS support by design.
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "CORS_DISABLED"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not (parsed.path in ("/api/jobs", "/api/providers", "/api/goals") or parsed.path.startswith("/api/commands/")):
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        if self.headers.get("X-AOS-Panel-Token") != self.token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_PANEL_TOKEN"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "BODY_SIZE_INVALID"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            if parsed.path.startswith("/api/commands/"):
                cmd_name = parsed.path[len("/api/commands/"):]
                result = execute_command_on_runtime(cmd_name, payload, self.config)
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/api/providers":
                result = configure_provider(payload)
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/api/goals":
                result = submit_goal(payload, self.config)
                self._json(HTTPStatus.ACCEPTED, result)
                return
            result = submit_job(payload, self.config)
            self._json(HTTPStatus.ACCEPTED, result)
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": exc.__class__.__name__, "message": str(exc)[:1000]})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid leaking local request data into console logs.
        return


def ensure_panel_token(panel_config_path: Path) -> Dict[str, Any]:
    data = _read_json(panel_config_path, {})
    token = data.get("panel_token")
    if not isinstance(token, str) or len(token) < 32:
        data["panel_token"] = secrets.token_urlsafe(32)
    data.setdefault("schema_version", "1.0.0")
    data.setdefault("bind_host", "127.0.0.1")
    data.setdefault("port", 8765)
    return data


def serve(local_host_config: Path, panel_config_path: Path) -> int:
    host_config = load_config(local_host_config)
    panel = ensure_panel_token(panel_config_path)
    if panel.get("bind_host") not in ("127.0.0.1", "localhost"):
        raise ValueError("AOS Direct must bind to loopback only")
    port = int(panel.get("port", 8765))
    if not (1024 <= port <= 65535):
        raise ValueError("Invalid panel port")
    _atomic_json(panel_config_path, panel)

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.aos_config = host_config  # type: ignore[attr-defined]
    server.aos_token = panel["panel_token"]  # type: ignore[attr-defined]
    print(f"AOS_DIRECT_READY=http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Direct loopback control panel")
    parser.add_argument("--host-config", required=True)
    parser.add_argument("--panel-config", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return serve(
            Path(args.host_config).expanduser().resolve(),
            Path(args.panel_config).expanduser().resolve(),
        )
    except Exception as exc:
        print(f"AOS_DIRECT_HOLD: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
