"""Materialize an exact-SHA, self-contained Runtime V1 candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent / "src"))
from aos.provenance import ProvenanceError, get_authoritative_git_head, is_valid_full_sha
from aos.runtime_assets import materialize_runtime_assets
from aos.process_utils import run_headless
from aos.runtime_store import atomic_json


def verify_ci_run(repo: str, ci_run_id: int, expected_sha: str, timeout: float = 15.0) -> Dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{ci_run_id}"
    headers = {"User-Agent": "AOS-Materializer/2.0", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            from aos.secure_store import read_provider_secret
            token = read_provider_secret("GITHUB")
        except Exception:
            token = None
    if token:
        headers["Authorization"] = f"token {token.strip()}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProvenanceError(f"GitHub Actions API returned HTTP {exc.code} for CI run {ci_run_id}: {exc.reason}")
    except Exception as exc:
        raise ProvenanceError(f"Failed to query GitHub Actions API for CI run {ci_run_id}: {exc}")
    head_sha = str(data.get("head_sha") or "").strip().lower()
    conclusion = str(data.get("conclusion") or "").strip().lower()
    status = str(data.get("status") or "").strip().lower()
    if status != "completed":
        raise ProvenanceError(f"CI run {ci_run_id} is not completed (status={status!r})")
    if conclusion != "success":
        raise ProvenanceError(f"CI run {ci_run_id} conclusion is not 'success' (conclusion={conclusion!r})")
    if head_sha != expected_sha:
        raise ProvenanceError(f"CI run {ci_run_id} head_sha mismatch: expected {expected_sha!r}, got {head_sha!r}")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_clean_source(root: Path) -> None:
    if not (root / ".git").exists():
        return
    result = run_headless(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        timeout=30,
    )
    if result.returncode != 0:
        raise ProvenanceError(f"Unable to verify source worktree cleanliness: {result.stderr.strip()}")
    if result.stdout.strip():
        raise ProvenanceError("Source worktree is dirty; exact-SHA materialization requires a clean checkout")


def _bootstrap(site: Path) -> str:
    return f"""import importlib,os,sys
from pathlib import Path
SITE={str(site)!r}
os.environ['PYTHONPATH']=SITE
os.environ['PYTHONNOUSERSITE']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.dont_write_bytecode=True
sys.path.insert(0,SITE)
def _candidate_import(name):
    module=importlib.import_module(name)
    origin=Path(module.__file__).resolve()
    try:
        origin.relative_to(Path(SITE).resolve())
    except ValueError:
        raise RuntimeError(f'AOS-owned module {{name}} escaped candidate: {{origin}}')
    return module
_candidate_import('aos')
_candidate_import('extensions')
"""


def materialize(
    sha: str,
    ci_run_id: int,
    repo_root: Optional[Path] = None,
    remote_repo: str = "MertSGI/AOS",
    candidate_base: Optional[Path] = None,
) -> Path:
    cleaned_sha = str(sha).strip().lower()
    if not is_valid_full_sha(cleaned_sha):
        raise ProvenanceError(f"Source SHA is not a valid 40-character hex SHA: {sha!r}")
    if ci_run_id <= 0:
        raise ProvenanceError(f"Invalid CI run ID: {ci_run_id}")
    root = (repo_root or Path(__file__).parent).expanduser().resolve()
    git_head = get_authoritative_git_head(root)
    if git_head != cleaned_sha:
        raise ProvenanceError(f"Authoritative local Git HEAD ({git_head}) does not match requested source SHA ({cleaned_sha})")
    assert_clean_source(root)
    verify_ci_run(remote_repo, ci_run_id, cleaned_sha)

    default_base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AOS" / "runtime-v1" / "candidate"
    base = (candidate_base or default_base).expanduser().resolve()
    candidate_root = base / cleaned_sha
    runtime_home = base.parent
    if candidate_root.exists() and any(candidate_root.iterdir()):
        raise ProvenanceError(f"Immutable candidate already exists: {candidate_root}")
    candidate_root.mkdir(parents=True, exist_ok=True)
    asset_result = materialize_runtime_assets(root, candidate_root)
    shutil.copy2(root / "pyproject.toml", candidate_root / "pyproject.toml")

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_id = f"candidate-runtime-v1.8-{cleaned_sha[:12]}"
    build_record = {
        "build_source_sha": cleaned_sha,
        "ci_run_id": ci_run_id,
        "built_at": built_at,
        "slot_id": slot_id,
    }
    atomic_json(candidate_root / "build-record.json", build_record)

    bootstrap = _bootstrap(candidate_root / "site")
    launchers = {
        "launch_supervisor.py": bootstrap + f"\nfrom aos.runtime_supervisor import main\nraise SystemExit(main(['--config',{str(runtime_home / 'supervisor-config.json')!r}]))\n",
        "launch_runtime_server.py": bootstrap + f"\nfrom aos.runtime_server import main\nraise SystemExit(main(['--config',{str(runtime_home / 'runtime-config.json')!r}]))\n",
        "launch_panel.py": bootstrap + f"\nfrom aos.control_panel import main\nraise SystemExit(main(['--host-config',{str(runtime_home / 'control-panel-host-config.json')!r},'--panel-config',{str(runtime_home / 'control-panel-config.json')!r}]))\n",
    }
    for name, content in launchers.items():
        (candidate_root / name).write_text(content, encoding="utf-8", newline="\n")

    asset_manifest = json.loads(Path(asset_result["manifest_path"]).read_text(encoding="utf-8"))
    files = dict(asset_manifest["files"])
    for name in (
        "pyproject.toml", "build-record.json", "runtime-assets-manifest.json",
        "launch_supervisor.py", "launch_runtime_server.py", "launch_panel.py",
    ):
        files[name] = _sha256(candidate_root / name)
    tree = hashlib.sha256()
    for rel, file_sha in sorted(files.items()):
        tree.update(rel.encode("utf-8") + b"\0" + file_sha.encode("ascii") + b"\n")
    manifest = {
        "manifest_version": "2.0.0",
        "provenance": "PROVEN",
        "ci_run_id": ci_run_id,
        "built_at": built_at,
        "candidate_slot_id": slot_id,
        "candidate_source_sha": cleaned_sha,
        "build_source_sha": cleaned_sha,
        "required_source_roots": ["site/aos", "site/extensions", "schemas", "descriptors"],
        "file_count": len(files),
        "files": files,
        "candidate_tree_sha256": tree.hexdigest(),
    }
    atomic_json(candidate_root / "candidate-manifest.json", manifest)
    print(f"MATERIALIZED_CANDIDATE={candidate_root}")
    print(f"CANDIDATE_SLOT_ID={slot_id}")
    return candidate_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a self-contained exact-SHA AOS candidate")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run-id", required=True, type=int)
    parser.add_argument("--repo", default="MertSGI/AOS")
    parser.add_argument("--candidate-base")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        materialize(
            args.source_sha,
            args.ci_run_id,
            remote_repo=args.repo,
            candidate_base=Path(args.candidate_base) if args.candidate_base else None,
        )
        return 0
    except (ProvenanceError, ValueError) as exc:
        print(f"MATERIALIZER_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"MATERIALIZER_UNHANDLED_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
