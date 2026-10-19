"""Independent Controller counterexamples at a pinned WP2 source. No provider calls."""
from __future__ import annotations
import datetime,hashlib,http.client,json,os,socket,sqlite3,subprocess,sys,tempfile,threading,time
from pathlib import Path
from aos.v2.api import AOSApiServer,_strict_json_loads
from aos.v2.engine import Engine,ExecutionPlan,TaskNode
from aos.v2.acceptance import OracleCriterion
from aos.v2.bridge import WorkerGitBridge,TaskIntent,compute_file_sha256
from aos.v2.lifecycle import LifecycleStore,TrustedGrant
from aos.v2.registry import authorize,load_registry,revalidate
from aos.v2.resources import ResourceProfile,check_eligibility,ResourceUnavailableError
from aos.v2.sandbox import SandboxRunner,preflight_sandbox
import faulthandler
faulthandler.dump_traceback_later(25, exit=True)
import aos.v2.sandbox as sandbox_module
from aos.v2.sandbox import SandboxPreflightResult
# Source-only review: actual nested OS launch was locally blocked/interrupted.
def source_only_preflight():
 return SandboxPreflightResult('EXECUTION_ISOLATION_UNAVAILABLE','linux',0,'NONE',('CONTROLLER_SOURCE_ONLY_NO_OS_ISOLATION_CLAIM',))
sandbox_module.preflight_sandbox=source_only_preflight
preflight_sandbox=source_only_preflight
OUT=Path(__file__).parent
ROOT=Path(tempfile.mkdtemp(prefix='controller-owned-',dir=OUT))
observations={}
def git(r,*args):
 return subprocess.check_output(['git',*args],cwd=r,text=True,stderr=subprocess.PIPE).strip()
def repo(name):
 r=ROOT/name;r.mkdir();git(r,'init','-b','aos-owned');git(r,'config','user.name','AOS Controller probe');git(r,'config','user.email','controller-probe@aos.invalid')
 (r/'src').mkdir();(r/'src/a.py').write_text('def f(x):\n    return 0\n');(r/'other.txt').write_text('original\n')
 git(r,'add','.');git(r,'commit','-m','controller-owned initial');return r,git(r,'rev-parse','HEAD')
r,base=repo('api-repo'); oracle=ROOT/'oracle.json';oracle.write_text(json.dumps({'cases':[{'case_id':'one','args':[1],'expected':1}]}))
reg=ROOT/'registry.json'; regdata={'schema_version':1,'projects':[{'project_id':'probe','enabled':True,'classification':'DISPOSABLE_SOURCE','authority_id':'CONTROLLER_PROBE_ONLY','workspace':str(r),'base_sha':base,'write_files':['src/a.py'],'criteria':[{'id':'C1','kind':'FUNCTION_ORACLE_V1','module':'a','symbol':'f','oracle_ref':str(oracle),'oracle_sha256':compute_file_sha256(oracle)}],'resources':[],'cost_mode':'ZERO_PAID','production':'NO_GO','expires_at':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()}],'resources':[]}
def save_reg():reg.write_text(json.dumps(regdata));reg.chmod(0o600)
save_reg();home=ROOT/'api-home';server=AOSApiServer(home,reg);th=threading.Thread(target=server.serve_forever,daemon=True);th.start();port=server.server_address[1]
def request(method,path,body=None,extra=None):
 c=http.client.HTTPConnection('127.0.0.1',port,timeout=3); headers={'Authorization':'Bearer '+server.bearer_token,'Host':f'127.0.0.1:{port}'};headers.update(extra or {})
 b=body if isinstance(body,bytes) else json.dumps(body).encode() if body is not None else None
 c.request(method,path,body=b,headers=headers);res=c.getresponse();status=res.status;raw=res.read();c.close();return status,json.loads(raw)
