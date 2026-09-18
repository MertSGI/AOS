import json
import urllib.request

req = urllib.request.Request(
    'https://api.github.com/repos/MertSGI/AOS/actions/runs?per_page=5',
    headers={'User-Agent': 'AOS-Agent'}
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        for r in data.get('workflow_runs', []):
            print(f"{r.get('id')} | {r.get('head_sha')} | {r.get('status')} | {r.get('conclusion')} | {r.get('name')}")
except Exception as e:
    print(f"ERROR: {e}")
