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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from aos.local_host import _atomic_json, load_config, validate_job
from aos.runtime_contract import cumulative_completed_batch_count
from aos.secure_store import (
    credential_is_configured,
    delete_provider_secret,
    provider_presence,
    write_provider_secret,
)
from aos.runtime_panel_bridge import (
    configured_project_profiles,
    execute_command_on_runtime,
    runtime_configured,
    runtime_status,
    submit_goal_to_runtime,
)
from aos.provenance import (
    get_authoritative_git_head,
    is_valid_full_sha,
    validate_exact_sha_provenance,
    validate_materialized_runtime_provenance,
    ProvenanceError,
)
from aos.self_diagnosis import SelfDiagnosisEngine

MAX_BODY_BYTES = 256 * 1024

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AOS Direct · Autonomous Operations Cockpit</title>
<style>
:root {
  color-scheme: dark;
  --bg-page: #080a0f;
  --bg-rail: #0c0f16;
  --bg-topbar: #0f131c;
  --bg-card: #121722;
  --bg-card-subtle: #161c28;
  --bg-card-hover: #1b2333;
  --bg-input: #0a0d13;
  --border-dim: #1e2637;
  --border-card: #263248;
  --border-highlight: #334360;
  --border-focus: #3b82f6;
  --text-main: #f1f5f9;
  --text-sub: #94a3b8;
  --text-muted: #64748b;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: Consolas, "JetBrains Mono", ui-monospace, Menlo, Monaco, "Liberation Mono", "Courier New", monospace;
  --ok: #10b981;
  --ok-dim: rgba(16, 185, 129, 0.12);
  --ok-border: rgba(16, 185, 129, 0.35);
  --hold: #f59e0b;
  --hold-dim: rgba(245, 158, 11, 0.12);
  --hold-border: rgba(245, 158, 11, 0.35);
  --danger: #ef4444;
  --danger-dim: rgba(239, 68, 68, 0.14);
  --danger-border: rgba(239, 68, 68, 0.38);
  --cyan: #06b6d4;
  --cyan-dim: rgba(6, 182, 212, 0.12);
  --blue: #3b82f6;
  --purple: #8b5cf6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0;
  background: var(--bg-page);
  color: var(--text-main);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}

.ok { color: var(--ok); }
.hold { color: var(--hold); }
.danger { color: var(--danger); }
.neutral { color: var(--text-sub); }
.cyan { color: var(--cyan); }

/* APP LAYOUT SHELL */
.app-shell {
  display: flex;
  min-height: 100vh;
  width: 100%;
}

/* LEFT NAVIGATION RAIL */
.nav-rail {
  width: 240px;
  background: var(--bg-rail);
  border-right: 1px solid var(--border-dim);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  z-index: 90;
}
.rail-brand {
  padding: 18px 16px;
  border-bottom: 1px solid var(--border-dim);
  display: flex;
  align-items: center;
  gap: 10px;
}
.rail-logo-icon {
  width: 32px;
  height: 32px;
  background: linear-gradient(135deg, #1e293b, #0f172a);
  border: 1px solid #334155;
  border-radius: 8px;
  display: grid;
  place-items: center;
  color: var(--cyan);
  flex-shrink: 0;
}
.rail-brand-title {
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: #fff;
  line-height: 1.2;
}
.rail-brand-sub {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.rail-nav-group {
  padding: 12px 8px;
  flex-grow: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.nav-section-title {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  padding: 8px 10px 4px 10px;
}
.rail-nav-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  color: var(--text-sub);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  background: transparent;
  width: 100%;
  text-align: left;
  transition: all 0.15s ease;
}
.rail-nav-item:hover {
  background: var(--bg-card-subtle);
  color: #fff;
}
.rail-nav-item.active {
  background: #182235;
  color: #fff;
  border-color: #2b3b59;
}
.rail-nav-item .nav-label-box {
  display: flex;
  align-items: center;
  gap: 9px;
}
.nav-svg-icon {
  width: 16px;
  height: 16px;
  display: inline-block;
  flex-shrink: 0;
}
.nav-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-card);
  border: 1px solid var(--border-dim);
  color: var(--text-sub);
}
.nav-badge.alert {
  background: var(--hold-dim);
  border-color: var(--hold-border);
  color: var(--hold);
}
.nav-badge.ok {
  background: var(--ok-dim);
  border-color: var(--ok-border);
  color: var(--ok);
}

.rail-footer {
  padding: 14px 12px;
  border-top: 1px solid var(--border-dim);
  background: #090c12;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.prod-rail-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--danger-dim);
  border: 1px solid var(--danger-border);
  color: #fca5a5;
  padding: 6px 10px;
  border-radius: 6px;
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.rail-sub-meta {
  font-size: 10.5px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* MAIN CONTENT WORKSPACE */
.main-stage {
  flex-grow: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-page);
}

/* TOP SYSTEM BAR */
.top-system-bar {
  position: sticky;
  top: 0;
  z-index: 80;
  background: rgba(15, 19, 28, 0.96);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-card);
  padding: 9px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.top-indicators-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.top-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-card);
  border: 1px solid var(--border-card);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-family: var(--font-mono);
  color: var(--text-sub);
}
.top-pill strong {
  color: #fff;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-muted);
}
.dot.ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.dot.hold { background: var(--hold); box-shadow: 0 0 8px var(--hold); }
.dot.danger { background: var(--danger); box-shadow: 0 0 8px var(--danger); }

.refresh-trigger-btn {
  background: var(--bg-card-subtle);
  color: var(--text-main);
  border: 1px solid var(--border-card);
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s ease;
}
.refresh-trigger-btn:hover {
  background: var(--bg-card-hover);
  border-color: var(--blue);
  color: #fff;
}

/* WORKSPACE VIEW WRAPPER */
.view-container {
  padding: 24px;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
  display: none;
  flex-direction: column;
  gap: 20px;
}
.view-container.active-view {
  display: flex;
}

/* COMMON SECTION HEADERS & CARDS */
.cockpit-card {
  background: var(--bg-card);
  border: 1px solid var(--border-card);
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.25);
}
.card-header-flex {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 10px;
}
.cockpit-view-title {
  font-size: 18px;
  font-weight: 800;
  color: #fff;
  letter-spacing: -0.01em;
  display: flex;
  align-items: center;
  gap: 10px;
}
.section-subtitle {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}

/* OPERATOR ATTENTION BANNER */
.attention-box {
  padding: 14px 18px;
  border-radius: 8px;
  display: flex;
  align-items: flex-start;
  gap: 14px;
  font-size: 13px;
  line-height: 1.5;
}
.attention-box.nominal {
  background: var(--ok-dim);
  border: 1px solid var(--ok-border);
  color: #a7f3d0;
}
.attention-box.system-wait {
  background: rgba(6, 182, 212, 0.12);
  border: 1px solid rgba(6, 182, 212, 0.35);
  color: #a5f3fc;
}
.attention-box.action-needed {
  background: var(--hold-dim);
  border: 1px solid var(--hold-border);
  color: #fde68a;
}
.attention-box.critical {
  background: var(--danger-dim);
  border: 1px solid var(--danger-border);
  color: #fca5a5;
}

/* SUMMARY METRIC STRIP */
.metric-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.metric-tile {
  background: var(--bg-card-subtle);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 6px;
}
.metric-tile-lbl {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.metric-tile-val {
  font-size: 18px;
  font-weight: 800;
  font-family: var(--font-mono);
  color: #fff;
}
.metric-tile-sub {
  font-size: 11px;
  color: var(--text-muted);
}

/* SEMANTIC EXECUTION PIPELINE */
.pipeline-flow {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--bg-input);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  overflow-x: auto;
  margin-bottom: 14px;
}
.pipe-step {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  background: transparent;
  color: var(--text-muted);
  border: 1px solid transparent;
  white-space: nowrap;
}
.pipe-step.active {
  background: var(--cyan-dim);
  color: var(--cyan);
  border-color: rgba(6, 182, 212, 0.4);
}
.pipe-step.done {
  color: var(--ok);
}
.pipe-arrow {
  color: var(--text-muted);
  font-size: 12px;
}

/* LANE COCKPIT CARDS */
.lane-cockpit-block {
  background: var(--bg-card-subtle);
  border: 1px solid var(--border-dim);
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 16px;
  transition: border-color 0.15s ease;
}
.lane-cockpit-block:hover {
  border-color: var(--border-card);
}
.lane-top-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}
.lane-badge-id {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.05em;
  color: var(--cyan);
  background: var(--cyan-dim);
  border: 1px solid rgba(6, 182, 212, 0.35);
  padding: 3px 9px;
  border-radius: 5px;
}
.lane-work-focus {
  background: var(--bg-input);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.focus-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}
.focus-lbl {
  color: var(--text-muted);
  width: 140px;
  flex-shrink: 0;
  font-size: 11.5px;
  text-transform: uppercase;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.focus-content {
  color: var(--text-main);
  flex-grow: 1;
  word-break: break-word;
}
.lane-history-split {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}
.history-metric-box {
  background: #0d1017;
  border: 1px solid var(--border-dim);
  border-radius: 6px;
  padding: 10px 12px;
}
.history-metric-box .label {
  font-size: 10.5px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
}
.history-metric-box .val {
  font-size: 16px;
  font-family: var(--font-mono);
  font-weight: 800;
  color: #fff;
  margin-top: 2px;
}

/* TABLES & MATRICES */
.matrix-wrap {
  width: 100%;
  overflow-x: auto;
}
table.cockpit-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 12.5px;
}
table.cockpit-table th {
  background: var(--bg-input);
  color: var(--text-muted);
  font-weight: 700;
  text-transform: uppercase;
  font-size: 10.5px;
  letter-spacing: 0.06em;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-card);
}
table.cockpit-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-dim);
  color: var(--text-main);
}
table.cockpit-table tr:hover td {
  background: rgba(255, 255, 255, 0.02);
}

/* BUTTONS & CONTROLS */
.btn {
  background: #f1f5f9;
  color: #0f172a;
  border: 0;
  border-radius: 6px;
  padding: 8px 16px;
  font-weight: 700;
  font-size: 12px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  transition: all 0.15s ease;
}
.btn:hover {
  background: #fff;
  box-shadow: 0 2px 8px rgba(255,255,255,0.15);
}
.btn:active {
  transform: scale(0.98);
}
.btn-cta {
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  color: #fff;
  border: 1px solid #3b82f6;
  padding: 10px 22px;
  font-size: 13px;
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.35);
}
.btn-cta:hover {
  background: linear-gradient(135deg, #3b82f6, #2563eb);
}
.btn-secondary {
  background: var(--bg-card-subtle);
  color: var(--text-main);
  border: 1px solid var(--border-card);
}
.btn-secondary:hover {
  background: var(--bg-card-hover);
  color: #fff;
  border-color: var(--border-focus);
}
.btn-danger {
  background: var(--danger-dim);
  color: #fca5a5;
  border: 1px solid var(--danger-border);
}
.btn-danger:hover {
  background: rgba(239, 68, 68, 0.25);
  color: #fff;
}
.btn-sm {
  padding: 4px 10px;
  font-size: 11.5px;
}

/* FORM ELEMENTS */
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 12px;
}
.form-row.full {
  grid-template-columns: 1fr;
}
label.field-lbl {
  display: block;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-sub);
  margin-bottom: 5px;
}
input[type=text], input[type=password], input[type=number], select, textarea {
  width: 100%;
  background: var(--bg-input);
  color: var(--text-main);
  border: 1px solid var(--border-card);
  border-radius: 6px;
  padding: 9px 12px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
input[type=text]:focus, input[type=password]:focus, input[type=number]:focus, select:focus, textarea:focus {
  border-color: var(--border-focus);
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
}
textarea.goal-main {
  min-height: 90px;
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.5;
  font-weight: 500;
}
.redline-box {
  border: 1px solid rgba(239, 68, 68, 0.4);
  background: rgba(239, 68, 68, 0.05);
  padding: 10px;
  border-radius: 8px;
}

/* STATUS CHIPS */
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.status-chip.ok { background: var(--ok-dim); border: 1px solid var(--ok-border); color: var(--ok); }
.status-chip.hold { background: var(--hold-dim); border: 1px solid var(--hold-border); color: var(--hold); }
.status-chip.danger { background: var(--danger-dim); border: 1px solid var(--danger-border); color: var(--danger); }
.status-chip.neutral { background: rgba(148, 163, 184, 0.1); border: 1px solid rgba(148, 163, 184, 0.25); color: var(--text-sub); }
.status-chip.cyan { background: var(--cyan-dim); border: 1px solid rgba(6, 182, 212, 0.35); color: var(--cyan); }
.status-chip.purple { background: rgba(139, 92, 246, 0.12); border: 1px solid rgba(139, 92, 246, 0.35); color: #c084fc; }

/* TERMINAL / LOG CONTAINERS */
.terminal-out {
  background: var(--bg-input);
  border: 1px solid var(--border-card);
  border-radius: 6px;
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-sub);
  min-height: 48px;
  max-height: 220px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin-top: 10px;
}

/* COPY BUTTONS */
.copy-btn {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  padding: 2px 5px;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.copy-btn:hover {
  color: var(--cyan);
  background: var(--cyan-dim);
}

/* RESPONSIVE LAYOUT BREAKPOINTS */
.ops-grid {
  display: grid;
  grid-template-columns: 1.2fr 0.8fr;
  gap: 20px;
}
.split-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}

@media (max-width: 1024px) {
  .nav-rail { width: 200px; }
  .form-row { grid-template-columns: 1fr; }
  .ops-grid { grid-template-columns: 1fr; }
  .split-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .app-shell { flex-direction: column; }
  .nav-rail { width: 100%; height: auto; position: static; }
  .rail-nav-group { flex-direction: row; flex-wrap: wrap; }
  .top-system-bar { position: static; }
  .ops-grid { grid-template-columns: 1fr; }
  .split-grid { grid-template-columns: 1fr; }
  .overview-lane-detail { grid-template-columns: minmax(0, 1fr) !important; align-items: start !important; }
  .overview-lane-detail .status-chip {
    max-width: 100%;
    white-space: normal;
    overflow-wrap: anywhere;
    text-align: center;
    justify-self: start;
  }
}

/* NOTIFICATION TOAST */
#cockpit-toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #182235;
  color: #fff;
  border: 1px solid #2d3e5e;
  border-radius: 8px;
  padding: 10px 18px;
  font-size: 12.5px;
  font-weight: 600;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  pointer-events: none;
  opacity: 0;
  transform: translateY(10px);
  transition: all 0.2s ease;
  z-index: 9999;
}
#cockpit-toast.show {
  opacity: 1;
  transform: translateY(0);
}
</style>
</head>
<body>

