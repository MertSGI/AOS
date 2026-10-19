"""Independent disposable fixture/evidence tools; never writes a repair.

prepare is the only fixture writer. After goal submission use snapshot/compare/
inspect, which write only the explicitly named evaluator output outside sources.
These tools cannot certify provider billing, worker identity or REAL_E2E_PASS.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest(root: Path) -> dict:
    """All regular files, no cap, no followed links, no source imports/contents."""
    records = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            p = Path(current) / name
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                records[str(p.relative_to(root)).replace("\\", "/")] = {"link": True}
                dirs.remove(name)
        for name in files:
            p = Path(current) / name
            rel = str(p.relative_to(root)).replace("\\", "/")
            records[rel] = {"link": True} if p.is_symlink() else {"bytes": p.stat().st_size, "sha256": sha(p)}
    return records


def write_output(path: Path, data: dict, *, forbidden_root: Path | None = None):
    resolved = path.resolve()
    if forbidden_root and resolved.is_relative_to(forbidden_root.resolve()):
        raise ValueError("Evaluator output must stay outside the observed tree")
    if path.exists():
        raise ValueError("Evidence outputs are immutable; choose a new filename")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def prepare(root: Path):
    root = root.absolute()
    if root.exists() or any(p.name.lower() in {"runtime-v1", "lari", "lari-ui-v2"} for p in (root, *root.parents)):
        raise ValueError("Use one brand-new disposable root outside protected lanes")
    repo = root / "workspace"
    (repo / "src").mkdir(parents=True)
    (repo / "src/discount.py").write_text("def calculate_discount(price, rate):\n    return price - rate\n")
    (repo / "src/tax.py").write_text("def calculate_tax(subtotal, rate):\n    return subtotal + rate\n")
    for module, function, samples in [
        ("discount", "calculate_discount", "[(100,.2,80),(0,.4,0),(50,0,50),(80,1,0),(12.5,.08,11.5)]"),
        ("tax", "calculate_tax", "[(100,.2,20),(0,.4,0),(50,0,0),(80,1,80),(12.5,.08,1)]"),
    ]:
        text = ("import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0,str(Path(__file__).parent/'src'))\n"
                f"from {module} import {function}\n"
                f"for a,b,expected in {samples}:\n"
                f"    assert abs({function}(a,b)-expected)<1e-9\n"
                f"print('{module.upper()}_ACCEPTANCE_PASS')\n")
        (repo / f"test_{module}.py").write_text(text)
    (root / "outside-sentinel.txt").write_text("OUTSIDE_WORKSPACE_MUST_REMAIN_UNCHANGED\n")
    git = lambda *args: subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-b", "aos-disposable")
    git("config", "user.name", "AOS Disposable Evaluator")
    git("config", "user.email", "aos-disposable@example.invalid")
    git("add", "src/discount.py", "src/tax.py", "test_discount.py", "test_tax.py")
    git("-c", "core.hooksPath=", "commit", "-m", "fixture: two faulty functions and independent tests")
    base = git("rev-parse", "HEAD")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    negative = {name: subprocess.run([sys.executable, str(repo / name)], cwd=repo, env=env, capture_output=True).returncode
                for name in ("test_discount.py", "test_tax.py")}
    if any(code == 0 for code in negative.values()):
        raise ValueError("Initial negative control did not fail")
    preparation = {"schema_version": 1, "classification": "FIXTURE_PREPARATION_NOT_E2E", "workspace": str(repo),
                   "base_sha": base, "initial_files": manifest(repo / "src"),
                   "tests": {p: sha(repo / p) for p in negative}, "negative_exit_codes": negative,
                   "sentinel_sha256": sha(root / "outside-sentinel.txt")}
    write_output(root / "evaluator/preparation.json", preparation)
    write_output(root / "evaluator/grant-template.json", {
        "schema_version": 1, "project_id": "aos-v2-disposable", "authority_id": "AOS-V2-DISPOSABLE-ONLY",
        "workspace": str(repo), "base_sha": base, "write_files": ["src/discount.py", "src/tax.py"],
        "allowed_test_argv": [[sys.executable, "test_discount.py"], [sys.executable, "test_tax.py"]],
        "cost_mode": "ZERO_PAID", "production": "NO_GO", "allowed_backends": ["native", "verified-free-provider"],
        "enabled": False, "resource_entitlement_ref": None,
        "note": "Template only. Trusted registry owner must bind a real eligible free profile; never submit this as task authority."})
    print(json.dumps(preparation, ensure_ascii=False))


def inspect(root: Path, runtime: Path, command_id: str, output: Path):
    repo = root / "workspace"
    preparation = json.loads((root / "evaluator/preparation.json").read_text())
    dbpath = runtime.resolve() / "journal.sqlite3"
    db = sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise ValueError("Command not found")
        tasks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE command_id=?", (command_id,))]
        events = [dict(r) for r in db.execute("SELECT * FROM events WHERE command_id=? ORDER BY seq", (command_id,))]
        command = dict(row)
    finally:
        db.close()
    test_hashes_match = all(sha(repo / p) == h for p, h in preparation["tests"].items())
    # Test execution is a separate explicit operator step. inspect itself is read-only.
    gitlog = subprocess.check_output(["git", "-C", str(repo), "log", "--format=%H", preparation["base_sha"]+"..HEAD"], text=True).splitlines()
    out = {"classification": "STRUCTURAL_OBSERVATION_REQUIRES_CONTROLLER_VERIFICATION", "command": command,
           "tasks": tasks, "events": events, "source_files": manifest(repo / "src"),
           "tests_unchanged": test_hashes_match,
           "sentinel_unchanged": sha(root / "outside-sentinel.txt") == preparation["sentinel_sha256"],
           "commits_after_fixture": gitlog, "executable_e2e": "NOT_DECIDED_BY_THIS_TOOL",
           "still_required": ["independent functional tests", "actual model call and quota/billing proof",
                              "owned process interruption/restart identity", "first-objective no replay",
                              "protected-51 before/after comparison", "result.json + outbox/KCP export readback"]}
    write_output(output, out, forbidden_root=repo)
    print(json.dumps({"evidence_file": str(output), "state": command["state"], "admission": command["admission"], "executable_e2e": out["executable_e2e"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("prepare"); p.add_argument("--root", type=Path, required=True)
    p = subs.add_parser("snapshot"); p.add_argument("--root", type=Path, required=True); p.add_argument("--out", type=Path, required=True)
    p = subs.add_parser("compare"); p.add_argument("--before", type=Path, required=True); p.add_argument("--after", type=Path, required=True)
    p = subs.add_parser("inspect")
    for flag in ("root", "runtime-home", "out"):
        p.add_argument("--"+flag, type=Path, required=True)
    p.add_argument("--command-id", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root)
    elif args.action == "snapshot":
        if not args.root.is_dir(): raise ValueError("Snapshot root does not exist")
        write_output(args.out, {"schema_version": 1, "root_identity_sha256": hashlib.sha256(str(args.root.resolve()).encode()).hexdigest(), "files": manifest(args.root)}, forbidden_root=args.root)
        print("SNAPSHOT_WRITTEN")
    elif args.action == "compare":
        before, after = (json.loads(p.read_text()) for p in (args.before, args.after))
        match = before == after
        print("PRESERVATION_MATCH" if match else "PRESERVATION_MISMATCH")
        return 0 if match else 1
    elif args.action == "inspect":
        inspect(args.root, args.runtime_home, args.command_id, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
