"""Actual temp-disk/process crash probe of V1 storage, not a task E2E."""
import json, os, pathlib, subprocess, sys, tempfile
ROOT=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'src'))
from aos.runtime_store import RuntimeStore
from aos.runtime_admission import CommandAdmissionStore

for name in ['src/aos/runtime_store.py','src/aos/runtime_admission.py']:
    expected=subprocess.check_output(['git','show','9d3168afb35edf5ad06e6b5e419801493fe04572:'+name],cwd=ROOT)
    if (ROOT/name).read_bytes()!=expected:
        raise SystemExit('STOP: probe source differs from pinned audited V1 boundary')

with tempfile.TemporaryDirectory(prefix='aos-audit-probe-') as d:
    home=pathlib.Path(d);cid='audit-lifecycle-one'
    store=RuntimeStore(home)
    store.create_command({'command_id':cid,'project':{'project_id':'aos-disposable-audit'},'goal':'probe storage only'})
    store.write_result(cid,{'state':'PROJECT_COMPLETE'})
    store.write_state(cid,state='PROJECT_COMPLETE',worker_pid=None)
    store.append_event(cid,'run.finished',{'state':'PROJECT_COMPLETE'})
    terminal_admission=CommandAdmissionStore(home).get(cid).state
    code='''import os, pathlib, sys
sys.path.insert(0,sys.argv[1])
import aos.runtime_store as rs
root=pathlib.Path(sys.argv[2])
original=rs.atomic_json
def crash(path,payload):
    if path.name=='event-seq.json': os._exit(73)
    return original(path,payload)
rs.atomic_json=crash
rs.RuntimeStore(root).append_event('audit-event-crash','probe.before-death',{})
'''
    store.create_command({'command_id':'audit-event-crash','project':{'project_id':'aos-disposable-audit'},'goal':'event crash only'})
    child=subprocess.run([sys.executable,'-c',code,str(ROOT/'src'),d],capture_output=True)
    store.append_event('audit-event-crash','probe.after-restart',{})
    events=store.read_events('audit-event-crash')
    out={'classification':'ISOLATED_REAL_STORAGE_PROBE_NOT_MODEL_E2E','source_sha':'9d3168afb35edf5ad06e6b5e419801493fe04572','terminal_state':'PROJECT_COMPLETE','admission_after_terminal_writes':terminal_admission,'child_exit_code':child.returncode,'event_sequences':[e['seq'] for e in events],'duplicate_event_sequence':len({e['seq'] for e in events})!=len(events),'protected_windows_host':'NOT_ACCESSED','model_calls':0}
    print(json.dumps(out,indent=2))
    (pathlib.Path(__file__).parent/'discriminating_probe.json').write_text(json.dumps(out,indent=2)+'\n')