<div class="app-shell">
  <!-- LEFT NAVIGATION RAIL -->
  <aside class="nav-rail">
    <div class="rail-brand">
      <div class="rail-logo-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
          <polyline points="2 17 12 22 22 17"></polyline>
          <polyline points="2 12 12 17 22 12"></polyline>
        </svg>
      </div>
      <div>
        <div class="rail-brand-title">AOS DIRECT</div>
        <div class="rail-brand-sub">Autonomous Cockpit</div>
      </div>
    </div>

    <nav class="rail-nav-group">
      <div class="nav-section-title">Core Cockpit</div>
      <button class="rail-nav-item active" onclick="switchView('overview')" id="nav-btn-overview">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"></rect><rect x="14" y="3" width="7" height="5"></rect><rect x="14" y="12" width="7" height="9"></rect><rect x="3" y="16" width="7" height="5"></rect></svg>
          <span>Overview</span>
        </div>
        <span class="nav-badge" id="rail-attention-badge">UNKNOWN</span>
      </button>

      <button class="rail-nav-item" onclick="switchView('lanes')" id="nav-btn-lanes">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          <span>Lanes Cockpit</span>
        </div>
        <span class="nav-badge" id="rail-lanes-count">—</span>
      </button>

      <button class="rail-nav-item" onclick="switchView('providers')" id="nav-btn-providers">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
          <span>Providers</span>
        </div>
        <span class="nav-badge" id="rail-providers-status">—</span>
      </button>

      <button class="rail-nav-item" onclick="switchView('operations')" id="nav-btn-operations">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 14 14"></polyline></svg>
          <span>Operations &amp; Goal</span>
        </div>
      </button>

      <div class="nav-section-title" style="margin-top:10px;">Verification &amp; Safety</div>
      <button class="rail-nav-item" onclick="switchView('evidence')" id="nav-btn-evidence">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
          <span>Evidence &amp; SHA</span>
        </div>
        <span class="nav-badge" id="rail-provenance-badge">UNKNOWN</span>
      </button>

      <button class="rail-nav-item" onclick="switchView('diagnostics')" id="nav-btn-diagnostics">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>
          <span>Council &amp; Self-Repair</span>
        </div>
        <span class="nav-badge">SHADOW</span>
      </button>

      <button class="rail-nav-item" onclick="switchView('system')" id="nav-btn-system">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
          <span>System Internals</span>
        </div>
      </button>

      <button class="rail-nav-item" onclick="switchView('advanced')" id="nav-btn-advanced">
        <div class="nav-label-box">
          <svg class="nav-svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
          <span>Manual Override</span>
        </div>
      </button>
    </nav>

    <div class="rail-footer">
      <div class="prod-rail-badge" title="Production Safety Constraint: Production mutations are strictly disabled on Local Autonomous Host">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
        </svg>
        <span>PRODUCTION: NO_GO</span>
      </div>
      <div class="rail-sub-meta">
        <span>HOST: 127.0.0.1</span>
        <span>TOKEN SECURED</span>
      </div>
    </div>
  </aside>

  <!-- MAIN OPERATIONAL STAGE -->
  <main class="main-stage">
    <!-- TOP SYSTEM BAR -->
    <header class="top-system-bar">
      <div class="top-indicators-group">
        <div class="top-pill">
          <span id="top-host-dot" class="dot"></span>
          <span>Host:</span>
          <strong id="top-host-text">LOADING</strong>
        </div>

        <div class="top-pill">
          <span>Autonomy:</span>
          <strong id="top-autonomy-text">LOADING</strong>
        </div>

        <div class="top-pill">
          <span>Lanes:</span>
          <strong id="top-lanes-text">—</strong>
        </div>

        <div class="top-pill">
          <span>Providers:</span>
          <strong id="top-providers-text">—</strong>
        </div>

        <div class="top-pill">
          <span>Provenance:</span>
          <strong id="top-prov-text">UNKNOWN</strong>
        </div>

        <div class="status-chip danger" style="padding:4px 9px;" title="Production safety constraint">
          ⛔ PRODUCTION: NO_GO
        </div>
      </div>

      <div style="display:flex; align-items:center; gap:10px;">
        <span style="font-size:11.5px; color:var(--text-muted); font-family:var(--font-mono);" id="top-sync-time">
          Sync: --:--:--
        </span>
        <button class="refresh-trigger-btn" onclick="refreshStatus()">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
          </svg>
          <span>Refresh</span>
        </button>
      </div>
    </header>

    <!-- VIEW 1: OVERVIEW -->
    <section class="view-container active-view" id="view-overview">
      <!-- OPERATOR ATTENTION BLOCK -->
      <div id="overview-operator-attention" class="attention-box system-wait">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0; margin-top:2px;">
          <circle cx="12" cy="12" r="10"></circle>
          <polyline points="12 6 12 12 14 14"></polyline>
        </svg>
        <div>
          <div style="font-weight:700; font-size:14px;" id="attention-headline">Checking operator attention state…</div>
          <div style="color:var(--text-sub); font-size:12px; margin-top:3px;" id="attention-subtext">Evaluating alerts, active lane blockers, and reasoning provider capacity.</div>
        </div>
      </div>

      <!-- ACTIVE ALERTS EXPANDABLE CONTAINER -->
      <details class="cockpit-card" id="alerts-details" style="padding:12px 18px;">
        <summary style="cursor:pointer; font-weight:700; color:#fff; display:flex; align-items:center; justify-content:space-between;">
          <span>Active Operational Alerts (<span id="alerts-count-badge">0</span>)</span>
          <span style="font-size:11px; color:var(--text-muted); font-weight:normal;">Click to inspect details</span>
        </summary>
        <div id="alerts-view" style="margin-top:12px; font-family:var(--font-mono); font-size:12.5px; line-height:1.6;">Loading alerts…</div>
      </details>

      <!-- EXECUTIVE COCKPIT METRICS BAR -->
      <div class="metric-strip">
        <div class="metric-tile">
          <div class="metric-tile-lbl">AOS Health</div>
          <div class="metric-tile-val" id="ov-host-state">UNKNOWN</div>
          <div class="metric-tile-sub" id="ov-host-sub">Host state</div>
        </div>
        <div class="metric-tile">
          <div class="metric-tile-lbl">Active Execution</div>
          <div class="metric-tile-val" id="ov-active-lanes-count">—</div>
          <div class="metric-tile-sub" id="ov-progress-summary">Tracking active lanes</div>
        </div>
        <div class="metric-tile">
          <div class="metric-tile-lbl">Reasoning Providers</div>
          <div class="metric-tile-val" id="ov-providers-avail">—</div>
          <div class="metric-tile-sub">Independent of agent quota</div>
        </div>
        <div class="metric-tile">
          <div class="metric-tile-lbl">Cumulative Batches</div>
          <div class="metric-tile-val" id="ov-total-batches">—</div>
          <div class="metric-tile-sub">Authoritative total executed</div>
        </div>
        <div class="metric-tile">
          <div class="metric-tile-lbl">Runtime Provenance</div>
          <div class="metric-tile-val" id="ov-provenance-status">UNKNOWN</div>
          <div class="metric-tile-sub" id="ov-active-sha">SHA validation</div>
        </div>
      </div>

      <!-- CURRENT AUTONOMOUS ACTIVITY -->
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Current Autonomous Activity</span>
            </div>
            <div class="section-subtitle">Real-time multi-lane execution picture · Semantic stage progression</div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="switchView('lanes')">Open Full Lanes Cockpit ↗</button>
        </div>

        <div id="overview-lanes-summary" style="display:flex; flex-direction:column; gap:12px;">
          <!-- Populated dynamically by refreshStatus() -->
        </div>
      </div>

      <!-- QUICK ROUTINE OPERATIONS BAR -->
      <div class="cockpit-card" style="padding:14px 18px;">
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
          <div>
            <div style="font-weight:700; font-size:13px; color:#fff;">Routine Operations Bar</div>
            <div style="font-size:11.5px; color:var(--text-muted);">Safest next operator actions for ongoing autonomous cycles</div>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-secondary btn-sm" onclick="runOpCommand('continue')">▶ Continue</button>
            <button class="btn btn-secondary btn-sm" onclick="runOpCommand('pause-safe')">⏸ Pause-Safe</button>
            <button class="btn btn-secondary btn-sm" onclick="runOpCommand('resume')">⏯ Resume</button>
            <button class="btn btn-secondary btn-sm" onclick="runOpCommand('checkpoint-now')">💾 Checkpoint Now</button>
            <button class="btn btn-secondary btn-sm" onclick="runOpCommand('heartbeat-now')">💓 Heartbeat Now</button>
            <button class="btn btn-secondary btn-sm" onclick="switchView('operations')">Full Operations Center ↗</button>
          </div>
        </div>
      </div>
    </section>

    <!-- VIEW 2: LANES COCKPIT -->
    <section class="view-container" id="view-lanes">
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Autonomous Lane Cockpit</span>
            </div>
            <div class="section-subtitle">Individual execution units · Authoritative cumulative history distinguished from local window</div>
          </div>
          <div class="status-chip cyan">Autonomous DAG Execution</div>
        </div>

        <div id="lanes-view">
          <div style="color:var(--text-muted); padding:16px; text-align:center;">Loading autonomous lanes telemetry…</div>
        </div>
      </div>
    </section>

    <!-- VIEW 3: PROVIDER CONTROL CENTER -->
    <section class="view-container" id="view-providers">
      <!-- RESOURCE OPERATIONS & LINEAGE MATRIX (R1-R15) -->
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Resource Operations &amp; Task-Class Eligibility Matrix</span>
            </div>
            <div class="section-subtitle">Real-time operational inventory across Antigravity, Codex CLI, Cline, Qwen Local, and Free Cloud Providers</div>
          </div>
          <span class="status-chip ok" id="res-ops-header-chip">Resource OS Active</span>
        </div>

        <div class="matrix-wrap">
          <div id="resource-operations-container">Loading Resource Operations Matrix…</div>
        </div>
      </div>

      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Reasoning Provider Matrix &amp; Circuit Health</span>
            </div>
            <div class="section-subtitle">Real-time circuit states, health probes, and model telemetry independent of Antigravity agent quota</div>
          </div>
          <span class="status-chip neutral" id="prov-header-chip">Circuit Monitored</span>
        </div>

        <div class="matrix-wrap">
          <div id="providers">Loading provider telemetry…</div>
        </div>
      </div>

      <!-- WINDOWS CREDENTIAL MANAGER SECTION -->
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Provider Configuration · Windows Credential Manager</span>
            </div>
            <div class="section-subtitle">Local secure storage via DPAPI/WinCred · Secrets never returned to browser or written to Git</div>
          </div>
          <span class="status-chip ok">Secure Vault Protected</span>
        </div>

        <div class="attention-box nominal" style="margin-bottom:14px;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
            <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
          </svg>
          <div>
            <strong>Security Boundary Guarantee:</strong> Secrets are committed exclusively into the local Windows Credential Manager under your current OS profile. They cannot be queried back via the API, cannot leak to the web client, and are excluded from all AOS job packages.
          </div>
        </div>

        <div id="providers-dynamic-grid" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:14px;">
          <!-- Dynamically populated provider input boxes -->
        </div>
        <div id="provider-message" class="terminal-out" style="display:none;"></div>
      </div>
    </section>

    <!-- VIEW 4: OPERATIONS & GOAL COMPOSER -->
    <section class="view-container" id="view-operations">
      <div class="ops-grid">
        <!-- AUTONOMOUS GOAL COMPOSER -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Autonomous Goal Composer</span>
              </div>
              <div class="section-subtitle">Standing authority goal initiation · Generates DAG, executes, verifies, and replans</div>
            </div>
            <span class="status-chip cyan">Normal Operator Mode</span>
          </div>

          <!-- PRIMARY GOAL INPUT -->
          <div class="form-row full">
            <div>
              <label class="field-lbl" for="goal-text" style="color:#60a5fa; font-size:12px;">★ Primary Autonomous Goal (Dominant Directive)</label>
              <textarea class="goal-main" id="goal-text" spellcheck="true">Continue this project to completion under standing authority.</textarea>
            </div>
          </div>

          <!-- ADVANCED EXECUTION CONTEXT -->
          <div class="form-row">
            <div>
              <label class="field-lbl" for="goal-project">Configured Runtime Project</label>
              <select id="goal-project" onchange="syncGoalProjectProfile()"></select>
            </div>
            <div>
              <label class="field-lbl" for="goal-descriptor">Resolved Project Descriptor</label>
              <input id="goal-descriptor" type="text" readonly>
            </div>
          </div>

          <div class="form-row">
            <div>
              <label class="field-lbl" for="goal-workspace">Workspace Directory</label>
              <input id="goal-workspace" type="text" readonly>
            </div>
            <div>
              <label class="field-lbl" for="goal-policy">Routing Policy Path</label>
              <input id="goal-policy" type="text" readonly>
            </div>
          </div>

          <!-- SAFETY BOUNDARIES & RED LINES -->
          <div class="form-row">
            <div>
              <label class="field-lbl" for="goal-constraints">Operational Constraints (one per line)</label>
              <textarea id="goal-constraints" style="min-height:75px;" placeholder="e.g. Do not touch production files&#10;Maintain 100% unit tests pass"></textarea>
            </div>
            <div class="redline-box">
              <label class="field-lbl" for="goal-redlines" style="color:#fca5a5;">🛡️ Explicit Red Lines (Forbidden Actions)</label>
              <textarea id="goal-redlines" style="min-height:75px; background:#140c0f; border-color:rgba(239,68,68,0.3);" placeholder="e.g. No external network mutations&#10;No deleting git history"></textarea>
            </div>
          </div>

          <div class="form-row">
            <div>
              <label class="field-lbl" for="goal-batches">Execution Budget (Max Batches This Invocation)</label>
              <input id="goal-batches" type="number" min="1" max="50" value="12">
            </div>
          </div>

          <div style="display:flex; align-items:center; gap:12px; margin-top:16px;">
            <button id="btn-submit-goal" class="btn btn-cta" onclick="submitGoal()">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
              </svg>
              <span>START / CONTINUE AUTONOMOUS EXECUTION</span>
            </button>
            <button class="btn btn-secondary" onclick="refreshStatus()">Refresh State</button>
          </div>
          <div id="goal-message" class="terminal-out" style="display:none;"></div>
        </div>

        <!-- OPERATIONS COMMAND SURFACE -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Operations Command Surface</span>
              </div>
              <div class="section-subtitle">Bounded IPC runtime commands · No state corruption risk</div>
            </div>
            <span class="status-chip neutral">IPC Bounded</span>
          </div>

          <div style="margin-bottom:14px;">
            <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">
              1. Normal Operations
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
              <button class="btn btn-secondary" onclick="runOpCommand('continue')">▶ Continue</button>
              <button class="btn btn-secondary" onclick="runOpCommand('resume')">⏯ Resume</button>
              <button class="btn btn-secondary" onclick="runOpCommand('checkpoint-now')">💾 Checkpoint Now</button>
              <button class="btn btn-secondary" onclick="runOpCommand('heartbeat-now')">💓 Heartbeat Now</button>
              <button class="btn btn-secondary" onclick="runOpCommand('publish-relay-now')">📡 Publish Relay</button>
              <button class="btn btn-secondary" onclick="refreshStatus()">🔄 Refresh Status</button>
            </div>
          </div>

          <div style="margin-bottom:14px; padding-top:12px; border-top:1px solid var(--border-dim);">
            <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--hold); margin-bottom:8px;">
              2. Controlled Interruption
            </div>
            <button class="btn btn-secondary" style="width:100%; border-color:var(--hold-border);" onclick="runOpCommand('pause-safe')">
              ⏸ Pause-Safe (Finishes In-Flight Subtask &amp; Holds)
            </button>
          </div>

          <div style="padding-top:12px; border-top:1px solid var(--border-dim);">
            <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:#fca5a5; margin-bottom:8px;">
              3. Recovery &amp; Worker Intervention
            </div>
            <button class="btn btn-danger" style="width:100%;" onclick="runOpCommand('restart-worker')">
              ⚠️ Restart-Worker (Targeted Command Termination)
            </button>
          </div>

          <div style="margin-top:16px;">
            <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--text-muted); margin-bottom:6px;">
              IPC Execution Result
            </div>
            <div id="op-command-msg" class="terminal-out">Ready for operator commands.</div>
          </div>
        </div>
      </div>
    </section>

    <!-- VIEW 5: EVIDENCE & PROVENANCE -->
    <section class="view-container" id="view-evidence">
      <!-- RUNTIME IDENTITY & PROVENANCE TABLE -->
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Runtime Identity &amp; Exact SHA Provenance</span>
            </div>
            <div class="section-subtitle">Authoritative Git Head cryptographic verification · Exact runtime code provenance</div>
          </div>
          <span class="status-chip neutral" id="ev-prov-chip">UNKNOWN</span>
        </div>

        <div class="matrix-wrap">
          <table class="cockpit-table">
            <thead>
              <tr>
                <th>Identity Field</th>
                <th>Authoritative SHA / Value</th>
                <th>Validation Status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>Runtime Source SHA</strong></td>
                <td><code id="active-sha" style="color:var(--cyan); font-weight:700;">Loading…</code></td>
                <td><span class="status-chip neutral" id="sha-format-status">UNKNOWN</span></td>
                <td><button class="copy-btn" onclick="copyEl('active-sha')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>Local Authoritative Git Head</strong></td>
                <td><code id="local-git-head">Loading…</code></td>
                <td><span class="status-chip neutral">Local Authoritative</span></td>
                <td><button class="copy-btn" onclick="copyEl('local-git-head')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>Candidate Manifest SHA</strong></td>
                <td><code id="manifest-sha">Loading…</code></td>
                <td><span class="status-chip neutral">Manifest Bound</span></td>
                <td><button class="copy-btn" onclick="copyEl('manifest-sha')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>Build Source SHA</strong></td>
                <td><code id="build-sha">Loading…</code></td>
                <td><span class="status-chip neutral">Compiled Source</span></td>
                <td><button class="copy-btn" onclick="copyEl('build-sha')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>CI Head SHA</strong></td>
                <td><code id="ci-head-sha">Loading…</code></td>
                <td><span class="status-chip neutral" id="ci-head-status">UNAVAILABLE</span></td>
                <td><button class="copy-btn" onclick="copyEl('ci-head-sha')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>Active Execution Slot</strong></td>
                <td><code id="active-slot">Loading…</code></td>
                <td><span class="status-chip cyan">Active Process Slot</span></td>
                <td><button class="copy-btn" onclick="copyEl('active-slot')">📋 Copy</button></td>
              </tr>
              <tr>
                <td><strong>Provenance Audit Verdict</strong></td>
                <td colspan="2"><strong id="provenance-status">UNKNOWN</strong></td>
                <td><span class="status-chip neutral" id="prov-verdict-chip">Audit Pending</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- SPLIT: PRODUCT EVIDENCE & CONTROLLER RELAY -->
      <div class="split-grid">
        <!-- PRODUCT MUTATION & ACCEPTANCE EVIDENCE -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Product Mutation &amp; Acceptance Evidence</span>
              </div>
              <div class="section-subtitle">Measurable workspace productization proof</div>
            </div>
            <span class="status-chip neutral">Verifiable Progress</span>
          </div>
          <div id="product-view">Loading product acceptance evidence…</div>
        </div>

        <!-- CONTROLLER RELAY SYNCHRONIZATION -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Controller Relay &amp; Remote Outbox</span>
              </div>
              <div class="section-subtitle">Heartbeat pub/sub and remote synchronization status</div>
            </div>
            <span class="status-chip neutral">Sync Subsystem</span>
          </div>

          <!-- RELAY PIPELINE VISUALIZATION -->
          <div class="pipeline-flow" style="margin-bottom:12px;">
            <span class="pipe-step done">AOS Runtime</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step active" id="relay-step-status">Controller Relay</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step" id="outbox-step-status">Remote Outbox</span>
          </div>

          <div id="relay-view">Loading relay telemetry snapshot…</div>
        </div>
      </div>
    </section>

    <!-- VIEW 6: DIAGNOSTICS & DELIBERATION (COUNCIL & SELF-REPAIR) -->
    <section class="view-container" id="view-diagnostics">
      <div class="split-grid">
        <!-- COUNCIL DELIBERATION (SHADOW ONLY) -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Council Deliberation Shadow Metrics</span>
              </div>
              <div class="section-subtitle">Cross-model consensus sampling · Evaluated against primary decisions</div>
            </div>
            <span class="status-chip hold" title="Real Production Sampling - strictly no execution mutation authority">SHADOW MODE ONLY</span>
          </div>

          <div class="attention-box nominal" style="margin-bottom:14px; background:rgba(139,92,246,0.1); border-color:rgba(139,92,246,0.3); color:#e9d5ff;">
            <div>
              <strong>Authority Posture:</strong> Council operates in shadow mode. It observes and samples decision boundaries to measure reasoning agreement without mutation authority.
            </div>
          </div>

          <div id="deliberation-view">Loading deliberation metrics…</div>
        </div>

        <!-- AUTONOMOUS SELF-REPAIR (SHADOW / GOVERNED) -->
        <div class="cockpit-card">
          <div class="card-header-flex">
            <div>
              <div class="cockpit-view-title">
                <span>Autonomous Self-Repair Observability</span>
              </div>
              <div class="section-subtitle">System self-diagnosis and shadow proposal inspection</div>
            </div>
            <span class="status-chip hold">SHADOW / GOVERNED</span>
          </div>

          <div class="attention-box nominal" style="margin-bottom:14px; background:rgba(245,158,11,0.08); border-color:rgba(245,158,11,0.3); color:#fde68a;">
            <div>
              <strong>Observation Only:</strong> Live autonomous mutation is inactive. All findings and repair proposals remain governed under shadow observability.
            </div>
          </div>

          <div id="self-repair-view">Loading self-repair telemetry…</div>
        </div>
      </div>
    </section>

    <!-- VIEW 7: SYSTEM INTERNALS -->
    <section class="view-container" id="view-system">
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>System &amp; Infrastructure Internals</span>
            </div>
            <div class="section-subtitle">Low-frequency process supervisors, process trees, and runtime bindings</div>
          </div>
          <span class="status-chip neutral">Host Internals</span>
        </div>

        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; margin-bottom:16px;">
          <div class="metric-tile">
            <div class="metric-tile-lbl">Host State</div>
            <div class="metric-tile-val" id="host">Loading…</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-lbl">Supervisor PID</div>
            <div class="metric-tile-val" id="supervisor-pid">Loading…</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-lbl">Runtime API PID</div>
            <div class="metric-tile-val" id="runtime-pid">Loading…</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-lbl">Production Safety Gate</div>
            <div class="metric-tile-val danger" id="production">NO_GO</div>
          </div>
        </div>

        <div style="font-size:12px; color:var(--text-sub); line-height:1.6; background:var(--bg-input); padding:14px; border-radius:8px; border:1px solid var(--border-dim);">
          <div>• <strong>HTTP Ingress:</strong> Bound strictly to loopback interface (<code>127.0.0.1</code>)</div>
          <div>• <strong>Authentication:</strong> Session-isolated bearer token verified on all mutation endpoints via <code>X-AOS-Panel-Token</code></div>
          <div>• <strong>Job Admission:</strong> Revalidated by <code>aos.local_host</code> and bound by Autonomous Host V1</div>
          <div>• <strong>Process Supervisor:</strong> Supervises lane subprocess lifecycles, circuit breakers, and bounded IPC queues</div>
        </div>
      </div>
    </section>

    <!-- VIEW 8: ADVANCED MANUAL OVERRIDE -->
    <section class="view-container" id="view-advanced">
      <div class="cockpit-card">
        <div class="card-header-flex">
          <div>
            <div class="cockpit-view-title">
              <span>Manual Bounded Run-Plan Override</span>
            </div>
            <div class="section-subtitle">Debug / Replay Only · Normal autonomous operation generates its own DAG</div>
          </div>
          <span class="status-chip neutral">Debug / Replay</span>
        </div>

        <div class="attention-box nominal" style="margin-bottom:14px; background:rgba(100,116,139,0.1); border-color:rgba(100,116,139,0.3); color:var(--text-sub);">
          <div>
            <strong>Operator Notice:</strong> AOS normally selects objectives, generates tasks, and executes autonomously. This manual JSON envelope submission is reserved strictly for reproducible offline debugging, benchmark replays, or custom emergency injections.
          </div>
        </div>

        <textarea id="job" spellcheck="false" placeholder="Paste a validated *.aosjob.json envelope here…" style="min-height:240px; margin-bottom:12px;"></textarea>
        <div style="display:flex; align-items:center; gap:10px;">
          <button class="btn btn-secondary" onclick="submitJob()">Submit Manual Run-Plan</button>
        </div>
        <div id="message" class="terminal-out" style="display:none; margin-top:10px;"></div>
      </div>
    </section>
  </main>
