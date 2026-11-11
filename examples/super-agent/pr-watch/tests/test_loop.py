import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE=Path(__file__).resolve().parents[1]

def setup(tmp_path, reports):
    for name in ['tick_loop.sh','config.py','digest_store.py','wake_rules.py']:
        shutil.copyfile(SOURCE/name,tmp_path/name)
    (tmp_path/'watch.json').write_text('{"identities":{"ours":["your-login"]}}');(tmp_path/'ledger.json').write_text('{"prs":{}}')
    (tmp_path/'config.json').write_text(json.dumps({'watch_dir':'.','ledger_path':'ledger.json'}))
    (tmp_path/'mock-reports.json').write_text(json.dumps(reports))
    (tmp_path/'poll.py').write_text('''import json
from pathlib import Path
p=Path('count');n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))
reports=json.loads(Path('mock-reports.json').read_text());d=reports[min(n,len(reports)-1)]
print(json.dumps(d))
''')
    env=os.environ.copy();env.update({'PYTHON':sys.executable,'PR_WATCH_CONFIG':str(tmp_path/'config.json'),'INTERVAL':'0','RETRY_DELAY':'0','CAO_TERMINAL_ID':'test-owner'})
    return env

def run(tmp_path,env):
    return subprocess.run(['bash',str(tmp_path/'tick_loop.sh')],env=env,capture_output=True,text=True,timeout=10)

def test_lock_refuses_and_keeps_existing_lock(tmp_path):
    env=setup(tmp_path,[]);(tmp_path/'state/tick_loop.lock').mkdir(parents=True)
    p=run(tmp_path,env);assert p.returncode==3;assert 'another_loop_running' in p.stdout
    assert (tmp_path/'state/tick_loop.lock').exists();assert not (tmp_path/'count').exists()

def test_digest_then_wake_owner_and_unlock(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T00:00Z','errors':[],'new_issues':[{'number':1}]},{'polled_at':'2026-11-09T01:00Z','errors':[],'unwatched_open_prs':[{'number':2,'author':'your-login'}]}])
    p=run(tmp_path,env);assert p.returncode==0;assert 'WAKE' in p.stdout
    assert '(loop test-owner)' in (tmp_path/'log.md').read_text()
    assert not (tmp_path/'state/tick_loop.lock').exists()
    reports=[json.loads(p.read_text()) for p in (tmp_path/'reports').glob('*.wake.json')]
    assert any(r['filter_mode']=='WAKE' and r['digest_lines'] for r in reports)
    assert json.loads((tmp_path/'state/digest-pending.json').read_text())==[]

def test_retry_preserves_both_reports_and_digest(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T00:00Z','errors':[{'error':'partial'}],'unwatched_open_prs':[{'number':2,'author':'your-login'}]},{'polled_at':'2026-11-09T00:01Z','errors':[],'new_issues':[{'number':3}]}])
    p=run(tmp_path,env);assert p.returncode==0;assert 'retry=' in p.stdout
    primary=[json.loads(p.read_text()) for p in (tmp_path/'reports').glob('*.wake.json') if 'retry' not in p.name][0]
    assert any('issue#3' in line for line in primary['digest_lines'])

def test_persistent_poll_errors_wake(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T00:00Z','errors':[{'error':'partial'}]}])
    p=run(tmp_path,env);assert p.returncode==0;assert 'poll_errors=1/1' in p.stdout

def test_bad_configuration_reports_error(tmp_path):
    env=setup(tmp_path,[]);(tmp_path/'config.json').write_text('bad json')
    p=run(tmp_path,env);assert p.returncode==2;assert 'configuration_error' in p.stdout

def test_24h_catchup_delivers_once(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T01:00Z','errors':[]}]);(tmp_path/'state').mkdir()
    (tmp_path/'state/digest-pending.json').write_text(json.dumps([{'at':'2026-11-08T00:00Z','line':'noise'}]))
    p=run(tmp_path,env);assert p.returncode==0;assert ':: digest' in p.stdout
    assert json.loads((tmp_path/'state/digest-pending.json').read_text())==[]
