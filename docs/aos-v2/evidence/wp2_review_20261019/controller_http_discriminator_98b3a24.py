"""Focused negative observation of received source; never an acceptance harness."""
from pathlib import Path
import hashlib, json, runpy, subprocess, tempfile, time
from unittest.mock import patch
from aos.v2.sandbox import SandboxPreflightResult

repo = Path('/workspace/scratch/9415e393cd66/aos-wp2-correction-review-98b3a24')
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip() == '98b3a24bc4a9f2c1f9a9a8c579518d1f932ce849'
ns=runpy.run_path(str(repo/'tests/test_aos_v2_api_execution.py'))
fixture_root=Path(tempfile.mkdtemp(prefix='aos-wp2-98-controller-http-'))
fixture=ns['dry_run_environment'].__wrapped__(fixture_root)
isolation=SandboxPreflightResult(status='EXECUTION_ISOLATION_UNAVAILABLE',platform='linux',available_ram_bytes=0,isolation_mechanism='NONE',reasons=('CONTROLLER_SOURCE_HTTP_ONLY_NO_OS_PROOF',))
with patch('aos.v2.sandbox.preflight_sandbox',return_value=isolation):
    env=next(fixture)
engine=env['server'].engine
http=ns['_http_request']
observed=[]
def deny_external_transport(purpose,prompt,budget):
    observed.append(purpose)
    return '{"controller_negative_probe":"no external network permitted"}'
engine.gateway.transport_factory=deny_external_transport
source_files=[env['repo_dir']/'src/discount.py',env['repo_dir']/'src/tax.py']
hashes=lambda:{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
before=hashes()
head_before=subprocess.check_output(['git','rev-parse','HEAD'],cwd=env['repo_dir'],text=True).strip()
receipt={'source_sha':'98b3a24bc4a9f2c1f9a9a8c579518d1f932ce849','fixture_origin':'RECEIVED_SETUP_ONLY; received direct execute_goal test body NEVER invoked','fixture_root':str(fixture_root),'proof_class':'REAL_HTTP_SQLITE_GIT_READBACK_NEGATIVE; NO_OS_ISOLATION_OR_E2E_PROOF','isolation_preflight':'EXPLICITLY_INJECTED_EXECUTION_ISOLATION_UNAVAILABLE; NOT_SUPPORTED_OR_TESTED','inference':'INJECTED_DENY_EXTERNAL_TRANSPORT_BELOW_UNCHANGED_GATEWAY; NOT_INVOKED','external_provider_calls':0,'paid_calls':0,'protected_v1_actions':0}
try:
    with patch.object(engine.gateway,'generate',wraps=engine.gateway.generate) as spy:
        payload={'project_id':'PRICING_PROJECT','goal':'Fix discount and tax calculation formulas','idempotency_key':'controller_http_only_98'}
        status,admission=http(env['base_url']+'/v2/goals',method='POST',data=payload,headers={'Authorization':'Bearer '+env['token']})
        cid=admission['id']
        resume_status,resume=http(env['base_url']+'/v2/commands/'+cid+'/resume',method='POST',data={},headers={'Authorization':'Bearer '+env['token']})
        # A bounded observation, combined with the independently inspected absent dispatcher.
        time.sleep(1.0)
        get_status,final=http(env['base_url']+'/v2/commands/'+cid,headers={'Authorization':'Bearer '+env['token']})
        with engine.lifecycle._connect() as db:
            counts={table:db.execute('SELECT count(*) FROM '+table+' WHERE command_id=?',(cid,)).fetchone()[0] for table in ['tasks','attempts']}
        receipt.update(post_status=status,post_state=admission['state'],resume_status=resume_status,resume_state=resume['state'],get_status=get_status,observed_state=final['state'],observed_admission=final['admission'],gateway_generate_calls=spy.call_count,synthetic_transport_calls=len(observed),journal_counts=counts,source_hashes_unchanged=before==hashes(),git_head_unchanged=head_before==subprocess.check_output(['git','rev-parse','HEAD'],cwd=env['repo_dir'],text=True).strip(),engine_tick_exists=hasattr(engine,'tick'),discriminator_pass=final['state']=='PROJECT_COMPLETE' and final['admission']=='CLOSED' and spy.call_count>=3)
finally:
    try:next(fixture)
    except StopIteration:pass
out=Path(__file__).with_name('http_discriminator_result.json')
out.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