try:
 wrongport=port+1 if port<65535 else port-1
 observations['api_wrong_host_port_status']=request('GET','/v2/health',extra={'Host':f'127.0.0.1:{wrongport}'})[0]
 observations['api_wrong_origin_port_status']=request('GET','/v2/health',extra={'Origin':f'http://127.0.0.1:{wrongport}'})[0]
 status,admitted=request('POST','/v2/goals',{'project_id':'probe','goal':'repair f','idempotency_key':'independent-admit'})
 observations['api_admit']={'status':status,'state':admitted.get('state'),'admission':admitted.get('admission')}
 cid=admitted['id'];status,resumed=request('POST',f'/v2/commands/{cid}/resume',{'forged':'body_should_be_rejected'})
 time.sleep(0.15);snap=server.engine.lifecycle.snapshot(cid)
 with server.engine.lifecycle._connect() as db: count=db.execute('select count(*) from attempts where command_id=?',(cid,)).fetchone()[0]
 observations['api_resume_forged_body']={'status':status,'state':snap['state'],'tasks':len(snap['tasks']),'attempts':count}
 b=json.dumps({'project_id':'probe','goal':'repair f utf16','idempotency_key':'utf16'}).encode('utf-16')
 observations['api_utf16_body_status']=request('POST','/v2/goals',b)[0]
 parsed={}
 for name,b in [('nan',b'{"x":NaN}'),('deep',('['*10+'0'+']'*10).encode())]:
  try:_strict_json_loads(b);parsed[name]='ACCEPTED'
  except Exception as e:parsed[name]='REJECTED:'+type(e).__name__
 observations['strict_decoder']=parsed
 # Server-owned revalidation must detect criterion/resource/budget drift.
 binding=authorize(load_registry(reg),'probe').binding();regdata['projects'][0]['criteria'][0]['oracle_sha256']='b'*64;regdata['projects'][0]['budget']={'max_network_attempts':8};regdata['projects'][0]['resources']=['new-resource'];save_reg()
 try: revalidate(binding,reg);observations['registry_capability_drift']='ACCEPTED'
 except Exception as e:observations['registry_capability_drift']='REJECTED:'+type(e).__name__
 regdata['projects'][0]['criteria'][0]['oracle_sha256']=compute_file_sha256(oracle);regdata['projects'][0].pop('budget',None);regdata['projects'][0]['resources']=[];save_reg()
finally:server.shutdown_and_close();th.join(timeout=3)
# Nonempty unmarked home is silently adopted by Engine, before LifecycleStore can reject it.
unmarked=ROOT/'unmarked-home';unmarked.mkdir();(unmarked/'owned-sentinel.txt').write_text('do not adopt')
try:
 e=Engine(unmarked,reg);e.close();observations['engine_unmarked_home']='ADOPTED'
except Exception as ex:observations['engine_unmarked_home']='REJECTED:'+type(ex).__name__
# Resource unavailability is terminalized rather than held; plan and inference are SIMULATED.
crit=OracleCriterion('C1','a','f',oracle,compute_file_sha256(oracle));plan=ExecutionPlan('probe-plan',[TaskNode('repair-one','src/a.py',crit)])
with Engine(ROOT/'resource-home',reg) as e:
 def unavailable(*a):raise ResourceUnavailableError('CONTROLLER_INJECTED_RESOURCE_UNAVAILABLE_NO_NETWORK')
 try:e.execute_goal('probe','repair',plan,idempotency_key='resource-unavailable',generator_func=unavailable)
 except ResourceUnavailableError:pass
 snap=e.lifecycle.snapshot('cmd_resource_unavailable')
 observations['resource_unavailable_exception']={'state':snap['state'],'admission':snap['admission'],'task_state':snap['tasks'][0]['state'],'attempt_profiles':['simulated_profile'],'run_finished':sum(x['kind']=='run.finished' for x in snap['events'])}