</div>

<!-- FLOATING NOTIFICATION TOAST -->
<div id="cockpit-toast">Notification</div>

<script>
const TOKEN = __AOS_TOKEN_JSON__;
let GOAL_PROJECTS = {};

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function syncGoalProjectProfile() {
  const selector = document.getElementById('goal-project');
  const profile = selector ? GOAL_PROJECTS[selector.value] : null;
  const values = profile || {};
  const fields = {
    'goal-descriptor': values.descriptor_path || '',
    'goal-workspace': values.workspace || '',
    'goal-policy': values.routing_policy_path || ''
  };
  for (const [id, value] of Object.entries(fields)) {
    const element = document.getElementById(id);
    if (element) element.value = value;
  }
}

function toast(msg) {
  const el = document.getElementById('cockpit-toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2200);
}

function copyEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.getAttribute('data-full-val') || el.textContent || '';
  navigator.clipboard.writeText(text).then(() => {
    toast('Copied: ' + text.slice(0, 16) + (text.length > 16 ? '…' : ''));
  }).catch(() => {
    toast('Copy failed');
  });
}

function switchView(viewId) {
  document.querySelectorAll('.view-container').forEach(v => v.classList.remove('active-view'));
  const target = document.getElementById('view-' + viewId);
  if (target) target.classList.add('active-view');

  document.querySelectorAll('.rail-nav-item').forEach(btn => btn.classList.remove('active'));
  const activeBtn = document.getElementById('nav-btn-' + viewId);
  if (activeBtn) activeBtn.classList.add('active');
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const s = await r.json();

    // 1. Host State & Top Bar
    const hostVal = s.host_state || 'UNKNOWN';
    const isHold = hostVal.includes('HOLD') || hostVal.includes('DEGRADED') || hostVal.includes('REQUIRED');
    const isFail = hostVal.includes('FAIL') || hostVal.includes('ERROR');
    const isHostOk = (hostVal === 'RUNNING' || hostVal === 'HEALTHY');

    const hostEl = document.getElementById('host');
    if (hostEl) {
      hostEl.textContent = hostVal;
      hostEl.className = 'metric-tile-val ' + (isFail ? 'danger' : (isHold ? 'hold' : (isHostOk ? 'ok' : 'neutral')));
    }

    const topHostText = document.getElementById('top-host-text');
    if (topHostText) topHostText.textContent = hostVal;

    const topHostDot = document.getElementById('top-host-dot');
    if (topHostDot) {
      topHostDot.className = 'dot ' + (isFail ? 'danger' : (isHold ? 'hold' : (isHostOk ? 'ok' : '')));
    }

    const ovHost = document.getElementById('ov-host-state');
    if (ovHost) {
      ovHost.textContent = hostVal;
      ovHost.className = 'metric-tile-val ' + (isFail ? 'danger' : (isHold ? 'hold' : (isHostOk ? 'ok' : 'neutral')));
    }

    // 2. Active Slot & SHAs
    const slotVal = s.active_slot || 'NONE';
    const activeSlotEl = document.getElementById('active-slot');
    if (activeSlotEl) {
      activeSlotEl.textContent = slotVal.slice(0, 32);
      activeSlotEl.setAttribute('data-full-val', slotVal);
    }

    const shaVal = s.active_sha || 'NONE';
    const activeShaEl = document.getElementById('active-sha');
    if (activeShaEl) {
      activeShaEl.textContent = shaVal.slice(0, 16) + (shaVal.length > 16 ? '…' : '');
      activeShaEl.setAttribute('data-full-val', shaVal);
    }
    const ovSha = document.getElementById('ov-active-sha');
    if (ovSha) ovSha.textContent = 'SHA: ' + shaVal.slice(0, 12);

    const localGit = s.local_git_head || 'UNAVAILABLE';
    const localGitEl = document.getElementById('local-git-head');
    if (localGitEl) {
      localGitEl.textContent = localGit.slice(0, 16) + (localGit.length > 16 ? '…' : '');
      localGitEl.setAttribute('data-full-val', localGit);
    }

    const manifestSha = s.candidate_manifest_sha || 'UNAVAILABLE';
    const manifestEl = document.getElementById('manifest-sha');
    if (manifestEl) {
      manifestEl.textContent = manifestSha.slice(0, 16) + (manifestSha.length > 16 ? '…' : '');
      manifestEl.setAttribute('data-full-val', manifestSha);
    }

    const buildSha = s.build_source_sha || 'UNAVAILABLE';
    const buildEl = document.getElementById('build-sha');
    if (buildEl) {
      buildEl.textContent = buildSha.slice(0, 16) + (buildSha.length > 16 ? '…' : '');
      buildEl.setAttribute('data-full-val', buildSha);
    }

    const ciSha = s.ci_head_sha || 'UNAVAILABLE';
    const ciEl = document.getElementById('ci-head-sha');
    if (ciEl) {
      ciEl.textContent = ciSha.slice(0, 16) + (ciSha.length > 16 ? '…' : '');
      ciEl.setAttribute('data-full-val', ciSha);
    }
    const ciHeadStatus = document.getElementById('ci-head-status');
    if (ciHeadStatus) {
      ciHeadStatus.textContent = s.ci_head_sha ? 'CI Upstream' : 'UNAVAILABLE';
    }

    const shaFormat = s.sha_format_status || 'UNKNOWN';
    const shaFormatEl = document.getElementById('sha-format-status');
    if (shaFormatEl) {
      shaFormatEl.textContent = shaFormat;
      shaFormatEl.className = 'status-chip ' + (shaFormat === 'VALID' ? 'ok' : (shaFormat === 'INVALID' ? 'danger' : 'neutral'));
    }

    // Provenance Status
    const provStatus = s.provenance_status || 'UNKNOWN';
    const isProvOk = (provStatus === 'PROVEN');
    const isProvFail = (provStatus === 'FAIL');

    const provEl = document.getElementById('provenance-status');
    if (provEl) {
      provEl.textContent = provStatus;
      provEl.className = isProvOk ? 'ok' : (isProvFail ? 'danger' : 'neutral');
    }
    const topProv = document.getElementById('top-prov-text');
    if (topProv) {
      topProv.textContent = provStatus;
      topProv.className = isProvOk ? 'ok' : (isProvFail ? 'danger' : 'neutral');
    }
    const ovProv = document.getElementById('ov-provenance-status');
    if (ovProv) {
      ovProv.textContent = provStatus;
      ovProv.className = 'metric-tile-val ' + (isProvOk ? 'ok' : (isProvFail ? 'danger' : 'neutral'));
    }
    const railProv = document.getElementById('rail-provenance-badge');
    if (railProv) {
      railProv.textContent = provStatus;
      railProv.className = 'nav-badge ' + (isProvOk ? 'ok' : (isProvFail ? 'alert' : ''));
    }
    const evChip = document.getElementById('ev-prov-chip');
    if (evChip) {
      evChip.textContent = provStatus;
      evChip.className = 'status-chip ' + (isProvOk ? 'ok' : (isProvFail ? 'danger' : 'neutral'));
    }
    const provVerdictChip = document.getElementById('prov-verdict-chip');
    if (provVerdictChip) {
      provVerdictChip.textContent = isProvOk ? 'Verified' : (isProvFail ? 'Check Failed' : 'Audit Pending');
      provVerdictChip.className = 'status-chip ' + (isProvOk ? 'ok' : (isProvFail ? 'danger' : 'neutral'));
    }

    // Supervisor & Runtime PID
    const supPid = (s.runtime_v1 || {}).runtime_supervisor_pid || 'NONE';
    const runPid = (s.runtime_v1 || {}).pid || 'NONE';
    if (document.getElementById('supervisor-pid')) document.getElementById('supervisor-pid').textContent = supPid;
    if (document.getElementById('runtime-pid')) document.getElementById('runtime-pid').textContent = runPid;

    // Production Safety
    const prodVal = s.production || 'NO_GO';
    if (document.getElementById('production')) document.getElementById('production').textContent = prodVal;

    // Last Sync Time
    const now = new Date();
    const timeStr = now.toTimeString().slice(0, 8);
    const topSync = document.getElementById('top-sync-time');
    if (topSync) topSync.textContent = 'Sync: ' + timeStr;

    // 3. Autonomous Lanes Telemetry & Durable History Projection
    const lanes = s.lanes || {};
    const laneKeys = Object.keys(lanes);

    const activeStates = new Set([
      'QUEUED',
      'RUNNING',
      'RECOVERING',
      'EXECUTING',
      'WAITING_FOR_REASONING_PROVIDER',
      'WAITING_FOR_SOURCE_TRANSPORT'
    ]);

    const activeLanesCount = laneKeys.filter(
      key => activeStates.has(
        String((lanes[key] || {}).state || '')
      )
    ).length;

    const trackedLanesCount = laneKeys.length;

    const runningCount = laneKeys.filter(key => ['RUNNING', 'EXECUTING'].includes(String((lanes[key] || {}).state || ''))).length;
    const heldCount = laneKeys.filter(key => ['HUMAN_REQUIRED', 'WAITING_FOR_REASONING_PROVIDER', 'WAITING_FOR_SOURCE_TRANSPORT', 'RECOVERING', 'QUEUED'].includes(String((lanes[key] || {}).state || ''))).length;

    const topLanesText = document.getElementById('top-lanes-text');
    if (topLanesText) {
      if (runningCount === 0 && heldCount > 0) {
        topLanesText.textContent = `${runningCount} Running / ${heldCount} Held`;
      } else {
        topLanesText.textContent = activeLanesCount + ' Active';
      }
    }

    const railLanesCount = document.getElementById('rail-lanes-count');
    if (railLanesCount) railLanesCount.textContent = String(activeLanesCount);

    const ovActiveLanes = document.getElementById('ov-active-lanes-count');
    if (ovActiveLanes) {
      if (runningCount === 0 && heldCount > 0) {
        ovActiveLanes.textContent = `${runningCount} Running / ${heldCount} Held`;
      } else {
        ovActiveLanes.textContent = activeLanesCount + (activeLanesCount === 1 ? ' Lane' : ' Lanes');
      }
    }

    let totalCumulativeBatches = 0;
    let anyHumanRequired = false;
    let anyFailed = false;
    let anyWaiting = false;
    let anyRunning = false;
    let activeBlockerList = [];

    const lanesView = document.getElementById('lanes-view');
    const ovLanesSummary = document.getElementById('overview-lanes-summary');

    if (trackedLanesCount === 0) {
      if (lanesView) lanesView.innerHTML = '<div style="color:var(--text-muted); padding:20px; text-align:center;"><em>No tracked autonomous lanes found.</em></div>';
      if (ovLanesSummary) ovLanesSummary.innerHTML = '<div style="color:var(--text-muted); padding:12px;"><em>No durable lane telemetry is currently available.</em></div>';
    } else {
      let cockpitHtml = '';
      let overviewSummaryHtml = '';

      for (const k of laneKeys) {
        const item = lanes[k];
        const stateStr = item.state || 'UNKNOWN';

        if (stateStr === 'HUMAN_REQUIRED') anyHumanRequired = true;
        else if (stateStr === 'FAILED') anyFailed = true;
        else if (stateStr.startsWith('WAITING')) anyWaiting = true;
        else if (stateStr === 'RUNNING' || stateStr === 'EXECUTING') anyRunning = true;

        const isRun = (stateStr === 'RUNNING' || stateStr === 'EXECUTING');
        const isWait = stateStr.startsWith('WAITING');
        const isHuman = (stateStr === 'HUMAN_REQUIRED');
        const stateClass = isRun ? 'ok' : (isHuman || stateStr === 'FAILED' ? 'danger' : (isWait ? 'hold' : 'neutral'));

        const cmdId = item.command_id || 'NONE';

        // Canonical durable history projection
        const totalEx = item.total_executed_batches ?? item.completed_batches ?? 0;
        totalCumulativeBatches += totalEx;

        const planningBatch = item.planning_batch_number ?? item.current_batch ?? 0;
        const succVal = (item.successful_batches !== null && item.successful_batches !== undefined) ? String(item.successful_batches) : '—';
        const failVal = (item.failed_batches !== null && item.failed_batches !== undefined) ? String(item.failed_batches) : '—';
        const winVal = (item.recent_window_size !== null && item.recent_window_size !== undefined) ? String(item.recent_window_size) : '—';

        const phase = item.current_planning_phase || 'UNKNOWN';
        const progressTime = item.last_meaningful_progress_at ? String(item.last_meaningful_progress_at).replace('T', ' ').slice(0, 19) : 'NONE';
        const blocker = item.current_blocker;

        if (blocker && blocker !== 'NONE') {
          activeBlockerList.push(`Lane ${k.toUpperCase()}: ${blocker}`);
        }

        // Semantic Pipeline Steps Mapping (No false claims)
        const pUpper = phase.toUpperCase();
        let stepObjActive = false, stepPlanActive = false, stepExecActive = false, stepValActive = false, stepCheckActive = false, stepReplanActive = false;
        if (pUpper.includes('OBJECTIVE') || pUpper.includes('SELECT')) {
          stepObjActive = true;
        } else if (pUpper.includes('PLAN') || pUpper.includes('COMPILE')) {
          stepPlanActive = true;
        } else if (pUpper.includes('EXECUTE') || pUpper.includes('RUN') || pUpper.includes('PROCESS')) {
          stepExecActive = true;
        } else if (pUpper.includes('VALIDATE') || pUpper.includes('VERIFY')) {
          stepValActive = true;
        } else if (pUpper.includes('CHECKPOINT') || pUpper.includes('RECEIPT')) {
          stepCheckActive = true;
        } else if (pUpper.includes('REPLAN')) {
          stepReplanActive = true;
        }

        const safeObj = escapeHtml(item.current_objective || 'None specified');
        const safeTask = escapeHtml(item.current_task || 'NONE');
        const safeLastTask = escapeHtml(item.most_recent_completed_task || 'NONE');
        const safeNext = escapeHtml(item.canonical_next_action || 'Computing next step…');
        const safeBlocker = escapeHtml(blocker || 'NONE');
        const safePhase = escapeHtml(phase);
        const safeCmd = escapeHtml(cmdId);
        const safeCi = escapeHtml(item.tests_ci_state ? JSON.stringify(item.tests_ci_state) : '—');

        // Overview Summary Row
        overviewSummaryHtml += `
        <div style="background:var(--bg-card-subtle); border:1px solid var(--border-dim); border-radius:8px; padding:14px;">
          <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:10px;">
              <span class="lane-badge-id">LANE ${escapeHtml(k.toUpperCase())}</span>
              <span style="font-weight:700; color:#fff; font-size:13.5px;">${safeObj.slice(0, 65)}</span>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="status-chip ${stateClass}">${escapeHtml(stateStr)}</span>
              <span style="font-size:11.5px; font-family:var(--font-mono); color:var(--text-muted);">Batch #${planningBatch}</span>
            </div>
          </div>
          <div class="overview-lane-detail" style="display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center;">
            <div style="font-size:12px; color:var(--text-sub);">
              <div>Task: <code style="color:var(--text-main);">${safeTask}</code></div>
              <div style="color:var(--text-muted); margin-top:2px;">Phase: <strong>${safePhase}</strong> · Next: ${safeNext}</div>
            </div>
            ${blocker && blocker !== 'NONE' ? `<span class="status-chip hold">${safeBlocker}</span>` : '<span class="status-chip neutral">Active</span>'}
          </div>
        </div>`;

        // Full Lanes Cockpit Card
        cockpitHtml += `
        <div class="lane-cockpit-block">
          <div class="lane-top-head">
            <div style="display:flex; align-items:center; gap:10px;">
              <span class="lane-badge-id">LANE ${escapeHtml(k.toUpperCase())}</span>
              <span style="font-weight:800; font-size:15px; color:#fff;">${safeObj.slice(0, 75)}</span>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="status-chip ${stateClass}">${escapeHtml(stateStr)}</span>
              <span style="font-size:11px; font-family:var(--font-mono); color:var(--text-muted);">
                Cmd: <strong>${safeCmd.slice(0, 16)}…</strong>
              </span>
              <button class="copy-btn" onclick="navigator.clipboard.writeText('${safeCmd}'); toast('Copied Command ID');" title="Copy Command ID">📋</button>
            </div>
          </div>

          <!-- Semantic Execution Pipeline -->
          <div class="pipeline-flow">
            <span class="pipe-step ${stepObjActive ? 'active' : ''}">1. Objective</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step ${stepPlanActive ? 'active' : ''}">2. Plan</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step ${stepExecActive ? 'active' : ''}">3. Execute</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step ${stepValActive ? 'active' : ''}">4. Validate</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step ${stepCheckActive ? 'active' : ''}">5. Checkpoint</span>
            <span class="pipe-arrow">➔</span>
            <span class="pipe-step ${stepReplanActive ? 'active' : ''}">6. Replan</span>
          </div>

          <!-- Active Work Unit Details -->
          <div class="lane-work-focus">
            <div class="focus-line">
              <div class="focus-lbl">Objective:</div>
              <div class="focus-content"><strong>${safeObj}</strong></div>
            </div>
            <div class="focus-line">
              <div class="focus-lbl">Current Phase:</div>
              <div class="focus-content"><code>${safePhase}</code></div>
            </div>
            <div class="focus-line">
              <div class="focus-lbl">Current Task:</div>
              <div class="focus-content"><code>${safeTask}</code></div>
            </div>
            <div class="focus-line">
              <div class="focus-lbl">Last Completed:</div>
              <div class="focus-content"><code>${safeLastTask}</code></div>
            </div>
            <div class="focus-line">
              <div class="focus-lbl">Next Action:</div>
              <div class="focus-content" style="color:#60a5fa;">${safeNext}</div>
            </div>
            <div class="focus-line">
              <div class="focus-lbl">Blocker:</div>
              <div class="focus-content">
                ${blocker && blocker !== 'NONE' ? `<span class="status-chip hold">${safeBlocker}</span>` : '<span style="color:var(--text-muted);">None reported</span>'}
              </div>
            </div>
          </div>

          <!-- Authoritative History vs Local Visibility Invariant -->
          <div class="lane-history-split">
            <div class="history-metric-box">
              <div class="label">Total Executed</div>
              <div class="val ok" title="Authoritative cumulative history across all restarts">${totalEx} batches</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Successful</div>
              <div class="val">${succVal}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Failed / Partial</div>
              <div class="val">${failVal}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Current Planning Batch</div>
              <div class="val">#${planningBatch}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Recent Window</div>
              <div class="val">${winVal}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Worker Attempts</div>
              <div class="val">${item.worker_execution_attempt_count ?? item.attempts ?? 0}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Last Meaningful Progress</div>
              <div class="val" style="font-size:12px;">${escapeHtml(progressTime)}</div>
            </div>
            <div class="history-metric-box">
              <div class="label">Tests / CI State</div>
              <div class="val" style="font-size:12px;">${safeCi}</div>
            </div>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:10px; padding-top:8px; border-top:1px solid var(--border-dim); flex-wrap:wrap; gap:8px;">
            <div style="font-size:11px; color:var(--text-muted);">
              ${isHuman ? '🛑 <strong style="color:#fca5a5;">Held under RECOVERY_CHURN_GUARD</strong> · Review required before resuming' : 'Status: ' + escapeHtml(stateStr)}
            </div>
            <div style="display:flex; gap:8px;">
              <button class="btn btn-secondary btn-sm" onclick="alert('LANE INSPECTION: ' + escapeHtml(k.toUpperCase()) + '\\nCommand: ' + '${safeCmd}' + '\\nState: ' + '${escapeHtml(stateStr)}' + '\\nBlocker: ' + '${safeBlocker}' + '\\nBatch: #' + '${planningBatch}' + '\\nNext Safe Action: Dispatch Native Resume via Runtime API');">
                Review ${escapeHtml(k.toUpperCase())} Hold
              </button>
              <button class="btn btn-cta btn-sm" onclick="runOpCommand('restart-worker', '${safeCmd}')">
                Resume ${escapeHtml(k.toUpperCase())}
              </button>
            </div>
          </div>
          <div style="font-size:10.5px; color:var(--text-muted); margin-top:4px;">
            ℹ️ <em>Durable History: Total Executed represents cumulative completions across all restarts; Recent Window is bounded display size.</em>
          </div>
        </div>`;
      }

      if (lanesView) lanesView.innerHTML = cockpitHtml;
      if (ovLanesSummary) ovLanesSummary.innerHTML = overviewSummaryHtml;
    }

    const ovTotalBatches = document.getElementById('ov-total-batches');
    if (ovTotalBatches) ovTotalBatches.textContent = totalCumulativeBatches + ' Total';

    // Top Bar Autonomy Status Derivation
    let autonomyStatus = 'UNKNOWN';
    if (anyHumanRequired) autonomyStatus = 'HUMAN_REQUIRED';
    else if (anyFailed) autonomyStatus = 'ATTENTION';
    else if (anyRunning) autonomyStatus = 'RUNNING';
    else if (anyWaiting) autonomyStatus = 'WAITING';
    else if (activeLanesCount === 0) autonomyStatus = 'IDLE';

    const topAutonomy = document.getElementById('top-autonomy-text');
    if (topAutonomy) {
      topAutonomy.textContent = autonomyStatus;
      topAutonomy.className = (autonomyStatus === 'RUNNING' ? 'ok' : (autonomyStatus.includes('WAIT') ? 'hold' : (autonomyStatus === 'HUMAN_REQUIRED' ? 'danger' : '')));
    }

    // 4. Reasoning Providers & Matrix
    const providerRows = s.provider_details || [];
    const providersContainer = document.getElementById('providers');
    let availableProvidersCount = 0;
    const totalProvidersCount = providerRows.length;

    if (totalProvidersCount === 0) {
      if (providersContainer) providersContainer.innerHTML = '<div style="color:var(--text-muted); padding:10px;">Detailed provider telemetry is currently unavailable. This does not mean zero providers are configured.</div>';

      const topProvText = document.getElementById('top-providers-text');
      if (topProvText) topProvText.textContent = 'UNKNOWN';

      const ovProvAvail = document.getElementById('ov-providers-avail');
      if (ovProvAvail) ovProvAvail.textContent = 'UNKNOWN';

      const railProvStatus = document.getElementById('rail-providers-status');
      if (railProvStatus) {
        railProvStatus.textContent = '?';
        railProvStatus.className = 'nav-badge alert';
      }
    } else {
      let ph = `
      <table class="cockpit-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Model ID / Role</th>
            <th>Billing Class</th>
            <th>Circuit State</th>
            <th>Probe Status</th>
            <th>Credential / Service</th>
            <th>Last Success</th>
            <th>Failure Class</th>
            <th>Next Probe</th>
            <th>Probes / Fails</th>
          </tr>
        </thead>
        <tbody>`;

      for (const row of providerRows) {
        const cState = row.circuit_state || 'UNKNOWN';
        const isClosed = (cState === 'CLOSED');
        if (isClosed) availableProvidersCount++;
        const cChip = isClosed ? 'ok' : (cState === 'UNKNOWN' ? 'hold' : 'danger');

        const billing = row.billing_class || 'FREE';
        const model = row.model_id || 'default';
        const probes = `${row.probe_count ?? 0} / ${row.failover_count ?? 0}`;
        const lastSuccess = row.last_success_at ? String(row.last_success_at).slice(0, 19).replace('T', ' ') : 'NONE';
        const nextProbe = row.next_probe_at ? String(row.next_probe_at).slice(0, 10) : 'NONE';
        const availability = row.credential_available === null || row.credential_available === undefined
          ? `Local: ${row.local_service_available === true ? 'YES' : 'NO'}`
          : `Credential: ${row.credential_available ? 'CONFIGURED' : 'MISSING'}`;

        const safePid = escapeHtml(row.provider_id);
        const safeModel = escapeHtml(model);
        const safeBilling = escapeHtml(billing);
        const safeProbeStatus = escapeHtml(row.probe_status || 'NOT_ATTEMPTED');
        const safeFailClass = escapeHtml(row.failure_class || 'NONE');

        ph += `
          <tr>
            <td><strong>${safePid}</strong></td>
            <td><code>${safeModel}</code></td>
            <td><span class="status-chip ${billing === 'PAID' ? 'hold' : 'neutral'}">${safeBilling}</span></td>
            <td><span class="status-chip ${cChip}">${escapeHtml(cState)}</span></td>
            <td>${safeProbeStatus}</td>
            <td><small>${escapeHtml(availability)}</small></td>
            <td><small>${escapeHtml(lastSuccess)}</small></td>
            <td><code>${safeFailClass}</code></td>
            <td><small>${escapeHtml(nextProbe)}</small></td>
            <td><code>${probes}</code></td>
          </tr>`;
      }
      ph += '</tbody></table>';
      if (providersContainer) providersContainer.innerHTML = ph;

      // Render Resource Operations & Task-Class Eligibility Matrix
      const resOpsContainer = document.getElementById('resource-operations-container');
      if (resOpsContainer) {
        const resRows = s.resource_operations_matrix || [];
        if (resRows.length === 0) {
          resOpsContainer.innerHTML = '<div style="color:var(--text-muted); padding:10px;">Resource operations matrix not yet loaded.</div>';
        } else {
          let rm = `
          <table class="cockpit-table">
            <thead>
              <tr>
                <th>Resource Name</th>
                <th>Type</th>
                <th>Executable / Runtime</th>
                <th>Version</th>
                <th>Auth Status</th>
                <th>Cost Class</th>
                <th>Health</th>
                <th>Lifecycle State</th>
                <th>Blocker / Retry</th>
                <th>Task-Class Eligibility</th>
              </tr>
            </thead>
            <tbody>`;
          for (const r of resRows) {
            const hState = r.general_health || 'UNKNOWN';
            const hChip = (hState === 'AVAILABLE') ? 'ok' : ((hState === 'QUOTA_EXHAUSTED' || hState.includes('REQUIRED')) ? 'hold' : 'neutral');
            const elig = r.eligibility_by_task_class || {};
            const eligList = Object.entries(elig).map(([k, v]) => `<span style="display:inline-block; margin-right:4px; font-size:10.5px; padding:2px 5px; border-radius:4px; background:${v ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)'}; color:${v ? '#4ade80' : '#f87171'}; border:1px solid ${v ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'};">${k}: ${v ? 'YES' : 'NO'}</span>`).join('');
            rm += `
              <tr>
                <td><strong>${escapeHtml(r.name)}</strong></td>
                <td><small style="color:var(--text-sub);">${escapeHtml(r.resource_type)}</small></td>
                <td><code>${escapeHtml(r.executable)}</code></td>
                <td><small>${escapeHtml(r.version)}</small></td>
                <td><span class="status-chip ${r.auth_status.includes('AUTH') || r.auth_status.includes('CONFIGURED') || r.auth_status.includes('SUBSCRIPTION') ? 'ok' : 'neutral'}">${escapeHtml(r.auth_status)}</span></td>
                <td><small>${escapeHtml(r.cost_class)}</small></td>
                <td><span class="status-chip ${hChip}">${escapeHtml(hState)}</span></td>
                <td><code>${escapeHtml(r.lifecycle_state)}</code></td>
                <td><small style="color:${r.current_blocker === 'NONE' ? 'var(--text-muted)' : '#fca5a5'};">${escapeHtml(r.current_blocker || 'NONE')}</small></td>
                <td>${eligList}</td>
              </tr>`;
          }
          rm += '</tbody></table>';
          resOpsContainer.innerHTML = rm;
        }
      }

      const topProvText = document.getElementById('top-providers-text');
      if (topProvText) topProvText.textContent = `${availableProvidersCount} / ${totalProvidersCount} Avail`;

      const ovProvAvail = document.getElementById('ov-providers-avail');
      if (ovProvAvail) ovProvAvail.textContent = `${availableProvidersCount} / ${totalProvidersCount} Avail`;

      const railProvStatus = document.getElementById('rail-providers-status');
      if (railProvStatus) {
        railProvStatus.textContent = `${availableProvidersCount}/${totalProvidersCount}`;
        railProvStatus.className = 'nav-badge ' + (availableProvidersCount > 0 ? 'ok' : 'alert');
      }

      const provHeaderChip = document.getElementById('prov-header-chip');
      if (provHeaderChip) {
        provHeaderChip.textContent = availableProvidersCount > 0 ? 'Circuit Monitored' : 'All Circuits Open / Backoff';
        provHeaderChip.className = 'status-chip ' + (availableProvidersCount > 0 ? 'ok' : 'hold');
      }

      // Windows Credential Manager dynamic inputs
      const gridEl = document.getElementById('providers-dynamic-grid');
      if (gridEl) {
        const regProviders = s.sanitized_providers || [];
        let gHtml = '';
        for (const cp of regProviders) {
          if (!cp.credential_configurable) continue;
          const pid = cp.provider_id;
          const isConfigured = Boolean(cp.configured);
          const linkHtml = cp.provider_console_url ? ` · <a href="${escapeHtml(cp.provider_console_url)}" target="_blank" rel="noreferrer" style="color:var(--cyan); text-decoration:none;">Console Keys ↗</a>` : '';
          gHtml += `
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-dim); border-radius:8px; padding:14px;">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
              <strong style="color:#fff; font-size:13.5px;">${escapeHtml(cp.display_name || pid)}</strong>
              <span class="status-chip ${isConfigured ? 'ok' : 'neutral'}">${isConfigured ? 'VAULT READY' : 'UNCONFIGURED'}</span>
            </div>
            <div style="font-size:11px; color:var(--text-muted); margin-bottom:8px;">
              Env: <code>${escapeHtml(cp.credential_env_var)}</code> · Class: ${escapeHtml(cp.billing_class)}${linkHtml}
            </div>
            <input id="key-${escapeHtml(pid)}" type="password" autocomplete="off" placeholder="Paste ${escapeHtml(pid)} API key…">
            <div style="display:flex; gap:6px; margin-top:8px;">
              <button class="btn btn-secondary btn-sm" onclick="saveProvider('${escapeHtml(pid)}', '${escapeHtml(cp.credential_env_var || '')}')">
                Save in Windows Vault
              </button>
              <button class="btn btn-danger btn-sm" onclick="clearProvider('${escapeHtml(pid)}', '${escapeHtml(cp.credential_env_var || '')}')">
                Clear
              </button>
            </div>
          </div>`;
        }
        gridEl.innerHTML = gHtml;
      }
    }

    // 5. OPERATOR ATTENTION VS SYSTEM ATTENTION
    const alerts = s.alerts || [];
    const attentionBox = document.getElementById('overview-operator-attention');
    const attentionHead = document.getElementById('attention-headline');
    const attentionSub = document.getElementById('attention-subtext');
    const railAttention = document.getElementById('rail-attention-badge');

    // Populate Detailed Active Alerts Panel
    const alertsBadge = document.getElementById('alerts-count-badge');
    if (alertsBadge) alertsBadge.textContent = String(alerts.length);
    const alertsView = document.getElementById('alerts-view');
    if (alertsView) {
      if (alerts.length === 0) {
        alertsView.innerHTML = '<span style="color:var(--text-muted);">No active findings or alerts. System telemetry and provider circuits are operating nominally.</span>';
      } else {
        alertsView.innerHTML = alerts.map(a => `<div style="color:#f0b66c; margin-bottom:6px;">⚠ ${escapeHtml(a)}</div>`).join('');
      }
    }

    // Distinguish Operator Attention from System Wait / Attention
    if (anyHumanRequired || isFail) {
      if (attentionBox) attentionBox.className = 'attention-box critical';
      if (railAttention) {
        railAttention.textContent = 'ACTION';
        railAttention.className = 'nav-badge alert';
      }
      if (attentionHead) attentionHead.textContent = 'Operator Action Required';
      if (attentionSub) attentionSub.textContent = anyHumanRequired
        ? 'A lane has entered HUMAN_REQUIRED state. Operator review or approval is required.'
        : `Host system entered ${hostVal} state. Immediate inspection recommended.`;
    } else if (anyWaiting || activeBlockerList.length > 0 || isHold || alerts.length > 0) {
      if (attentionBox) attentionBox.className = 'attention-box system-wait';
      if (railAttention) {
        railAttention.textContent = 'WAIT';
        railAttention.className = 'nav-badge alert';
      }
      if (anyWaiting && activeBlockerList.some(b => b.includes('REASONING_PROVIDER'))) {
        if (attentionHead) attentionHead.textContent = 'System Attention · Autonomous Provider Wait';
        if (attentionSub) attentionSub.textContent = 'Reasoning providers are temporarily unavailable or rate limited. Autonomous backoff and circuit probe loops are active.';
      } else if (activeBlockerList.length > 0) {
        if (attentionHead) attentionHead.textContent = `System Attention · ${escapeHtml(activeBlockerList[0])}`;
        if (attentionSub) attentionSub.textContent = 'Lane execution holding pending autonomous resolution. No manual human action requested unless persistent.';
      } else {
        if (attentionHead) attentionHead.textContent = `System Telemetry Notice (${alerts.length} alert${alerts.length === 1 ? '' : 's'})`;
        if (attentionSub) attentionSub.textContent = 'System alerts present. Inspect active alert details below.';
      }
    } else {
      if (attentionBox) attentionBox.className = 'attention-box nominal';
      if (railAttention) {
        railAttention.textContent = 'OK';
        railAttention.className = 'nav-badge ok';
      }
      if (attentionHead) attentionHead.textContent = 'All Systems Nominal · No Operator Intervention Required';
      if (attentionSub) attentionSub.textContent = `AOS is actively executing under standing authority. ${activeLanesCount} lane(s) registered.`;
    }

    // Deliberation Shadow Metrics (Preserve all telemetry)
    const delib = s.deliberation || {};
    const totalSamples = delib.total_samples || 0;
    const delibView = document.getElementById('deliberation-view');
    if (delibView) {
      const reasons = delib.trigger_reasons || {};
      const reasonEntries = Object.entries(reasons).map(([k, v]) => `<span style="background:#1e2637; border-radius:4px; padding:2px 6px; margin-right:6px; font-family:var(--font-mono); font-size:11px;">${escapeHtml(k)}: <strong>${v}</strong></span>`).join(' ');
      const sharePct = ((delib.COUNCIL_REASONING_SHARE_ESTIMATE ?? 0) * 100).toFixed(1);
      delibView.innerHTML = `
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:8px; margin-bottom:12px;">
          <div class="history-metric-box">
            <div class="label">Shadow Samples</div>
            <div class="val ok">${totalSamples}</div>
          </div>
          <div class="history-metric-box">
            <div class="label">Quorum Count</div>
            <div class="val">${delib.quorum_count || 0}</div>
          </div>
          <div class="history-metric-box">
            <div class="label">Agreement Count</div>
            <div class="val">${delib.agreement_count || 0}</div>
          </div>
          <div class="history-metric-box">
            <div class="label">Reasoning Share</div>
            <div class="val">${sharePct}%</div>
          </div>
        </div>
        <div style="font-size:12px; color:var(--text-sub); display:flex; flex-direction:column; gap:4px;">
          <div>Primary Provider Calls: <strong>${delib.PRIMARY_PROVIDER_CALL_COUNT ?? 0}</strong> · Tokens: <strong>${delib.PRIMARY_PROVIDER_TOKEN_ESTIMATE ?? 0}</strong></div>
          <div>Council Provider Calls: <strong>${delib.COUNCIL_PROVIDER_CALL_COUNT ?? 0}</strong> · Tokens: <strong>${delib.COUNCIL_PROVIDER_TOKEN_ESTIMATE ?? 0}</strong></div>
        </div>
        <div style="margin-top:8px; color:var(--text-muted); font-size:11.5px;">Trigger Reasons: ${reasonEntries || 'None recorded'}</div>`;
    }

    // Self-Repair Telemetry & Finding Drill-Down
    const sr = s.self_repair || {};
    const srView = document.getElementById('self-repair-view');
    if (srView) {
      let srHtml = `
        <div style="display:flex; flex-direction:column; gap:8px; font-size:12.5px;">
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:6px;">
            <span style="color:var(--text-muted)">Diagnosis Status:</span>
            <strong>${escapeHtml(sr.self_diagnosis_status || 'HEALTHY_NO_ACTION')}</strong>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:6px;">
            <span style="color:var(--text-muted)">Active Findings:</span>
            <span>${sr.active_finding_count ?? 0} (Blocking: <strong style="color:${(sr.blocking_finding_count || 0) > 0 ? '#e06c75' : '#74d99f'}">${sr.blocking_finding_count ?? 0}</strong>)</span>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:6px;">
            <span style="color:var(--text-muted)">Last Finding:</span>
            <span>${escapeHtml(sr.self_repair_last_finding || 'NONE')} (Class: <code>${escapeHtml(sr.last_failure_class || 'NONE')}</code>)</span>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:6px;">
            <span style="color:var(--text-muted)">Severity / Autonomy Impact:</span>
            <span>${escapeHtml(sr.last_finding_severity || 'NONE')} / ${escapeHtml(sr.last_autonomy_impact || 'NONE')}</span>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:6px;">
            <span style="color:var(--text-muted)">Shadow Proposal Status:</span>
            <span>${escapeHtml(sr.shadow_repair_proposal_status || 'NONE')} (Live Active: NO)</span>
          </div>
          <div style="display:flex; justify-content:space-between;">
            <span style="color:var(--text-muted)">Eligibility Gate:</span>
            <span class="status-chip neutral">${escapeHtml(sr.self_repair_eligibility || 'PENDING_GATE')}</span>
          </div>
        </div>`;

      const findingsList = sr.findings || [];
      if (findingsList.length > 0) {
        srHtml += `<div style="margin-top:10px; border-top:1px solid var(--border-dim); padding-top:8px;">
          <div style="font-weight:bold; margin-bottom:6px; color:var(--text-sub);">Recent Shadow Findings (Read-Only Inspection):</div>`;
        for (const f of findingsList.slice(0, 5)) {
          const sevColor = (f.severity === 'CRITICAL' || f.severity === 'HIGH') ? '#ef4444' : ((f.severity === 'MEDIUM') ? '#f59e0b' : '#10b981');
          srHtml += `<details style="margin-bottom:6px; background:var(--bg-card-subtle); border:1px solid var(--border-dim); border-radius:6px; padding:6px 10px;">
            <summary style="cursor:pointer; font-size:12px;">
              <strong style="color:${sevColor}">[${escapeHtml(f.severity)}]</strong> <code>${escapeHtml(f.failure_class)}</code>: ${escapeHtml((f.symptom || '').slice(0, 60))}…
            </summary>
            <div style="margin-top:6px; font-size:11px; font-family:var(--font-mono); line-height:1.5;">
              <div>Finding ID: <strong>${escapeHtml(f.finding_id)}</strong></div>
              <div>Component: ${escapeHtml(f.component)} · Recurrence: ${f.recurrence_count}</div>
              <div>Impact: ${escapeHtml(f.autonomy_impact)} · Authority: ${escapeHtml(f.repair_authority)}</div>
              <div>Root Cause: ${escapeHtml(f.suspected_root_cause || 'NONE')}</div>
              ${f.proposed_repair ? `<div style="margin-top:4px; color:var(--ok);">Proposed Repair (Shadow Only): ${escapeHtml(f.proposed_repair.minimal_change || 'NONE')}</div>` : ''}
            </div>
          </details>`;
        }
        srHtml += `</div>`;
      }
      srView.innerHTML = srHtml;
    }

    // Controller Relay
    const relay = s.relay || {};
    const relayView = document.getElementById('relay-view');
    if (relayView) {
      if (!relay.writer) {
        relayView.innerHTML = '<div style="color:var(--text-muted); padding:10px;"><em>No native controller relay data loaded yet.</em></div>';
      } else {
        const outboxStatus = relay.remote_outbox_status || 'DISABLED';
        const outboxClass = outboxStatus === 'PUBLISHED' ? 'ok' : (outboxStatus.includes('DEGRADED') ? 'hold' : 'neutral');
        const lastPub = relay.last_remote_publish_at ? String(relay.last_remote_publish_at).slice(11, 19) + 'Z' : 'NONE';
        relayView.innerHTML = `
          <div style="display:flex; flex-direction:column; gap:6px; font-size:12.5px;">
            <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
              <span style="color:var(--text-muted)">Writer Entity:</span>
              <strong>${escapeHtml(relay.writer || 'AOS')} (Seq #${relay.sequence_number ?? 0})</strong>
            </div>
            <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
              <span style="color:var(--text-muted)">Heartbeat / Forward Progress:</span>
              <span><strong class="${relay.aos_heartbeat === 'ALIVE' ? 'ok' : 'danger'}">${escapeHtml(relay.aos_heartbeat || 'ALIVE')}</strong> · ${escapeHtml(relay.forward_progress || 'YES')}</span>
            </div>
            <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
              <span style="color:var(--text-muted)">Remote Outbox Status:</span>
              <span class="status-chip ${outboxClass}">${escapeHtml(outboxStatus)}</span>
            </div>
            <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
              <span style="color:var(--text-muted)">Remote Issue #:</span>
              <strong>${relay.remote_issue_number ? '#' + escapeHtml(relay.remote_issue_number) : 'NONE'}</strong>
            </div>
            <div style="display:flex; justify-content:space-between;">
              <span style="color:var(--text-muted)">Last Remote Publish:</span>
              <small>${escapeHtml(lastPub)}</small>
            </div>
          </div>`;
      }
    }

    // Product Evidence
    const pe = s.product_evidence || {};
    const prodView = document.getElementById('product-view');
    if (prodView) {
      const mDelta = pe.meaningful_batch_delta ?? 0;
      const safeArt = escapeHtml(pe.first_workspace_productization_artifact || 'NONE');
      const safeMut = escapeHtml(pe.first_user_facing_ui_mutation || 'NONE');
      const safeSuite = escapeHtml(pe.browser_evidence_status || 'AWAITING_BROWSER_SUITE_RUN');
      const safeResp = escapeHtml(pe.responsive_evidence_status || 'AWAITING_BROWSER_SUITE_RUN');
      const safeCiState = escapeHtml(pe.tests_ci_state ? JSON.stringify(pe.tests_ci_state) : '—');
      prodView.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:6px; font-size:12.5px;">
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
            <span style="color:var(--text-muted)">Meaningful Batch Delta:</span>
            <span class="status-chip ${mDelta > 0 ? 'ok' : 'neutral'}">+${mDelta}</span>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
            <span style="color:var(--text-muted)">Workspace Artifact:</span>
            <small><code>${safeArt}</code></small>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
            <span style="color:var(--text-muted)">First UI Mutation:</span>
            <small><code>${safeMut}</code></small>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
            <span style="color:var(--text-muted)">Browser Suite Status:</span>
            <span class="status-chip neutral">${safeSuite}</span>
          </div>
          <div style="display:flex; justify-content:space-between; border-bottom:1px solid var(--border-dim); padding-bottom:4px;">
            <span style="color:var(--text-muted)">Responsive Suite Status:</span>
            <span class="status-chip neutral">${safeResp}</span>
          </div>
          <div style="display:flex; justify-content:space-between;">
            <span style="color:var(--text-muted)">Tests / CI State:</span>
            <small><code>${safeCiState}</code></small>
          </div>
        </div>`;
    }

    GOAL_PROJECTS = s.projects || {};
    const selector = document.getElementById('goal-project');
    if (selector) {
      const previous = selector.value;
      selector.innerHTML = Object.keys(GOAL_PROJECTS).map(projectId =>
        `<option value="${escapeHtml(projectId)}">${escapeHtml(projectId)}</option>`
      ).join('');
      const preferred = previous && GOAL_PROJECTS[previous] ? previous : s.default_project_id;
      if (preferred && GOAL_PROJECTS[preferred]) selector.value = preferred;
      syncGoalProjectProfile();
    }

  } catch (e) {
    if (document.getElementById('top-host-text')) document.getElementById('top-host-text').textContent = 'DISCONNECTED';
    if (document.getElementById('top-host-dot')) document.getElementById('top-host-dot').className = 'dot hold';
    if (document.getElementById('host')) document.getElementById('host').textContent = 'DISCONNECTED';
  }
}

async function runOpCommand(cmd) {
  const out = document.getElementById('op-command-msg');
  if (out) out.textContent = 'Executing command [' + cmd + ']…';
  let payload = {};
  if (cmd === 'restart-worker') {
    const cid = prompt('Enter command_id to restart worker:');
    if (!cid) {
      if (out) out.textContent = 'restart-worker cancelled by operator.';
      return;
    }
    payload.command_id = cid.trim();
  }
  try {
    const r = await fetch('/api/commands/' + encodeURIComponent(cmd), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-AOS-Panel-Token': TOKEN},
      body: JSON.stringify(payload)
    });
    const res = await r.json();
    if (out) out.textContent = JSON.stringify(res, null, 2);
    toast('Command [' + cmd + '] dispatched');
    await refreshStatus();
  } catch (e) {
    if (out) out.textContent = 'Command ' + cmd + ' failed: ' + e;
    toast('Command failed');
  }
}

async function saveProvider(provider, envVar) {
  const input = document.getElementById('key-' + provider);
  const out = document.getElementById('provider-message');
  if (out) out.style.display = 'block';
  const secret = input ? input.value.trim() : '';
  if (!secret) {
    if (out) out.textContent = 'Enter a valid API key for ' + provider;
    toast('Missing API key');
    return;
  }
  try {
    const r = await fetch('/api/providers', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify({provider, secret, action:'save', credential_env_var: envVar})
    });
    const data = await r.json();
    if (input) input.value = '';
    if (out) out.textContent = data.ready ? provider + ' saved securely in Windows Credential Manager and is ready.' : JSON.stringify(data, null, 2);
    toast(provider + ' key saved in Windows Vault');
    await refreshStatus();
  } catch (e) {
    if (out) out.textContent = 'Provider save failed: ' + e;
  }
}

async function clearProvider(provider, envVar) {
  const out = document.getElementById('provider-message');
  if (out) out.style.display = 'block';
  try {
    const r = await fetch('/api/providers', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify({provider, action:'delete', credential_env_var: envVar})
    });
    const data = await r.json();
    if (out) out.textContent = provider + (data.deleted ? ' removed from Windows Credential Manager.' : ' had no stored credential.');
    toast(provider + ' credential cleared');
    await refreshStatus();
  } catch (e) {
    if (out) out.textContent = 'Provider clear failed: ' + e;
  }
}

async function submitGoal() {
  const out = document.getElementById('goal-message');
  if (out) out.style.display = 'block';
  const lines = id => {
    const el = document.getElementById(id);
    return el ? el.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean) : [];
  };
  const goalEl = document.getElementById('goal-text');
  const projectEl = document.getElementById('goal-project');
  const payload = {
    project_id: projectEl ? projectEl.value : '',
    goal: goalEl ? goalEl.value.trim() : '',
    constraints: lines('goal-constraints'),
    red_lines: lines('goal-redlines'),
    max_batches: Number((document.getElementById('goal-batches') || {}).value || 12)
  };
  if (!payload.project_id) {
    if (out) out.textContent = 'Error: Select a configured Runtime V1 project.';
    toast('Configured project required');
    return;
  }
  if (!payload.goal) {
    if (out) out.textContent = 'Error: Primary Goal statement is required.';
    toast('Goal statement required');
    return;
  }
  try {
    if (out) out.textContent = 'Submitting autonomous project goal…';
    const r = await fetch('/api/goals', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify(payload)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.message || data.error || ('HTTP ' + r.status));
    if (!data.accepted) throw new Error('Runtime did not accept the autonomous goal');
    if (out) out.textContent = JSON.stringify(data, null, 2);
    toast('Autonomous Goal Accepted');
    await refreshStatus();
  } catch (e) {
    if (out) out.textContent = 'Goal submission failed: ' + e;
    toast('Goal submission failed');
  }
}

async function submitJob() {
  const out = document.getElementById('message');
  if (out) out.style.display = 'block';
  let payload;
  const jobEl = document.getElementById('job');
  try {
    payload = JSON.parse(jobEl ? jobEl.value : '{}');
  } catch (e) {
    if (out) out.textContent = 'Invalid JSON in job envelope: ' + e;
    toast('Invalid JSON');
    return;
  }
  try {
    if (out) out.textContent = 'Submitting manual run-plan override…';
    const r = await fetch('/api/jobs', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify(payload)
    });
    const data = await r.json();
    if (out) out.textContent = JSON.stringify(data, null, 2);
    toast('Manual Job Submitted');
    await refreshStatus();
  } catch (e) {
    if (out) out.textContent = 'Submit failed: ' + e;
    toast('Submit failed');
  }
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


def _command_recency_key(
    command_id: str,
    state: Dict[str, Any],
    command: Dict[str, Any],
    *,
    active_or_waiting: set[str],
    latest_command_id: Optional[str],
) -> tuple[int, float, str]:
    """Order command projections by live authority, then durable recency.

    Command identifiers are random and must never determine which project
    lineage the cockpit presents. Runtime-reported active/waiting commands are
    authoritative, followed by the runtime's latest command, then the newest
    durable state for projects without a live command.
    """

    live_priority = 2 if command_id in active_or_waiting else 0
    if command_id == latest_command_id:
        live_priority = max(live_priority, 1)

    raw_timestamp = state.get("updated_at") or command.get("created_at") or ""
    try:
        normalized = str(raw_timestamp).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        timestamp = float("-inf")

    return live_priority, timestamp, command_id


def _provider_presence() -> Dict[str, bool]:
    result = provider_presence()
    result["OLLAMA"] = False
    return result


def get_resource_operations_matrix(runtime_v1: Optional[Dict[str, Any]] = None) -> list[Dict[str, Any]]:
    """Build comprehensive operational telemetry for all agentic and reasoning resources.
    
    Exposes: name, resource type, executable/runtime, version, auth status, cost class,
    general health, task classes, lifecycle state, blocker/retry info, and task-class eligibility.
    """
    matrix: list[Dict[str, Any]] = []
    runtime_v1 = runtime_v1 or {}
    prov_details = {str(p.get("provider_id")): p for p in runtime_v1.get("provider_details", [])}

    # 1. Antigravity CLI
    ag_attestation_file = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "capabilities" / "antigravity.json"
    ag_attested = False
    ag_ver = "unknown"
    ag_exe = "antigravity.exe"
    if ag_attestation_file.is_file():
        try:
            ag_data = json.loads(ag_attestation_file.read_text("utf-8"))
            ag_attested = ag_data.get("capability_status") == "PROVEN"
            ag_ver = ag_data.get("reported_cli_version", "1.2.10")
            ag_exe = ag_data.get("executable_filename", "antigravity.exe")
        except Exception:
            pass

    matrix.append({
        "name": "Antigravity",
        "resource_type": "FIRST_CLASS_AGENTIC",
        "executable": ag_exe,
        "version": ag_ver,
        "auth_status": "AUTHENTICATED",
        "cost_class": "SUBSCRIPTION_INCLUDED",
        "general_health": "AVAILABLE" if ag_attested else "UNPROVEN",
        "task_classes": ["agentic_coding", "repo_ui_planning", "structured_planning", "file_edit", "process_exec"],
        "lifecycle_state": "ACTIVE_PRIMARY_AGENT",
        "quota_status": "NOMINAL",
        "retry_deadline": None,
        "current_blocker": "NONE" if ag_attested else "ATTESTATION_REQUIRED",
        "eligibility_by_task_class": {
            "structured_planning": True,
            "repo_ui_planning": True,
            "agentic_coding": True,
            "verification": True,
        },
    })

    # 2. Codex CLI
    codex_attestation_file = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "capabilities" / "codex-cli.json"
    codex_attested = False
    codex_ver = "unknown"
    codex_exe = "codex.exe"
    if codex_attestation_file.is_file():
        try:
            cdx_data = json.loads(codex_attestation_file.read_text("utf-8"))
            codex_attested = cdx_data.get("capability_status") == "PROVEN"
            codex_ver = cdx_data.get("reported_cli_version", "0.146.0")
            codex_exe = cdx_data.get("executable_filename", "codex.exe")
        except Exception:
            pass

    matrix.append({
        "name": "Codex CLI",
        "resource_type": "FIRST_CLASS_AGENTIC",
        "executable": codex_exe,
        "version": codex_ver,
        "auth_status": "CHATGPT_SUBSCRIPTION",
        "cost_class": "SUBSCRIPTION_INCLUDED",
        "general_health": "QUOTA_EXHAUSTED",
        "task_classes": ["agentic_coding", "repo_ui_planning", "structured_planning"],
        "lifecycle_state": "PRESERVED_STANDBY",
        "quota_status": "TEMPORARILY_QUOTA_UNAVAILABLE",
        "retry_deadline": "WAITING_FOR_QUOTA_RESET",
        "current_blocker": "QUOTA_EXHAUSTED",
        "eligibility_by_task_class": {
            "structured_planning": False,
            "repo_ui_planning": False,
            "agentic_coding": False,
            "verification": False,
        },
    })

    # 3. Cline CLI
    matrix.append({
        "name": "Cline",
        "resource_type": "AGENTIC_CLI_HARNESS",
        "executable": "cline",
        "version": "NOT_INSTALLED",
        "auth_status": "NOT_CONFIGURED",
        "cost_class": "FREE_HARNESS",
        "general_health": "NOT_INSTALLED",
        "task_classes": ["agentic_coding", "file_edit"],
        "lifecycle_state": "PREREQUISITE_EVALUATED",
        "quota_status": "N/A",
        "retry_deadline": None,
        "current_blocker": "NODE_JS_RUNTIME_PREREQUISITE",
        "eligibility_by_task_class": {
            "structured_planning": False,
            "repo_ui_planning": False,
            "agentic_coding": False,
            "verification": False,
        },
    })

    # 4. Qwen (llama.cpp Local)
    qwen_cap_file = Path(os.environ.get("LOCALAPPDATA", "")) / "AOS" / "capabilities" / "llama-cpp-qwen3-4b.json"
    qwen_proven = False
    if qwen_cap_file.is_file():
        try:
            q_data = json.loads(qwen_cap_file.read_text("utf-8"))
            qwen_proven = q_data.get("capability_status") == "PROVEN"
        except Exception:
            pass

    qwen_lifecycle = "STOPPED_READY"
    qwen_blocker = "NONE" if qwen_proven else "MODEL_BENCHMARK_REQUIRED"
    try:
        from aos.workers.llama_cpp_lifecycle import get_qwen_lifecycle_manager
        mgr = get_qwen_lifecycle_manager()
        snap = mgr.get_snapshot()
        qwen_lifecycle = snap.state.value
        if snap.error_message:
            qwen_blocker = snap.error_message
    except Exception:
        pass

    matrix.append({
        "name": "Qwen Local",
        "resource_type": "LOCAL_INFERENCE_REASONING",
        "executable": "llama-server.exe",
        "version": "Qwen3-4B-Q4_K_M (b11149)",
        "auth_status": "LOOPBACK_LOCAL_NO_AUTH",
        "cost_class": "FREE_LOCAL",
        "general_health": "AVAILABLE" if qwen_proven else "UNPROVEN",
        "task_classes": ["structured_planning", "classification", "bounded_reasoning"],
        "lifecycle_state": qwen_lifecycle,
        "quota_status": "UNLIMITED_LOCAL",
        "retry_deadline": None,
        "current_blocker": qwen_blocker,
        "eligibility_by_task_class": {
            "structured_planning": qwen_proven,
            "repo_ui_planning": False,
            "agentic_coding": False,
            "verification": qwen_proven,
        },
    })

    # 5. Cloud Free Providers (Nemotron, Groq, Gemini, Cloudflare, Cerebras, OpenRouter, HF, FreeLLMAPI, Jev)
    presence = provider_presence()

    def _prov_row(p_id: str, display: str, model_id: str, key_env: str, b_class: str, default_health: str, default_blocker: str):
        detail = prov_details.get(p_id, {})
        c_state = detail.get("circuit_state", "CIRCUIT_CLOSED" if presence.get(key_env) else "NOT_CONFIGURED")
        fail_cls = detail.get("failure_class")
        is_conf = bool(presence.get(key_env))
        blocker = fail_cls if fail_cls else (default_blocker if not is_conf else "NONE")
        g_health = detail.get("probe_status", default_health if is_conf else "AUTH_REQUIRED")
        return {
            "name": display,
            "resource_type": "CLOUD_REASONING_PROVIDER",
            "executable": "HTTP_API",
            "version": model_id,
            "auth_status": "CONFIGURED" if is_conf else "MISSING_KEY",
            "cost_class": b_class,
            "general_health": g_health,
            "task_classes": ["structured_planning"],
            "lifecycle_state": c_state,
            "quota_status": detail.get("failure_class", "NOMINAL"),
            "retry_deadline": detail.get("next_probe_at"),
            "current_blocker": blocker,
            "eligibility_by_task_class": {"structured_planning": is_conf and c_state != "OPEN", "repo_ui_planning": False},
        }

    matrix.extend([
        _prov_row("nemotron", "Nemotron", "nvidia/nemotron-3-ultra-550b", "NVIDIA", "FREE_TIER", "AVAILABLE", "NVIDIA_API_KEY_REQUIRED"),
        _prov_row("groq", "Groq", "openai/gpt-oss-120b", "GROQ", "FREE_TIER", "AVAILABLE", "GROQ_API_KEY_REQUIRED"),
        _prov_row("gemini", "Gemini", "gemini-3.6-flash", "GEMINI", "FREE_TIER", "RATE_LIMITED", "GEMINI_API_KEY_REQUIRED"),
        _prov_row("cloudflare", "Cloudflare", "@cf/meta/llama-3.3-70b", "CLOUDFLARE", "FREE_DAILY_QUOTA", "RATE_LIMITED", "CLOUDFLARE_API_TOKEN_REQUIRED"),
        _prov_row("cerebras", "Cerebras", "gpt-oss-120b", "CEREBRAS", "FREE_TRIAL", "CREDIT_EXHAUSTED", "CEREBRAS_API_KEY_REQUIRED"),
        _prov_row("openrouter_free", "OpenRouter Free", "openrouter/free", "OPENROUTER", "FREE", "AVAILABLE", "OPENROUTER_API_KEY_REQUIRED"),
        _prov_row("huggingface_router", "Hugging Face", "openai/gpt-oss-120b:fastest", "HUGGINGFACE", "FREE", "CREDIT_EXHAUSTED", "HF_TOKEN_REQUIRED"),
        {
            "name": "FreeLLMAPI",
            "resource_type": "LOCAL_META_GATEWAY",
            "executable": "node server/dist/index.js",
            "version": "commit 15c30081",
            "auth_status": "SOURCE_PINNED_NOT_BUILT",
            "cost_class": "FREE_LOCAL_BRIDGE",
            "general_health": "NOT_INSTALLED",
            "task_classes": ["structured_planning"],
            "lifecycle_state": "SOURCE_CHECKOUT_ONLY",
            "quota_status": "N/A",
            "retry_deadline": None,
            "current_blocker": "NODE_BUILD_REQUIRED",
            "eligibility_by_task_class": {"structured_planning": False, "repo_ui_planning": False},
        },
        {
            "name": "Jev",
            "resource_type": "ADVISORY_ONLY",
            "executable": "NONE",
            "version": "N/A",
            "auth_status": "NON_AUTHORITATIVE",
            "cost_class": "OPTIONAL_ADVISORY",
            "general_health": "DISABLED",
            "task_classes": [],
            "lifecycle_state": "DISABLED",
            "quota_status": "N/A",
            "retry_deadline": None,
            "current_blocker": "ZERO_COST_ENTITLEMENT_UNPROVEN",
            "eligibility_by_task_class": {"structured_planning": False, "repo_ui_planning": False},
        },
    ])
    return matrix


def _get_sanitized_providers(config: Optional[Dict[str, Any]], providers: Dict[str, Any]) -> list[Dict[str, Any]]:
    policy_path = None
    if isinstance(config, dict):
        def_proj = config.get("default_project", {})
        if isinstance(def_proj, dict) and def_proj.get("routing_policy_path"):
            p = Path(def_proj["routing_policy_path"])
            if p.is_file():
                policy_path = p
    if not policy_path:
        for candidate in [
            Path("descriptors/nemotron.planner-policy.json"),
            Path("C:/Projects/AOS/descriptors/nemotron.planner-policy.json"),
            Path("C:/Projects/AOS-lane-b/descriptors/nemotron.planner-policy.json"),
        ]:
            if candidate.is_file():
                policy_path = candidate
                break

    sanitized = []
    if policy_path and policy_path.is_file():
        try:
            from aos.provider_registry import load_routing_policy
            reg = load_routing_policy(str(policy_path))
            for entry in reg.list_providers():
                is_configured = credential_is_configured(
                    entry.provider_id,
                    entry.credential_env_var,
                    presence=providers,
                )
                paid_provider = entry.billing_class == "PAID"
                sanitized.append({
                    "provider_id": entry.provider_id,
                    "display_name": entry.display_name or entry.provider_id,
                    "credential_env_var": entry.credential_env_var,
                    "billing_class": entry.billing_class,
                    "cloud_local": entry.cloud_local,
                    "enabled": bool(entry.enabled and not paid_provider),
                    "credential_configurable": bool(entry.credential_env_var),
                    "provider_console_url": entry.provider_console_url,
                    "configured": is_configured,
                })
        except Exception:
            pass
    return sanitized


def _command_work(command_root: Path, state: Dict[str, Any], command: Dict[str, Any]) -> Dict[str, Any]:
    runtime = command_root / "project-runtime"
    checkpoint = _read_json(runtime / "planning-kernel-checkpoint.json", {})
    batch_number = int(checkpoint.get("batch_number", state.get("completed_batch_count", 0)) or 0)
    objective = checkpoint.get("objective") if isinstance(checkpoint.get("objective"), dict) else {}
    objective_files = sorted(runtime.glob("objective-*.json"))
    if not objective and objective_files:
        objective = _read_json(objective_files[-1], {})
    situation_files = sorted(runtime.glob("situation-*.json"))
    situation = _read_json(situation_files[-1], {}) if situation_files else {}
    plan = _read_json(runtime / "batches" / f"batch-{batch_number:04d}" / "generated-run-plan.json", {})
    tasks = plan.get("tasks", []) if isinstance(plan.get("tasks"), list) else []
    last_receipt = checkpoint.get("last_receipt", {}) if isinstance(checkpoint.get("last_receipt"), dict) else {}
    completed_ids = [str(item) for item in last_receipt.get("completed_task_ids", [])]
    current_task = next(
        (str(task.get("node_id")) for task in tasks if str(task.get("node_id")) not in completed_ids),
        None,
    )
    completed_batches = checkpoint.get("completed_batches", []) if isinstance(checkpoint.get("completed_batches"), list) else []
    progress_timestamps = [
        str((item.get("receipt") or {}).get("timestamp"))
        for item in completed_batches
        if isinstance(item, dict) and isinstance(item.get("receipt"), dict) and (item.get("receipt") or {}).get("timestamp")
    ]

    total_executed_int = cumulative_completed_batch_count(checkpoint, state)

    successful_batches = checkpoint.get("successful_batch_count")
    successful_batches_int = int(successful_batches) if successful_batches is not None else total_executed_int

    failed_batches = checkpoint.get("failed_batch_count")
    failed_batches_int = int(failed_batches) if failed_batches is not None else 0

    recent_window_size = len(checkpoint.get("recent_completed_batches", completed_batches) or [])

    return {
        "current_objective": objective.get("title") or objective.get("objective_id") or command.get("goal"),
        "current_planning_phase": checkpoint.get("phase") or state.get("state"),
        "current_batch": batch_number,
        "planning_batch_number": batch_number,
        "total_executed_batches": total_executed_int,
        "successful_batches": successful_batches_int,
        "failed_batches": failed_batches_int,
        "recent_window_size": recent_window_size,
        "current_task": current_task,
        "most_recent_completed_task": completed_ids[-1] if completed_ids else None,
        "canonical_next_action": situation.get("canonical_next_action"),
        "last_meaningful_progress_at": max(progress_timestamps) if progress_timestamps else None,
        "current_blocker": checkpoint.get("reason") if str(state.get("state", "")).startswith("WAITING") else state.get("failure_class"),
        "last_accepted_milestone": completed_ids[-1] if completed_ids else None,
        "tests_ci_state": situation.get("ci_state"),
    }


def configure_provider(payload: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(payload.get("provider", "")).strip().upper()
    env_var = payload.get("credential_env_var") or payload.get("env_var")
    if env_var:
        env_var = str(env_var).strip()
    action = str(payload.get("action", "save")).strip().lower()
    if action == "save":
        secret = payload.get("secret")
        if not isinstance(secret, str):
            raise ValueError("Provider secret is required")
        if env_var:
            write_provider_secret(provider, secret, env_var=env_var)
        else:
            write_provider_secret(provider, secret)
        return {
            "schema_version": "1.0.0",
            "provider": provider,
            "ready": True,
            "stored": "WINDOWS_CREDENTIAL_MANAGER",
            "secret_returned": False,
        }
    if action == "delete":
        if env_var:
            deleted = delete_provider_secret(provider, env_var=env_var)
        else:
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
    projects = configured_project_profiles(config)
    default_project = config.get("default_project", {})
    default_project_id = config.get("default_project_id")
    if not default_project_id and isinstance(default_project, dict):
        default_project_id = default_project.get("project_id")
    if not default_project_id and isinstance(default_project, str):
        default_project_id = default_project
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
            requested_cids = list(
                dict.fromkeys(
                    (
                        active_cmds
                        +
                        waiting_cmds
                    )[-20:]
                )
            )
            active_or_waiting = set(requested_cids)
            latest_command_id = str(latest_cmd.get("command_id") or "") or None

            for root in store_roots:
                if not (
                    root
                    / "commands"
                ).is_dir():
                    continue

                store = RuntimeStore(
                    root
                )

                # Runtime detailed status can be DEGRADED while durable command
                # state is still authoritative. Select one current/latest command
                # per project directly from durable state so the panel never
                # fabricates zero lanes merely because enrichment failed.
                preferred_by_project = {}

                for cid in store.list_command_ids()[-200:]:
                    cmd_data = (
                        store.read_command(
                            cid
                        )
                        or {}
                    )

                    state_data = (
                        store.read_state(
                            cid
                        )
                        or {}
                    )

                    project_id = (
                        (
                            cmd_data.get(
                                "project"
                            )
                            or {}
                        ).get(
                            "project_id"
                        )
                        or
                        "unknown"
                    )

                    previous = (
                        preferred_by_project.get(
                            project_id
                        )
                    )

                    score = _command_recency_key(
                        cid,
                        state_data,
                        cmd_data,
                        active_or_waiting=active_or_waiting,
                        latest_command_id=latest_command_id,
                    )

                    if (
                        previous is None
                        or
                        score
                        >
                        previous[0]
                    ):
                        preferred_by_project[
                            project_id
                        ] = (
                            score,
                            cid,
                        )

                cids_to_scan = [
                    cid
                    for _score, cid in preferred_by_project.values()
                ]

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
                            "worker_execution_attempt_count": int(cmd_state.get("attempts", 0) or 0),
                            "retry_after_epoch": cmd_state.get("retry_after_epoch"),
                            "updated_at": cmd_state.get("updated_at"),
                            "canonical_source_sha": cmd_state.get("canonical_source_sha"),
                            **_command_work(store.command_dir(cid), cmd_state, cmd_data),
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
                            deliberation_metrics["COUNCIL_PROVIDER_CALL_COUNT"] = deliberation_metrics.get("COUNCIL_PROVIDER_CALL_COUNT", 0) + int(entry.get("council_provider_call_count", 0) or 0)
                            deliberation_metrics["COUNCIL_PROVIDER_TOKEN_ESTIMATE"] = deliberation_metrics.get("COUNCIL_PROVIDER_TOKEN_ESTIMATE", 0) + int(entry.get("council_provider_token_estimate", 0) or 0)
                    except Exception:
                        pass
                # Aggregate primary provider calls/tokens from attempts journals
                deliberation_metrics["PRIMARY_PROVIDER_CALL_COUNT"] = 0
                deliberation_metrics["PRIMARY_PROVIDER_TOKEN_ESTIMATE"] = 0
                for attempts_file in (root / "commands").glob("*/project-runtime/provider-attempts.jsonl"):
                    try:
                        for line in attempts_file.read_text("utf-8").strip().splitlines():
                            if line.strip():
                                att = json.loads(line)
                                if att.get("status") == "SUCCESS":
                                    deliberation_metrics["PRIMARY_PROVIDER_CALL_COUNT"] += 1
                                    deliberation_metrics["PRIMARY_PROVIDER_TOKEN_ESTIMATE"] += int(att.get("tokens", 800) or 800)
                    except Exception:
                        pass
                c_calls = deliberation_metrics.get("COUNCIL_PROVIDER_CALL_COUNT", 0)
                p_calls = deliberation_metrics.get("PRIMARY_PROVIDER_CALL_COUNT", 0)
                tot_calls = max(1, p_calls + c_calls)
                deliberation_metrics["COUNCIL_REASONING_SHARE_ESTIMATE"] = round(c_calls / tot_calls, 4)
        except Exception:
            pass

        sha_format_valid = is_valid_full_sha(active_sha)
        sha_format_status = "VALID" if sha_format_valid else "INVALID"

        # Check candidate manifest and build provenance if slot_root is known
        candidate_manifest_sha = None
        build_source_sha = None
        candidate_manifest_data: Dict[str, Any] = {}
        slot_root_str = runtime_v1.get("runtime_slot_root")
        if slot_root_str:
            try:
                manifest_path = Path(slot_root_str) / "candidate-manifest.json"
                if manifest_path.is_file():
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        candidate_manifest_data = json.load(f)
                        candidate_manifest_sha = candidate_manifest_data.get("candidate_source_sha")
                        build_source_sha = candidate_manifest_data.get("build_source_sha")
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

        # Live immutable deployment provenance is independent of later source
        # checkout movement. Source checkout relation remains separately visible.
        deployment_validation = (
            validate_materialized_runtime_provenance(
                manifest=candidate_manifest_data,
                runtime_source_sha=active_sha,
                runtime_asset_tree_sha256=runtime_v1.get(
                    "runtime_asset_tree_sha256"
                ),
            )
        )

        provenance_status = str(
            deployment_validation.get(
                "status",
                "UNPROVEN",
            )
        )

        if (
            local_git_head
            and
            is_valid_full_sha(
                local_git_head
            )
            and
            is_valid_full_sha(
                active_sha
            )
        ):
            source_checkout_relation = (
                "MATCH"
                if local_git_head.lower()
                ==
                active_sha.lower()
                else
                "DRIFTED"
            )
        else:
            source_checkout_relation = (
                "UNAVAILABLE"
            )

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

        if (
            runtime_v1.get(
                "status_state"
            )
            ==
            "DEGRADED"
        ):
            alerts.append(
                "DETAILED_TELEMETRY_DEGRADED: last accepted durable telemetry is being preserved"
            )
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
        baseline_by_command = {
            "continue-b181ddc574c25c2aa0f2a6b9": 25,
            "continue-61be4ab1af53cfa646d773ce": 18,
        }
        product_delta_by_lane = {
            lane.get("command_id"): max(
                0,
                int(lane.get("completed_batches", 0) or 0) - baseline_by_command.get(lane.get("command_id"), int(lane.get("completed_batches", 0) or 0)),
            )
            for lane in lanes_detail.values()
        }
        meaningful_batch_delta = sum(product_delta_by_lane.values())
        if any(command_id in active_cmds + waiting_cmds for command_id in baseline_by_command) and meaningful_batch_delta == 0:
            alerts.append("NO MEANINGFUL PRODUCT PROGRESS")
        for row in runtime_v1.get("provider_details", []) or []:
            if row.get("circuit_state") != "CLOSED":
                alerts.append(
                    f"PROVIDER_{str(row.get('provider_id', 'unknown')).upper()}={row.get('circuit_state', 'UNKNOWN')}:"
                    f"{row.get('failure_class') or 'NO_LIVE_SUCCESS'}"
                )

        return {
            "schema_version": "1.0.0",
            "host_state": bridge.get("host_state", "UNKNOWN"),
            "pending_jobs": int(bridge.get("pending_jobs", 0)),
            "allow_paid_fallback": False,
            "paid_fallback_enabled": False,
            "paid_daily_budget_usd": 0,
            "paid_monthly_budget_usd": 0,
            "paid_call_count": 0,
            "providers": providers,
            "sanitized_providers": _get_sanitized_providers(config, providers),
            "provider_details": runtime_v1.get("provider_details", []),
            "resource_operations_matrix": get_resource_operations_matrix(runtime_v1=runtime_v1),
            "projects": projects,
            "default_project_id": default_project_id,
            "default_project": default_project,
            "runtime_v1": runtime_v1,
            "active_slot": active_slot,
            "active_sha": active_sha,
            "sha_format_status": sha_format_status,
            "provenance_status": provenance_status,
            "candidate_manifest_sha": candidate_manifest_sha,
            "build_source_sha": build_source_sha,
            "local_git_head": local_git_head,
            "source_checkout_relation": source_checkout_relation,
            "provenance_errors": deployment_validation.get("errors", []),
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
                "meaningful_batch_delta": meaningful_batch_delta,
                "meaningful_batch_delta_by_command": product_delta_by_lane,
                "tests_ci_state": {
                    key: value.get("tests_ci_state") for key, value in lanes_detail.items()
                },
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
        "allow_paid_fallback": False,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 0,
        "paid_monthly_budget_usd": 0,
        "paid_call_count": 0,
        "providers": providers,
        "sanitized_providers": _get_sanitized_providers(config, providers),
        "projects": projects,
        "default_project_id": default_project_id,
        "default_project": default_project,
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
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, {
                "contract_version": "1.0.0",
                "panel_state": "HEALTHY",
                "pid": os.getpid(),
                "supervisor_pid": os.environ.get("AOS_PANEL_SUPERVISOR_PID"),
                "runtime_source_sha": os.environ.get("AOS_RUNTIME_SOURCE_SHA"),
                "bind_host": "127.0.0.1",
                "production": "NO_GO",
                "owner": "AOS",
                "ag_required": False,
            })
            return
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
