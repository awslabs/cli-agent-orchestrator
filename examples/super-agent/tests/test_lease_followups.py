"""Review 24b regressions, exercised against real tools and scratch state."""
import json
import socket
import subprocess
import sys
import time

import pytest
from conftest import KIT, BIN, base_env, run
from state_helpers import load, HIST_ROW
import test_check_mandate as CM
import test_automerge_gate as AG


def launch_release(tmp_path):
    started=tmp_path/'release-started'
    started.unlink(missing_ok=True)
    code="""import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('release_test',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
Path(sys.argv[2]).write_text('started')
sys.exit(m.main(['release']))
"""
    process=subprocess.Popen([sys.executable,'-I','-c',code,str(BIN/'writer_lease.py'),str(started)],env=base_env(tmp_path),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+5
    while not started.exists() and process.poll() is None and time.monotonic()<deadline:
        time.sleep(.01)
    assert started.exists(),'release process did not start'
    return process


def foreign(tmp_path):
    (tmp_path/'state/WRITER').write_text('host=other-host since=2026-11-13T00:00:00Z\n')


@pytest.mark.parametrize('action', ['ff_fix_push', 'main_merge_push', 'factual_fix_reply',
    'rerun_failed_outage', 'arm_automerge', 'update_branch', 'approve_review_plan'])
def test_all_standing_actions_need_writer(tmp_path, action):
    data=CM.act(CM.doc())
    data['mandates'][0]['action']=action
    assert CM.check(tmp_path,data,action=action)==(0,'ALLOW M1')
    foreign(tmp_path)
    rc,out=CM.check(tmp_path,data,action=action)
    assert rc==1 and out.startswith('DENY not_writer'),out


@pytest.mark.parametrize('case',['go','policy','head'])
def test_review_named_allow_paths_need_writer(tmp_path,case):
    data=CM.doc() if case=='go' else CM.act(CM.doc())
    args=CM.GO_ARGS if case=='go' else ('--expect-policy-version','test-1') if case=='policy' else ('--expect-head',CM.H1,'--remote-head',CM.H1,*CM.PIN)
    rows=[CM.go_row()] if case=='go' else None
    assert CM.recheck(tmp_path,data,*args,go_rows=rows)[0]==0
    foreign(tmp_path)
    rc,out=CM.recheck(tmp_path,data,*args,go_rows=rows)
    assert rc==1 and out.startswith('DENY not_writer'),out


@pytest.mark.parametrize('action,state',[('arm','CLEAN'),('arm','BEHIND'),('update_branch','BEHIND')])
def test_automerge_outward_actions_need_writer(tmp_path,action,state):
    assert AG.gate(tmp_path,AG.pr(mergeStateStatus=state),[AG.review()],action=action)[0]==0
    foreign(tmp_path)
    rc,out=AG.gate(tmp_path,AG.pr(mergeStateStatus=state),[AG.review()],action=action)
    assert rc==1 and out.startswith('DENY not_writer'),out


def test_observe_and_wait_available_to_reader(tmp_path):
    foreign(tmp_path)
    assert CM.check(tmp_path,CM.doc())==(3,'OBSERVE M1')
    assert AG.gate(tmp_path,AG.pr(reviewDecision=None),[])[0]==3


@pytest.mark.parametrize('variant',['prefix','suffix'])
def test_hostname_is_exact(tmp_path,variant):
    host=socket.gethostname()
    other=host[:-1] if variant=='prefix' else host+'.local'
    (tmp_path/'state/WRITER').write_text(f'host={other} since=2026-11-13T00:00:00Z\n')
    with pytest.raises(ValueError,match='not_writer'): load('writer_lease').require_writer()


def test_output_symlink_into_state_is_fenced(tmp_path):
    foreign(tmp_path)
    alias=tmp_path/'alias';alias.symlink_to(tmp_path/'state',target_is_directory=True)
    with pytest.raises(ValueError,match='not_writer'): load('state_schema').require_writer_for_output(alias/'artifact')


def test_claim_foreign_host_on_empty_state_refuses(tmp_path):
    (tmp_path/'state/WRITER').unlink()
    r=run('writer_lease.py','claim','--host','other-host',env=base_env(tmp_path))
    assert r.returncode==1 and 'not_writer' in r.stdout
    assert not (tmp_path/'state/WRITER').exists()


def test_show_says_this_host(tmp_path):
    assert 'this host: yes' in run('writer_lease.py','show',env=base_env(tmp_path)).stdout
    foreign(tmp_path)
    assert 'this host: no' in run('writer_lease.py','show',env=base_env(tmp_path)).stdout


def test_force_audit(tmp_path):
    foreign(tmp_path)
    e=base_env(tmp_path,CAO_TERMINAL_ID='test-operator')
    r=run('writer_lease.py','claim','--host',socket.gethostname(),'--force',env=e)
    assert r.returncode==0 and 'WARNING' in r.stderr
    row=json.loads((tmp_path/'state/WRITER.log').read_text().splitlines()[-1])
    assert row['from']=='other-host' and row['to']==socket.gethostname() and row['by']=='test-operator'
    assert row['time'].endswith('Z') and row['action']=='force'


def test_release_waits_for_inflight_write(tmp_path,monkeypatch):
    """Release must remain blocked after the writer's last lease check, until data lands."""
    SS=load('state_schema');release=None
    def check(path,row):
        nonlocal release
        release=launch_release(tmp_path)
        time.sleep(.15)
        assert release.poll() is None,'release passed an in-flight writer'
    try:
        SS.append_row(tmp_path/'state/history.jsonl',HIST_ROW,check)
    finally:
        if release is not None:
            stdout,stderr=release.communicate(timeout=5)
    assert release.returncode==0 and stdout.strip()=='RELEASED WRITER'
    assert (tmp_path/'state/history.jsonl').exists() and not (tmp_path/'state/WRITER').exists()


@pytest.mark.parametrize('platform',['','linux'])
@pytest.mark.parametrize('bad',['missing','unknown','string','bool','nan','huge','root','section','heavy','probe'])
def test_bad_resource_config_fails_closed(tmp_path,platform,bad):
    cfg=json.loads((KIT/('resources.linux.json' if platform else 'resources.json')).read_text())
    key=next(iter(cfg['stop']))
    if bad=='missing': del cfg['stop'][key]
    elif bad=='unknown': cfg['stop'][key+'_typo']=cfg['stop'].pop(key)
    elif bad=='string': cfg['stop'][key]='4'
    elif bad=='bool': cfg['stop'][key]=True
    elif bad=='nan': cfg['stop'][key]=float('nan')
    elif bad=='huge': cfg['stop'][key]=10**400
    elif bad=='root': cfg=[]
    elif bad=='section': cfg['throttle']=[]
    elif bad=='heavy': cfg['heavy']['stale_hours']='3'
    else: cfg['probe_timeouts']={'sysctl':'bad'}
    path=tmp_path/'resources.json';path.write_text(json.dumps(cfg))
    r=run('resource_gate.py','--resources',path,env=base_env(tmp_path))
    assert r.returncode==1 and r.stdout.startswith('STOP bad_config:') and 'Traceback' not in r.stderr,r.stdout+r.stderr


def test_linux_reason_does_not_round_across_boundary():
    RG=load('resource_gate');cfg=json.loads((KIT/'resources.linux.json').read_text())
    m={'memory_psi_some_avg10':0,'memory_psi_full_avg10':0,'memory_available_gib':11.99999,'swap_used_gib':0,'disk_free_gib':100,'load1':1,'ncpu':16,'live_terminals':1}
    assert RG.decide(m,cfg)==('THROTTLE',['memory_available_gib=11.99999<12'])


@pytest.mark.parametrize('kind',['wrong','absent'])
def test_heavy_slot_is_host_local(tmp_path,kind):
    if kind=='wrong': foreign(tmp_path)
    else: (tmp_path/'state/WRITER').unlink();(tmp_path/'state').rmdir()
    RG=load('resource_gate');cfg=json.loads((KIT/'resources.json').read_text())
    try:
        rc,line=RG.acquire('local-suite','GO',[],cfg)
    except ValueError as exc:
        assert False, f'host-local slot unexpectedly requires WRITER: {exc}'
    assert rc==0,line
    token=line.split('token=')[1].split()[0]
    assert RG.refresh(token,cfg)[0]==0
    assert RG.release(token,cfg)[0]==0
    assert not (tmp_path/'state/WRITER').exists() if kind=='absent' else (tmp_path/'state/WRITER').read_text().startswith('host=other-host ')


@pytest.mark.parametrize('op',['write','append','copy'])
def test_state_write_refuses_foreign_lease(tmp_path,op):
    foreign(tmp_path);src=tmp_path/'src';src.write_text('source')
    dst=tmp_path/'state/example';dst.write_text('old')
    args=['--out',dst,'--from',src]+(['--append'] if op=='append' else [])
    r=run('state_write.py',*args,env=base_env(tmp_path))
    assert r.returncode!=0 and 'not_writer' in r.stdout and dst.read_text()=='old'


def test_state_write_atomic_and_append(tmp_path):
    dst=tmp_path/'state/example';src=tmp_path/'src';src.write_text('first\n')
    e=base_env(tmp_path)
    assert run('state_write.py','--out',dst,'--from',src,env=e).returncode==0
    src.write_text('next\n')
    assert run('state_write.py','--out',dst,'--from',src,'--append',env=e).returncode==0
    assert dst.read_text()=='first\nnext\n'
    assert not list(dst.parent.glob('.state-write-*'))


def test_oneoff_and_clear_hold_need_writer(tmp_path):
    d=CM.act(CM.shipped())
    row=CM.go_row(id='101-clear_hold-x',action='clear_hold',policy_version=d['policy_version'],branch=None,new_head=None)
    args=('--go-id','101-clear_hold-x','--pr','101','--expect-head',CM.H1,'--remote-head',CM.H1)
    assert CM.recheck(tmp_path,d,*args,action='clear_hold',go_rows=[row])[0]==0
    assert CM.oneoff(tmp_path,CM.doc(),[CM.oneoff_row(body_sha256='AUTO')])[0]==0
    foreign(tmp_path)
    for rc,out in [CM.recheck(tmp_path,d,*args,action='clear_hold',go_rows=[row]),
                   CM.oneoff(tmp_path,CM.doc(),[CM.oneoff_row(body_sha256='AUTO')])]:
        assert rc==1 and out.startswith('DENY not_writer'),out


@pytest.mark.parametrize('section,key', [('stop','memory_available_gib_lt'),
    ('throttle','memory_psi_some_avg10_gt'), ('throttle','load_per_cpu_gt'),
    ('heavy','stale_hours'), ('heavy','lock_timeout_s')])
@pytest.mark.parametrize('bad',['missing','typo','infinite','negative'])
def test_config_every_section_validated(tmp_path,section,key,bad):
    cfg=json.loads((KIT/'resources.linux.json').read_text())
    if bad=='missing': del cfg[section][key]
    elif bad=='typo': cfg[section][key+'_typo']=cfg[section].pop(key)
    elif bad=='infinite': cfg[section][key]=float('inf')
    else: cfg[section][key]=-1
    path=tmp_path/'resources.json';path.write_text(json.dumps(cfg))
    result=run('resource_gate.py','--resources',path,env=base_env(tmp_path))
    assert result.returncode==1 and result.stdout.startswith('STOP bad_config:') and result.stderr.startswith("hint: ")


@pytest.mark.parametrize('field,value,reason',[('memory_psi_full_avg10',10.0000001,'memory_psi_full_avg10=10.0000001>10'),
    ('load1',19.2000001,'load_per_cpu=1.20000000625>1.2')])
def test_linux_reason_preserves_threshold_side(field,value,reason):
    RG=load('resource_gate');cfg=json.loads((KIT/'resources.linux.json').read_text())
    m={'memory_psi_some_avg10':0,'memory_psi_full_avg10':0,'memory_available_gib':12,'swap_used_gib':0,'disk_free_gib':100,'load1':1,'ncpu':16,'live_terminals':1}
    m[field]=value
    assert reason in RG.decide(m,cfg)[1]


@pytest.mark.parametrize('writer',['go','plan','holds','automerge','outbox','state_write'])
def test_release_waits_across_each_data_lock(tmp_path,monkeypatch,writer):
    """The lock order must be shared lease, then destination; verify with a real release."""
    import fcntl
    SS=load('state_schema');real=fcntl.flock;release=None
    def lock(fd,operation):
        nonlocal release
        result=real(fd,operation)
        if operation==fcntl.LOCK_EX:
            release=launch_release(tmp_path)
            time.sleep(.15)
            assert release.poll() is None,'release passed the data lock'
        return result
    monkeypatch.setattr(fcntl,'flock',lock)
    try:
        # Empty/invalid data is fine: the assertions target the critical section, not schema.
        if writer=='outbox':
            with load('outbox').locked(): pass
        elif writer=='state_write': load('state_write').write(tmp_path/'state/file',b'data')
        else:
            try:
                if writer=='go': load('go_ledger').append({})
                elif writer=='plan': SS.append_plan_row(tmp_path/'state/plan-approvals.jsonl',{})
                elif writer=='holds': load('holds').write_cleared(str(tmp_path/'state/holds.json'),{})
                else: load('automerge_gate').write_hold(str(tmp_path/'state/holds.json'),1,('clear',None))
            except ValueError as exc:
                assert not str(exc).startswith('not_writer'),str(exc)
    finally:
        if release is not None: stdout,stderr=release.communicate(timeout=5)
    assert release is not None and release.returncode==0,stderr
    assert not (tmp_path/'state/WRITER').exists()


def test_config_root_typo(tmp_path):
    cfg=json.loads((KIT/'resources.linux.json').read_text());cfg['thr0ttle']={}
    path=tmp_path/'resources.json';path.write_text(json.dumps(cfg))
    r=run('resource_gate.py','--resources',path,env=base_env(tmp_path))
    assert r.returncode==1 and r.stdout.startswith('STOP bad_config:root:unknown_key:')


def test_config_unknown_extra(tmp_path):
    cfg=json.loads((KIT/'resources.linux.json').read_text());cfg['stop']['typo']=0
    path=tmp_path/'resources.json';path.write_text(json.dumps(cfg))
    r=run('resource_gate.py','--resources',path,env=base_env(tmp_path))
    assert r.returncode==1 and r.stdout.startswith('STOP bad_config:stop:unknown_key:')


@pytest.mark.parametrize('key',['about','cao_url','disk_path'])
def test_config_metadata_type(tmp_path,key):
    cfg=json.loads((KIT/'resources.linux.json').read_text());cfg[key]=3
    path=tmp_path/'resources.json';path.write_text(json.dumps(cfg))
    r=run('resource_gate.py','--resources',path,env=base_env(tmp_path))
    assert r.returncode==1 and r.stdout.startswith('STOP bad_config:'+key+':not_text')


def test_writer_rechecks_after_shared_lock(tmp_path,monkeypatch):
    import fcntl
    WL=load('writer_lease');real=fcntl.flock
    def revoke(fd,operation):
        result=real(fd,operation)
        if operation==fcntl.LOCK_SH: (tmp_path/'state/WRITER').unlink()
        return result
    monkeypatch.setattr(fcntl,'flock',revoke)
    with pytest.raises(ValueError,match='not_writer'):
        with WL.writer_locked(): pass


def test_force_audit_failure_keeps_previous_lease(tmp_path):
    foreign(tmp_path);old=(tmp_path/'state/WRITER').read_bytes()
    (tmp_path/'state/WRITER.log').mkdir()
    result=run('writer_lease.py','claim','--host',socket.gethostname(),'--force',env=base_env(tmp_path))
    assert result.returncode==1 and 'not_writer lease_io_error' in result.stdout
    assert (tmp_path/'state/WRITER').read_bytes()==old
