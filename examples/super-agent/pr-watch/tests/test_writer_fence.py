import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import pytest
import config
import digest_store
import poll
from conftest import SOURCE

STAMP='2026-11-13T00:00:00Z'


def setup(tmp_path,monkeypatch,kind='right'):
    kitstate=tmp_path/'kitstate';kitstate.mkdir()
    writer=kitstate/'WRITER';writer.write_text(f'host={socket.gethostname()} since={STAMP}\n')
    if kind=='wrong': writer.write_text(f'host=other-host since={STAMP}\n')
    if kind=='missing': writer.unlink()
    if kind=='missing_state': writer.unlink();kitstate.rmdir()
    state=tmp_path/'state';state.mkdir()
    if kind=='missing_watch_state': state.rmdir()
    watch=tmp_path/'watch.json';watch.write_text('{"repo":"owner/repo","prs":{}}')
    ledger=tmp_path/'ledger.json';ledger.write_text('{"prs":{}}')
    c=tmp_path/'config.json';c.write_text(json.dumps({'watch_dir':str(tmp_path),'ledger_path':'ledger.json'}))
    monkeypatch.setenv('PR_WATCH_CONFIG',str(c));monkeypatch.setenv('SUPER_AGENT_STATE_DIR',str(kitstate))
    monkeypatch.setattr(poll,'STATE_DIR',str(state));monkeypatch.setattr(poll,'CONFIG',config.load())
    return config.load()


def bytes_tree(root):
    return {str(p.relative_to(root)):p.read_bytes() if p.is_file() else None for p in root.rglob('*')}


@pytest.mark.parametrize('kind',['wrong','missing','missing_state','missing_watch_state'])
@pytest.mark.parametrize('op',['poll','save','atomic','process','journal','replay','delivered','loop'])
def test_watcher_refuses_before_any_write(tmp_path,monkeypatch,kind,op):
    c=setup(tmp_path,monkeypatch,kind)
    report=tmp_path/'report.json';report.write_text('{"polled_at":"2026-11-13T00:00Z","new_issues":[{"number":1}],"digest_pending":[]}')
    if op=='replay':
        c['journal'].parent.mkdir(exist_ok=True);c['journal'].write_text(json.dumps([{'id':'test','report':json.loads(report.read_text()),'wake':None}]))
        if kind=='missing_watch_state': c['journal'].unlink();c['journal'].parent.rmdir()
    before=bytes_tree(tmp_path)
    attempted=[]
    monkeypatch.setattr(poll,'gql',lambda *a:pytest.fail('network attempt'))
    if op=='poll':
        from types import SimpleNamespace
        def github(*args, **kwargs):
            attempted.append('github')
            return SimpleNamespace(stdout='[]')
        monkeypatch.setattr(poll.subprocess,'run',github)
    def invoke():
        if op=='poll': return poll.main()
        if op=='save': return poll.save(str(tmp_path/'state/snapshot.json'),{})
        if op=='atomic': return digest_store.atomic(tmp_path/'digest.md','data')
        if op=='process': return digest_store.process(report,c)
        if op=='journal': return digest_store.journal_add(c['journal'],{})
        if op=='replay': return digest_store.replay(c)
        if op=='delivered': return digest_store.delivered(report,c)
    if op=='loop':
        e={'PATH':'/usr/bin:/bin','PYTHON':sys.executable,'PR_WATCH_CONFIG':str(tmp_path/'config.json'),'SUPER_AGENT_STATE_DIR':str(tmp_path/'kitstate'),'PYTHONDONTWRITEBYTECODE':'1','RETRY_DELAY':'0','INTERVAL':'0','CAO_WORKPLACE_CONFIG':os.environ['CAO_WORKPLACE_CONFIG']}
        r=subprocess.run(['bash',str(SOURCE/'tick_loop.sh')],env=e,capture_output=True,text=True,timeout=10)
        assert r.returncode!=0 and 'not_writer' in r.stdout and 'Traceback' not in r.stderr
    else:
        # An empty replay is read-only, so missing watcher-state test uses atomic below.
        if op=='replay' and kind=='missing_watch_state':
            with pytest.raises(ValueError,match='not_writer'): digest_store.atomic(tmp_path/'digest.md','data')
        else:
            with pytest.raises(ValueError,match='not_writer'): invoke()
    assert attempted==[]
    assert bytes_tree(tmp_path)==before


def test_right_host_watcher_writes(tmp_path,monkeypatch):
    c=setup(tmp_path,monkeypatch)
    report=tmp_path/'report.json';report.write_text('{"polled_at":"2026-11-13T00:00Z","new_issues":[{"number":1}]}')
    digest_store.journal_add(c['journal'],json.loads(report.read_text()))
    result=digest_store.process(report,c)
    assert result['mode']=='DIGEST' and c['digest'].exists()
    poll.save(str(tmp_path/'state/snapshot.json'),{'ok':True})
    assert json.loads((tmp_path/'state/snapshot.json').read_text())=={'ok':True}


