"""Materialize candidate slot for AOS runtime with strict fail-closed exact SHA provenance."""
import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Add src to sys.path so we can import authoritative provenance utilities
sys.path.insert(0, str(Path(__file__).parent / "src"))
from aos.provenance import (
    ProvenanceError,
    get_authoritative_git_head,
    is_valid_full_sha,
)


def verify_ci_run(repo: str, ci_run_id: int, expected_sha: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Verify GitHub Actions CI run conclusion SUCCESS and exact head_sha."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{ci_run_id}"
    headers = {
        "User-Agent": "AOS-Materializer/1.0",
        "Accept": "application/vnd.github+json",
    }
    # Check for optional token in environment or secure store
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            from aos.secure_store import read_provider_secret
            token = read_provider_secret("GITHUB")
        except Exception:
            token = None
    if token:
        headers["Authorization"] = f"token {token.strip()}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProvenanceError(f"GitHub Actions API returned HTTP {exc.code} for CI run {ci_run_id}: {exc.reason}")
    except Exception as exc:
        raise ProvenanceError(f"Failed to query GitHub Actions API for CI run {ci_run_id}: {exc}")

    head_sha = data.get("head_sha", "").strip().lower()
    conclusion = str(data.get("conclusion", "")).strip().lower()
    status = str(data.get("status", "")).strip().lower()

    if status != "completed":
        raise ProvenanceError(f"CI run {ci_run_id} is not completed (status='{status}')")
    if conclusion != "success":
        raise ProvenanceError(f"CI run {ci_run_id} conclusion is not 'success' (conclusion='{conclusion}')")
    if head_sha != expected_sha:
        raise ProvenanceError(
            f"CI run {ci_run_id} head_sha mismatch: expected '{expected_sha}', got '{head_sha}'"
        )

    return data


def materialize(sha: str, ci_run_id: int, repo_root: Optional[Path] = None, remote_repo: str = "MertSGI/AOS") -> Path:
    """Validate exact SHA provenance and materialize candidate slot."""
    # 1. Validate full 40-character SHA format
    cleaned_sha = str(sha).strip().lower()
    if not is_valid_full_sha(cleaned_sha):
        raise ProvenanceError(f"Source SHA is not a valid 40-character hex SHA: '{sha}'")

    if ci_run_id <= 0:
        raise ProvenanceError(f"Invalid CI run ID: {ci_run_id}")

    # 2. Validate authoritative local Git HEAD equals requested source SHA
    root = repo_root or Path(__file__).parent
    git_head = get_authoritative_git_head(root)
    if git_head != cleaned_sha:
        raise ProvenanceError(
            f"Authoritative local Git HEAD ({git_head}) does not match requested source SHA ({cleaned_sha})"
        )

    # 3. Validate CI run conclusion SUCCESS and head_sha == source_sha
    verify_ci_run(remote_repo, ci_run_id, cleaned_sha)

    # 4. Materialize candidate slot
    short_sha = cleaned_sha[:12]
    slot_id = f"candidate-runtime-v1.7.50-{short_sha}"
    candidate_root = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\mozcelikbas\AppData\Local")) / "AOS" / "runtime-v1" / "candidate" / cleaned_sha

    src_aos = root / "src" / "aos"
    candidate_site_aos = candidate_root / "site" / "aos"
    candidate_site_aos.mkdir(parents=True, exist_ok=True)

    for item in src_aos.iterdir():
        dest = candidate_site_aos / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    for name in ["pyproject.toml", "schemas", "descriptors"]:
        s = root / name
        d = candidate_root / name
        if s.is_dir():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
        elif s.is_file():
            shutil.copy2(s, d)

    manifest = {
        "provenance": "PROVEN",
        "ci_run_id": ci_run_id,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate_slot_id": slot_id,
        "candidate_source_sha": cleaned_sha,
        "build_source_sha": cleaned_sha,
    }
    (candidate_root / "candidate-manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")
    build_record = {
        "build_source_sha": cleaned_sha,
        "ci_run_id": ci_run_id,
        "built_at": manifest["built_at"],
        "slot_id": slot_id,
    }
    (candidate_root / "build-record.json").write_text(json.dumps(build_record, indent=4), encoding="utf-8")

    sup_code = f"""import os,sys
SITE=r'{candidate_root}\\site'
os.environ['PYTHONPATH']=SITE+os.pathsep+os.environ.get('PYTHONPATH','')
sys.path.insert(0,SITE)
from aos.runtime_supervisor import main
raise SystemExit(main(['--config','C:\\\\Users\\\\mozcelikbas\\\\AppData\\\\Local\\\\AOS\\\\runtime-v1\\\\supervisor-config.json']))
"""
    (candidate_root / "launch_supervisor.py").write_text(sup_code, encoding="utf-8")

    server_code = f"""import os,sys
SITE=r'{candidate_root}\\site'
os.environ['PYTHONPATH']=SITE+os.pathsep+os.environ.get('PYTHONPATH','')
sys.path.insert(0,SITE)
from aos.runtime_server import main
raise SystemExit(main(['--config','C:\\\\Users\\\\mozcelikbas\\\\AppData\\\\Local\\\\AOS\\\\runtime-v1\\\\runtime-config.json']))
"""
    (candidate_root / "launch_runtime_server.py").write_text(server_code, encoding="utf-8")

    panel_code = f"""import os,sys
SITE=r'{candidate_root}\\site'
os.environ['PYTHONPATH']=SITE+os.pathsep+os.environ.get('PYTHONPATH','')
sys.path.insert(0,SITE)
from aos.control_panel import main
raise SystemExit(main([
    '--host-config','C:\\\\Users\\\\mozcelikbas\\\\AppData\\\\Local\\\\AOS\\\\runtime-v1\\\\control-panel-host-config.json',
    '--panel-config','C:\\\\Users\\\\mozcelikbas\\\\AppData\\\\Local\\\\AOS\\\\runtime-v1\\\\control-panel-config.json'
]))
"""
    (candidate_root / "launch_panel.py").write_text(panel_code, encoding="utf-8")

    print(f"MATERIALIZED_CANDIDATE={candidate_root}")
    print(f"CANDIDATE_SLOT_ID={slot_id}")
    return candidate_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AOS Materializer: Validate exact SHA provenance and materialize candidate slot."
    )
    parser.add_argument(
        "--source-sha",
        required=True,
        help="Full 40-character hexadecimal Git commit SHA",
    )
    parser.add_argument(
        "--ci-run-id",
        required=True,
        type=int,
        help="Authoritative GitHub Actions CI run ID",
    )
    parser.add_argument(
        "--repo",
        default="MertSGI/AOS",
        help="GitHub repository path (default: MertSGI/AOS)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        materialize(args.source_sha, args.ci_run_id, remote_repo=args.repo)
        return 0
    except (ProvenanceError, ValueError) as exc:
        print(f"MATERIALIZER_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"MATERIALIZER_UNHANDLED_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