# Unknown schema should be read-only refused, but connect changes journal mode before refusal.
u=ROOT/'unknown-schema';u.mkdir();(u/'.aos-v2-home').write_text('AOS_V2_JOURNAL_1\n');dbpath=u/'journal.sqlite3';db=sqlite3.connect(dbpath);db.execute('pragma user_version=99');db.commit();db.close();before=hashlib.sha256(dbpath.read_bytes()).hexdigest()
try:LifecycleStore(u)
except ValueError:pass
observations['unknown_schema_refusal_mutated_db']=hashlib.sha256(dbpath.read_bytes()).hexdigest()!=before
# Direct storage fence check is a SOURCE contract probe, not a stale-process OS proof.
st=LifecycleStore(ROOT/'fence-home');grant=TrustedGrant('probe','CONTROLLER_PROBE_ONLY',str(r),base,('src/a.py',));st.admit('fence-cmd','fence-key','repair',('C1',),grant,owner_epoch=2)
try:st.transition('fence-cmd',0,'PLANNING',reason='stale epoch probe',owner_epoch=1);observations['stale_epoch_transition']='ACCEPTED'
except Exception as e:observations['stale_epoch_transition']='REJECTED:'+type(e).__name__
# Git/native effects are genuine, but this is a disposable bridge counterexample, not a worker task.
br,bbase=repo('bridge-repo');bridge=WorkerGitBridge(br);(br/'other.txt').write_text('unrelated staged bytes\n');git(br,'add','other.txt')
intent=TaskIntent('bridge-cmd','bridge-task',1,br,'aos-owned',bbase,'src/a.py',compute_file_sha256(br/'src/a.py'),[{'old':'return 0','new':'return x'}]);prepared=bridge.prepare_candidate(intent,ROOT/'bridge-scratch');published=bridge.publish_candidate(prepared)
observations['bridge_unrelated_staged_ref_published']=git(br,'rev-parse','HEAD')==published.commit_sha
(br/'src/a.py').write_text('UNEXPECTED_CONTROLLER_SENTINEL\n')
observations['reconcile_wrong_posthash_running']=bridge.reconcile_task(intent,current_task_status='RUNNING',stored_commit_sha=published.commit_sha,stored_prepared_tree=prepared.tree_sha).value
observations['reconcile_wrong_posthash_accepted']=bridge.reconcile_task(intent,current_task_status='ACCEPTED',stored_commit_sha=published.commit_sha,stored_prepared_tree=prepared.tree_sha).value
bridge.materialize_source(prepared);observations['materialize_overwrote_drift']=(br/'src/a.py').read_text()=='def f(x):\n    return x\n'
# Eligibility with explicit synthetic credential: no generation/network call.
now=datetime.datetime.now(datetime.timezone.utc);t=now.isoformat();os.environ['AOS_V2_REVIEW_DUMMY']='synthetic-no-provider-secret'
profile={'id':'probe-resource','enabled':True,'provider':'','model':'','protocol':'UNSUPPORTED','base_url':'http://127.0.0.1:9','credential_ref':'AOS_V2_REVIEW_DUMMY','capabilities':{},'context_window':1,'token_count_method':'unverified','entitlement':{'tier':'FREE','hard_no_overage':'false','observed_at':t},'quota':{'observed_at':t,'remaining_requests':1},'health':{'observed_at':t,'status':'HEALTHY'}}
observations['incomplete_incompatible_resource_eligibility']=check_eligibility(ResourceProfile.from_dict(profile))
os.environ.pop('AOS_V2_REVIEW_DUMMY',None)
# Actual sandbox negative probes touch ONLY Controller-created canaries/listener.
pre=preflight_sandbox();observations['sandbox_preflight']={'supported':pre.is_supported,'reasons':list(pre.reasons),'available_ram_bytes':pre.available_ram_bytes}
if pre.is_supported:
 candidate=ROOT/'sandbox-candidate';candidate.mkdir();scratch=ROOT/'sandbox-scratch';outside=ROOT/'outside-read-canary';outside.write_text('OWNED_SYNTHETIC_CANARY');outside.chmod(0o600)
 listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(3);lp=listener.getsockname()[1];listener.settimeout(6);accepted=[]
 def accept_connections():
  try:
   for _ in range(2):
    c,_=listener.accept();accepted.append(c.recv(100).decode());c.close()
  except socket.timeout:pass
 lt=threading.Thread(target=accept_connections,daemon=True);lt.start()
 with socket.create_connection(('127.0.0.1',lp),timeout=2) as c:c.sendall(b'parent-positive')
 code='import json,socket,subprocess,sys\nr={}\ntry:\n c=socket.create_connection(("127.0.0.1",'+str(lp)+'),timeout=2);c.sendall(b"sandbox-probe");c.close();r["loopback_connected"]=True\nexcept Exception as e:r["loopback_denied"]=type(e).__name__\ntry:r["outside_read"] = open('+repr(str(outside))+').read()\nexcept Exception as e:r["outside_read_denied"]=type(e).__name__\np=subprocess.run([sys.executable,"-I","-B","-c","print(123)"],capture_output=True);r["child_exit"]=p.returncode\nprint(json.dumps(r))'
 result=SandboxRunner().run_test([sys.executable,'-I','-B','-c',code],candidate,scratch,timeout=8)
 lt.join(timeout=0.2);listener.close();observations['actual_sandbox_owned_probes']={'exit_code':result.exit_code,'stdout':result.stdout,'stderr':result.stderr,'listener_observed':accepted,'scope':'OWNED_SYNTHETIC_CANARY_AND_ACTIVE_LOOPBACK_ONLY'}
out={'receipt_id':'AOS-WP2-CONTROLLER-PROBES-20261019-01','source_sha':'7188d8995f09db00be17c6cb6207616c1b4dd7b2','proof_class':'INDEPENDENT_SOURCE_NATIVE_COUNTEREXAMPLES_SIMULATED_INFERENCE_OS_UNAVAILABLE_NOT_E2E','actual_model_calls':0,'paid_calls':0,'protected_runtime_access':0,'lari_writes':0,'fixture_root':str(ROOT),'observations':observations}
(OUT/'controller_source_probe_result.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