def test_loop_rechecks_lease_after_poll(tmp_path,monkeypatch):
    """A revoked lease cannot append the shell tick log after filtering."""
    import shutil
    setup(tmp_path,monkeypatch)
    # Retain the installed script/module layout while substituting only poll.
    repo=tmp_path/'copy';watcher=repo/'pr-babysit';watcher.mkdir(parents=True)
    kitbin=repo/'super-agent/bin';kitbin.mkdir(parents=True)
    shutil.copyfile(Path(config.writer_lease().__file__),kitbin/'writer_lease.py')
    shutil.copyfile(Path(config.writer_lease().__file__).with_name('workplace.py'),kitbin/'workplace.py')
    shutil.copyfile(Path(config.writer_lease().__file__).parents[1]/'workplace.json',kitbin.parent/'workplace.json')
    for name in ('tick_loop.sh','config.py','digest_store.py','wake_rules.py'):
        shutil.copyfile(SOURCE/name,watcher/name)
    (watcher/'poll.py').write_text('''import json,os
from pathlib import Path
Path(os.environ['SUPER_AGENT_STATE_DIR'],'WRITER').unlink()
print(json.dumps({'polled_at':'2026-11-13T00:00Z','errors':[]}))
''')
    e={'PATH':'/usr/bin:/bin','PYTHON':sys.executable,'PR_WATCH_CONFIG':str(tmp_path/'config.json'),'SUPER_AGENT_STATE_DIR':str(tmp_path/'kitstate'),'PYTHONDONTWRITEBYTECODE':'1','RETRY_DELAY':'0','INTERVAL':'0','CAO_WORKPLACE_CONFIG':os.environ['CAO_WORKPLACE_CONFIG']}
    result=subprocess.run(['bash',str(watcher/'tick_loop.sh')],env=e,capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'filter_error' in result.stdout
    assert not (tmp_path/'log.md').exists()
    # Revoked hosts do not remove state locks; the operator resolves this after stopping writers.
    assert (tmp_path/'state/tick_loop.lock').exists()


@pytest.mark.parametrize('phase',['tick','retry','log'])
def test_each_loop_check_stops_at_lease_loss(tmp_path,monkeypatch,phase):
    """Stubs intentionally bypass Python fences, isolating each shell check."""
    import shutil
    setup(tmp_path,monkeypatch)
    repo=tmp_path/'copy';watcher=repo/'pr-babysit';watcher.mkdir(parents=True)
    kitbin=repo/'super-agent/bin';kitbin.mkdir(parents=True)
    shutil.copyfile(Path(config.writer_lease().__file__),kitbin/'writer_lease.py')
    shutil.copyfile(Path(config.writer_lease().__file__).with_name('workplace.py'),kitbin/'workplace.py')
    shutil.copyfile(Path(config.writer_lease().__file__).parents[1]/'workplace.json',kitbin.parent/'workplace.json')
    for name in ('tick_loop.sh','config.py','wake_rules.py'):
        shutil.copyfile(SOURCE/name,watcher/name)
    (watcher/'digest_store.py').write_text('''import os,sys
from pathlib import Path
phase=os.environ['TEST_PHASE']
if sys.argv[1]=='--replay':
    if phase=='tick': Path(os.environ['SUPER_AGENT_STATE_DIR'],'WRITER').unlink()
    print('QUIET')
else:
    if phase=='log': Path(os.environ['SUPER_AGENT_STATE_DIR'],'WRITER').unlink()
    print('QUIET\\t'+sys.argv[1]+'\\tquiet')
''')
    (watcher/'poll.py').write_text('''import os,json
from pathlib import Path
p=Path('poll-count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
print(json.dumps({'polled_at':'2026-11-13T00:00:00Z','errors':[{'error':'retry'}] if os.environ['TEST_PHASE']=='retry' else []}))
''')
    # Retry sleep is the point at which a different process can release the lease.
    fakebin=tmp_path/'fakebin';fakebin.mkdir()
    sleeper=fakebin/'sleep';sleeper.write_text('#!/bin/sh\n'+sys.executable+' -c "import os;from pathlib import Path;Path(os.environ[\'SUPER_AGENT_STATE_DIR\'],\'WRITER\').unlink()"\n');sleeper.chmod(0o755)
    e={'PATH':str(fakebin)+':/usr/bin:/bin','PYTHON':sys.executable,'PR_WATCH_CONFIG':str(tmp_path/'config.json'),'SUPER_AGENT_STATE_DIR':str(tmp_path/'kitstate'),'PYTHONDONTWRITEBYTECODE':'1','RETRY_DELAY':'0','INTERVAL':'0','CAO_WORKPLACE_CONFIG':os.environ['CAO_WORKPLACE_CONFIG'],'TEST_PHASE':phase}
    result=subprocess.run(['bash',str(watcher/'tick_loop.sh')],env=e,capture_output=True,text=True,timeout=10)
    assert result.returncode==2 and 'WAKE none :: not_writer' in result.stdout,result.stdout+result.stderr
    count=tmp_path/'poll-count'
    assert not count.exists() if phase=='tick' else count.read_text()=='1'
    assert not (tmp_path/'log.md').exists()


@pytest.mark.parametrize('op',['atomic','save','log','loop_lock','loop_unlock'])
def test_release_waits_for_watcher_write(tmp_path,monkeypatch,op):
    import io
    import time
    setup(tmp_path,monkeypatch)
    lease_script=Path(config.writer_lease().__file__)
    release=None
    env={'PATH':'/usr/bin:/bin','SUPER_AGENT_STATE_DIR':str(tmp_path/'kitstate'),'PYTHONDONTWRITEBYTECODE':'1'}
    def attempt_release():
        nonlocal release
        started=tmp_path/'release-started'
        code="""import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('lease_test',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
Path(sys.argv[2]).write_text('started');sys.exit(m.main(['release']))
"""
        release=subprocess.Popen([sys.executable,'-I','-c',code,str(lease_script),str(started)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+5
        while not started.exists() and release.poll() is None and time.monotonic()<deadline: time.sleep(.01)
        assert started.exists()
        time.sleep(.1)
        assert release.poll() is None,'release passed an in-flight watcher write'
    if op in ('atomic','save'):
        real=os.replace
        def replace(src,dst):
            attempt_release();return real(src,dst)
        monkeypatch.setattr(os,'replace',replace)
    elif op=='log':
        real=os.fsync
        def fsync(fd):
            attempt_release();return real(fd)
        monkeypatch.setattr(os,'fsync',fsync)
        monkeypatch.setattr(sys,'stdin',io.StringIO('tick\n'))
    elif op=='loop_lock':
        real=Path.write_text
        def write(path,*args,**kwargs):
            if path.name=='pid': attempt_release()
            return real(path,*args,**kwargs)
        monkeypatch.setattr(Path,'write_text',write)
    else:
        lock=tmp_path/'state/tick_loop.lock';lock.mkdir();(lock/'pid').write_text('123\n')
        real=Path.unlink
        def unlink(path,*args,**kwargs):
            if path.name=='pid': attempt_release()
            return real(path,*args,**kwargs)
        monkeypatch.setattr(Path,'unlink',unlink)
    try:
        if op=='atomic': digest_store.atomic(tmp_path/'digest.md','data')
        elif op=='save': poll.save(str(tmp_path/'state/snapshot.json'),{})
        else: config.loop_mutation({'log':'--append-log','loop_lock':'--loop-lock','loop_unlock':'--loop-unlock'}[op],'123')
    finally:
        if release is not None: out,err=release.communicate(timeout=5)
    assert release is not None and release.returncode==0
    assert out.strip()=='RELEASED WRITER' and not (tmp_path/'kitstate/WRITER').exists()


def test_loop_log_append_uses_state_write_destination_lock(tmp_path,monkeypatch):
    """An atomic helper must not replace a log inode while the loop appends to it."""
    import fcntl
    import time
    setup(tmp_path,monkeypatch)
    path=tmp_path/'log.md';path.write_text('first\n')
    env={'PATH':'/usr/bin:/bin','PR_WATCH_CONFIG':str(tmp_path/'config.json'),
         'SUPER_AGENT_STATE_DIR':str(tmp_path/'kitstate'),'PYTHONDONTWRITEBYTECODE':'1'}
    with open(str(path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        process=subprocess.Popen([sys.executable,'-I',str(SOURCE/'config.py'),'--append-log'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            process.stdin.write('loop\n');process.stdin.close();process.stdin=None
            # Wait until the process has started and tried to append. A correct writer waits
            # on log.md.lock; the mutant can append immediately to the soon-replaced inode.
            time.sleep(.25)
            assert process.poll() is None,'loop bypassed the atomic helper destination lock'
            assert path.read_text()=='first\n'
            replacement=tmp_path/'replacement';replacement.write_text('first\nhelper\n')
            os.replace(replacement,path)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            out,err=process.communicate(timeout=5)
    assert process.returncode==0,out+err
    assert path.read_text()=='first\nhelper\nloop\n'
