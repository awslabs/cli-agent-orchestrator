"""Cooperative host fence: refused writes leave all state bytes and paths unchanged."""
import json
import socket
import pytest
from conftest import KIT, base_env, run
from state_helpers import load, BASELINE, HIST_ROW

STAMP='2026-11-13T00:00:00Z'


def provision(tmp_path,monkeypatch,kind='right'):
    state=tmp_path/'lease-state';state.mkdir()
    lease=state/'WRITER'
    lease.write_text(f'host={socket.gethostname()} since={STAMP}\n')
    if kind=='wrong': lease.write_text(f'host=other-host since={STAMP}\n')
    if kind=='missing': lease.unlink()
    if kind=='missing_state': lease.unlink();state.rmdir()
    if kind=='state_file': lease.unlink();state.rmdir();state.write_text('fenced')
    if kind=='malformed': lease.write_text('invalid\n')
    if kind=='unreadable': lease.unlink();lease.mkdir()
    monkeypatch.setenv('SUPER_AGENT_STATE_DIR',str(state))
    return state


def snapshot(state):
    if not state.exists(): return None
    if state.is_file(): return state.read_bytes()
    return {str(p.relative_to(state)): p.read_bytes() if p.is_file() else None for p in state.rglob('*')}


@pytest.mark.parametrize('kind',['wrong','missing','missing_state','state_file','malformed','unreadable'])
@pytest.mark.parametrize('writer',['go','outbox','holds','automerge','append','plan','state_write'])
def test_every_state_writer_refuses(tmp_path,monkeypatch,kind,writer):
    state=provision(tmp_path,monkeypatch,kind);before=snapshot(state)
    SS=load('state_schema')
    def write():
        if writer=='go': load('go_ledger').append({})
        elif writer=='outbox':
            with load('outbox').locked(): pass
        elif writer=='holds': load('holds').write_cleared(str(state/'holds.json'),{})
        elif writer=='automerge': load('automerge_gate').write_hold(str(state/'holds.json'),1,('clear',None))
        elif writer=='append': SS.append_row(str(state/'history.jsonl'),HIST_ROW,lambda p,r:None)
        elif writer=='state_write': load('state_write').write(str(state/'example'),b'data')
        elif writer=='plan': SS.append_plan_row(str(state/'plan-approvals.jsonl'),BASELINE)
        elif writer=='heavy':
            cfg=json.loads((KIT/'resources.json').read_text())
            load('resource_gate').acquire('test','GO',[],cfg)
    with pytest.raises(ValueError,match='not_writer'): write()
    assert snapshot(state)==before


def test_right_host_can_append_and_read(tmp_path,monkeypatch):
    state=provision(tmp_path,monkeypatch)
    SS=load('state_schema');SS.append_plan_row(str(state/'plan-approvals.jsonl'),BASELINE)
    assert SS.load_plan_ledger(str(state/'plan-approvals.jsonl'))==[BASELINE]


@pytest.mark.parametrize('kind',['wrong','missing','missing_state'])
def test_cli_writers_return_clear_error(tmp_path,monkeypatch,kind):
    state=provision(tmp_path,monkeypatch,kind)
    e=base_env(tmp_path);e['SUPER_AGENT_STATE_DIR']=str(state)
    fake=tmp_path/'fake.json';fake.write_text(json.dumps({'pressure_level':1,'free_memory_pct':60,'swap_used_gib':0,'disk_free_gib':100,'load1':1,'ncpu':10,'live_terminals':1}));e['SUPER_AGENT_FAKE_MEASUREMENTS']=str(fake)
    calls=[('state_schema.py',['append-plan-row','--row',json.dumps(BASELINE)]),('go_ledger.py',['revoke','--writer-role','conductor','--id','test','--by','test'])]
    e['CAO_TERMINAL_ID']='c0ffee01';before=snapshot(state)
    for name,args in calls:
        r=run(name,*args,env=e)
        assert r.returncode!=0 and 'not_writer' in r.stdout and 'Traceback' not in r.stderr
        assert snapshot(state)==before


@pytest.mark.parametrize('kind',['wrong','missing','missing_state'])
def test_read_only_tools_do_not_need_lease(tmp_path,monkeypatch,kind):
    state=provision(tmp_path,monkeypatch,kind);before=snapshot(state)
    e=base_env(tmp_path);e['SUPER_AGENT_STATE_DIR']=str(state)
    for name,args in [('outbox.py',['check','0'*64]),('state_schema.py',['check']),('go_ledger.py',['next-id','--pr','1','--action','arm_automerge','--head','a'*40])]:
        r=run(name,*args,env=e)
        assert r.returncode==0 and 'not_writer' not in r.stdout
        assert snapshot(state)==before


def lease(tmp_path,*args):
    e={'PATH':'/usr/bin:/bin','SUPER_AGENT_STATE_DIR':str(tmp_path/'state'),'PYTHONDONTWRITEBYTECODE':'1'}
    return run('writer_lease.py',*args,env=e)


def test_claim_release_and_force(tmp_path):
    state=tmp_path/'state';state.mkdir()
    assert lease(tmp_path,'show').returncode!=0
    local=socket.gethostname()
    assert lease(tmp_path,'claim','--host',local).returncode==0
    original=(state/'WRITER').read_bytes()
    assert lease(tmp_path,'claim','--host',local).returncode==0
    assert (state/'WRITER').read_bytes()==original
    r=lease(tmp_path,'claim','--host','other-host')
    assert r.returncode!=0 and 'not_writer' in r.stdout and (state/'WRITER').read_bytes()==original
    r=lease(tmp_path,'claim','--host','other-host','--force')
    assert r.returncode==0 and 'WARNING' in r.stderr
    r=lease(tmp_path,'release')
    assert r.returncode!=0 and 'not_writer' in r.stdout and (state/'WRITER').exists()
    assert lease(tmp_path,'claim','--host',local,'--force').returncode==0
    assert lease(tmp_path,'release').returncode==0 and not (state/'WRITER').exists()


