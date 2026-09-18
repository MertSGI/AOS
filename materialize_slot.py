import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

def materialize(sha: str, ci_run_id: int):
    short_sha = sha[:12]
    slot_id = f"candidate-runtime-v1.7.50-{short_sha}"
    candidate_root = Path(r"C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1\candidate") / sha

    src_aos = Path(r"C:\Projects\AOS-lane-b\src\aos")
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
        s = Path(r"C:\Projects\AOS-lane-b") / name
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
        "candidate_source_sha": sha,
        "build_source_sha": sha,
    }
    (candidate_root / "candidate-manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")
    build_record = {
        "build_source_sha": sha,
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
raise SystemExit(main(['--config','C:\\\\Users\\\\mozcelikbas\\\\AppData\\\\Local\\\\AOS\\\\runtime-v1\\\\control-panel-config.json']))
"""
    (candidate_root / "launch_panel.py").write_text(panel_code, encoding="utf-8")

    print(f"MATERIALIZED_CANDIDATE={candidate_root}")
    print(f"CANDIDATE_SLOT_ID={slot_id}")

if __name__ == "__main__":
    materialize("45e8179427b0b2e3650228ae44bc7a52e9fcf096", 35352424806)
