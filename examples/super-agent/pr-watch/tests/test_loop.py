import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

SOURCE=Path(os.environ.get('PR_WATCH_SOURCE',Path(__file__).resolve().parents[1]))

def setup(tmp_path, reports):
    for name in ['tick_loop.sh','config.py','digest_store.py','wake_rules.py']:
        shutil.copyfile(SOURCE/name,tmp_path/name)
    (tmp_path/'watch.json').write_text('{}');(tmp_path/'ledger.json').write_text('{"prs":{}}')
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

def test_lock_refuses_and_keeps_a_live_loop_holder(tmp_path):
    env=setup(tmp_path,[]); lock=tmp_path/'state/tick_loop.lock'; lock.mkdir(parents=True)
    holder_script=tmp_path/'live_tick_loop.sh'; holder_script.write_text('#!/bin/bash\nsleep 30\n')
    holder=subprocess.Popen(['bash',str(holder_script)])
    try:
        (lock/'pid').write_text(f'{holder.pid}\n')
        p=run(tmp_path,env);assert p.returncode==3;assert 'another_loop_running' in p.stdout
        assert lock.exists();assert not (tmp_path/'count').exists()
    finally:
        holder.terminate()
        holder.wait(timeout=3)

def test_a_dead_lock_holder_is_removed_before_the_loop_starts(tmp_path):
    env=setup(tmp_path,[]); lock=tmp_path/'state/tick_loop.lock'; lock.mkdir(parents=True)
    (lock/'pid').write_text('99999999\n')
    p=subprocess.run([sys.executable,str(tmp_path/'config.py'),'--loop-lock','42'],env=env,capture_output=True,text=True)
    assert p.returncode == 0 and 'removed stale tick loop lock' in p.stdout
    assert (lock/'pid').read_text() == '42\n'
    p=subprocess.run([sys.executable,str(tmp_path/'config.py'),'--loop-unlock','42'],env=env,capture_output=True,text=True)
    assert p.returncode == 0 and not lock.exists()

def test_loop_lock_does_not_remove_a_lock_before_its_pid_is_written(tmp_path):
    env=setup(tmp_path,[]); lock=tmp_path/'state/tick_loop.lock'; lock.mkdir(parents=True)
    p=subprocess.run([sys.executable,str(tmp_path/'config.py'),'--loop-lock','42'],env=env,capture_output=True,text=True)
    assert p.returncode == 3
    assert p.stdout.strip().startswith('WAKE none :: ')
    assert lock.exists() and not (lock/'pid').exists()

def test_loop_lock_replaces_a_stale_lock_that_has_an_extra_file(tmp_path):
    env=setup(tmp_path,[]); lock=tmp_path/'state/tick_loop.lock'; lock.mkdir(parents=True)
    (lock/'pid').write_text('99999999\n'); (lock/'unexpected').write_text('race\n')
    p=subprocess.run([sys.executable,str(tmp_path/'config.py'),'--loop-lock','42'],env=env,capture_output=True,text=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == ['removed stale tick loop lock']
    assert (lock/'pid').read_text() == '42\n'

def test_term_during_the_interval_sleep_exits_without_waiting_for_the_interval(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T00:00Z','errors':[]}]); env['INTERVAL']='30'
    p=subprocess.Popen(['bash',str(tmp_path/'tick_loop.sh')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        lock=tmp_path/'state/tick_loop.lock'
        end=time.monotonic()+5
        while not lock.exists() and time.monotonic() < end: time.sleep(.02)
        assert lock.exists()
        end=time.monotonic()+5
        while time.monotonic() < end:
            children=subprocess.run(['pgrep','-P',str(p.pid),'-x','sleep'],capture_output=True,text=True)
            if children.returncode == 0: break
            time.sleep(.02)
        assert children.returncode == 0
        p.terminate()
        p.wait(timeout=3)
    finally:
        if p.poll() is None: p.kill()
    assert p.returncode == 143 and not lock.exists()

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
    p=run(tmp_path,env);assert p.returncode==2;assert 'poll_errors=1/1' in p.stdout

def test_every_error_wake_exits_nonzero(tmp_path):
    for reason, replacement in [('filter_error', 'digest_store.py'), ('rule_error', 'wake_rules.py'),
                                ('delivered_error', 'digest_store.py')]:
        case=tmp_path/reason/'pr-watch';(case/'state').mkdir(parents=True)
        helper=case.parent/'bin';helper.mkdir()
        shutil.copyfile(SOURCE.parent/'bin/writer_lease.py',helper/'writer_lease.py')
        shutil.copyfile(SOURCE.parent/'bin/workplace.py',helper/'workplace.py')
        shutil.copyfile(SOURCE.parent/'workplace.json',case.parent/'workplace.json')
        env=setup(case,[{'polled_at':'2026-11-09T00:00Z','errors':[],
                         'new_issues':[{'number':1}]},
                        {'polled_at':'2026-11-09T01:00Z','errors':[],
                         'unwatched_open_prs':[{'number':2,'author':'your-login'}]}])
        original=case/replacement
        original.rename(case/(replacement+'.original'))
        if reason in ('delivered_error', 'filter_error'):
            failure = "'--delivered' in sys.argv" if reason == 'delivered_error' else "not any(a.startswith('--') for a in sys.argv[1:])"
            original.write_text(f'''import subprocess,sys
if {failure}: sys.exit(2)
sys.exit(subprocess.call([sys.executable,{str(case/(replacement+'.original'))!r},*sys.argv[1:]]))
''')
        elif reason == 'rule_error':
            original.write_text(f'''import sys
if __name__ == '__main__': sys.exit(2)
exec(compile(open({str(case/(replacement+'.original'))!r}).read(), {str(case/(replacement+'.original'))!r}, 'exec'))
''')
        else:
            original.write_text('import sys\nsys.exit(2)\n')
        result=run(case,env)
        assert result.returncode == 2 and reason.replace('delivered_error','WAKE') in result.stdout

def test_bad_configuration_reports_error(tmp_path):
    env=setup(tmp_path,[]);(tmp_path/'config.json').write_text('bad json')
    p=run(tmp_path,env);assert p.returncode==2;assert 'configuration_error' in p.stdout

def test_lock_replaces_a_live_pid_that_is_not_a_loop(tmp_path):
    env=setup(tmp_path,[]);lock=tmp_path/'state/tick_loop.lock';lock.mkdir(parents=True)
    (lock/'pid').write_text(f'{os.getpid()}\n')
    result=subprocess.run([sys.executable,str(tmp_path/'config.py'),'--loop-lock','42'],
                          env=env,capture_output=True,text=True,timeout=5)
    assert result.returncode == 0 and 'removed stale tick loop lock' in result.stdout
    assert (lock/'pid').read_text() == '42\n'

def test_24h_catchup_delivers_once(tmp_path):
    env=setup(tmp_path,[{'polled_at':'2026-11-09T01:00Z','errors':[]}]);(tmp_path/'state').mkdir(exist_ok=True)
    (tmp_path/'state/digest-pending.json').write_text(json.dumps([{'at':'2026-11-08T00:00Z','line':'noise'}]))
    p=run(tmp_path,env);assert p.returncode==0;assert ':: digest' in p.stdout
    assert json.loads((tmp_path/'state/digest-pending.json').read_text())==[]