def test_claim_does_not_create_state(tmp_path):
    r=lease(tmp_path,'claim','--host',socket.gethostname())
    assert r.returncode!=0 and 'not_writer' in r.stdout and not (tmp_path/'state').exists()


def test_malformed_claim_requires_force(tmp_path):
    state=tmp_path/'state';state.mkdir();(state/'WRITER').write_text('bad\n')
    assert lease(tmp_path,'claim','--host',socket.gethostname()).returncode!=0
    r=lease(tmp_path,'claim','--host',socket.gethostname(),'--force')
    assert r.returncode==0 and 'WARNING' in r.stderr


def test_remap_refuses_before_writing_watch(tmp_path,monkeypatch):
    state=provision(tmp_path,monkeypatch,'wrong');p=tmp_path/'watch.json';p.write_text('{"prs":{}}')
    e=base_env(tmp_path);e['SUPER_AGENT_STATE_DIR']=str(state)
    r=run('remap_owners.py','--watch',p,'--from','aaaaaaaa','--to','bbbbbbbb','--session','s',env=e)
    assert r.returncode!=0 and 'not_writer' in r.stdout and p.read_text()=='{"prs":{}}'


@pytest.mark.parametrize('kind',['missing_state','state_file'])
def test_require_state_refuses(tmp_path,monkeypatch,kind):
    state=provision(tmp_path,monkeypatch,kind)
    with pytest.raises(ValueError,match='not_writer state_missing_or_not_directory'):
        load('writer_lease').require_state(str(state))


@pytest.mark.parametrize('raw', ['bad\n','host=local since=2026-11-13T00:00:00\n','host=local since=2026-11-13T00:00:00+01:00\n','host=local since=bogus\n'])
def test_malformed_lease_refuses(tmp_path,monkeypatch,raw):
    state=provision(tmp_path,monkeypatch);(state/'WRITER').write_text(raw)
    with pytest.raises(ValueError,match='not_writer WRITER_malformed'):
        load('writer_lease').read_lease(str(state))


@pytest.mark.parametrize('host',['has space','has\nnewline','host=bad',''])
def test_invalid_claim_host_refuses(tmp_path,host):
    (tmp_path/'state').mkdir()
    r=lease(tmp_path,'claim','--host',host)
    assert r.returncode!=0 and 'not_writer invalid_host' in r.stdout
    assert not (tmp_path/'state/WRITER').exists()


def test_competing_claims_allow_only_one_host(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    WL=load('writer_lease');state=tmp_path/'state';state.mkdir()
    import threading
    caller=threading.local()
    monkeypatch.setattr(WL.socket,'gethostname',lambda:caller.host)
    def attempt(host):
        caller.host=host
        try: WL.claim(host,path=str(state));return True
        except ValueError as exc: assert 'not_writer' in str(exc);return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,['host-a','host-b']))
    assert sum(results)==1
    assert WL.read_lease(str(state))[0] in ('host-a','host-b')


@pytest.mark.parametrize('kind',['right','wrong','missing','missing_state'])
@pytest.mark.parametrize('tool',['holds_body','api_envelope'])
def test_artifact_output_into_state_is_fenced(tmp_path,monkeypatch,kind,tool):
    state=provision(tmp_path,monkeypatch,kind)
    src=tmp_path/'source';out=state/'artifact.json'
    if tool=='holds_body':
        src.write_text('[{"id":7,"body":"canonical body"}]')
        script,args='holds.py',['body','--from',src,'--item','review:7','--out',out]
    else:
        src.write_text('HTTP/2 200 OK\n\n{}')
        script,args='api_envelope.py',['--endpoint','repos/o/r/rules','--raw',src,'--out',out]
    e=base_env(tmp_path);e['SUPER_AGENT_STATE_DIR']=str(state);before=snapshot(state)
    r=run(script,*args,env=e)
    if kind=='right':assert r.returncode==0 and out.exists()
    else:
        assert r.returncode!=0 and 'not_writer' in r.stdout and 'Traceback' not in r.stderr
        assert snapshot(state)==before


@pytest.mark.parametrize('writer',['outbox','holds','automerge'])
def test_rechecks_lease_after_waiting_for_state_lock(tmp_path,monkeypatch,writer):
    import fcntl
    state=provision(tmp_path,monkeypatch)
    real=fcntl.flock
    def revoke_at_lock(fd,operation):
        real(fd,operation)
        if operation & fcntl.LOCK_EX and (state/'WRITER').exists():
            (state/'WRITER').unlink()
    monkeypatch.setattr(fcntl,'flock',revoke_at_lock)
    with pytest.raises(ValueError,match='not_writer'):
        if writer=='outbox':
            with load('outbox').locked(): pass
        elif writer=='holds': load('holds').write_cleared(str(state/'holds.json'),{})
        elif writer=='automerge': load('automerge_gate').write_hold(str(state/'holds.json'),1,('clear',None))
        else: load('resource_gate').acquire('test','GO',[],json.loads((KIT/'resources.json').read_text()))
    assert not (state/'holds.json').exists() and not (state/'heavy/active').exists()
