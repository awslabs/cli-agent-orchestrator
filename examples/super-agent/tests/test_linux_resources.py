"""Linux probes and unchanged macOS CLI contract; uses scratch /proc, no host probes."""
import contextlib
import io
import json
from types import SimpleNamespace
import pytest
from conftest import KIT
from state_helpers import load


def linux(tmp_path, monkeypatch, some=0.1, full=0.0, available=32, missing=None):
    rg=load('resource_gate')
    cfg=json.loads((KIT/'resources.linux.json').read_text())
    (tmp_path/'pressure').mkdir(exist_ok=True)
    (tmp_path/'pressure/memory').write_text(f'some avg10={some} avg60=0.00 avg300=0.00 total=1\nfull avg10={full} avg60=0.00 avg300=0.00 total=1\n')
    (tmp_path/'meminfo').write_text(f'MemAvailable: {int(available*1024**2)} kB\nSwapTotal: 1048576 kB\nSwapFree: 524288 kB\n')
    if missing: (tmp_path/missing).unlink()
    monkeypatch.setattr(rg.sys,'platform','linux')
    monkeypatch.setattr(rg,'PROC_ROOT',str(tmp_path))
    monkeypatch.setattr(rg.os,'getloadavg',lambda:(1,1,1))
    monkeypatch.setattr(rg.os,'cpu_count',lambda:16)
    paths=[]
    monkeypatch.setattr(rg.shutil,'disk_usage',lambda p:paths.append(p) or SimpleNamespace(free=100*1024**3))
    monkeypatch.setattr(rg,'count_live_terminals',lambda *a:(1,None))
    monkeypatch.setattr(rg,'_run',lambda *a:pytest.fail('macOS probe called on Linux'))
    return rg,cfg,paths


@pytest.mark.parametrize('some,full,available,verdict',[(0,0,32,'GO'),(10,10,12,'GO'),(10.01,0,32,'THROTTLE'),(0,10.01,32,'STOP'),(0,0,11.99,'THROTTLE'),(0,0,3.99,'STOP'),(0,0,4,'THROTTLE')])
def test_linux_probes(tmp_path,monkeypatch,some,full,available,verdict):
    rg,cfg,paths=linux(tmp_path,monkeypatch,some,full,available)
    measurements,errors=rg.measure(cfg)
    assert rg.decide(measurements,cfg,errors)[0]==verdict
    assert measurements['swap_used_gib']==0.5 and paths==['/home', '/tmp']


@pytest.mark.parametrize('missing',['pressure/memory','meminfo'])
def test_missing_linux_probe_throttles(tmp_path,monkeypatch,missing):
    rg,cfg,_=linux(tmp_path,monkeypatch,missing=missing)
    m,errors=rg.measure(cfg); verdict,reasons=rg.decide(m,cfg,errors)
    assert verdict=='THROTTLE' and any('unmeasured:' in r for r in reasons)


@pytest.mark.parametrize('path,text',[('pressure/memory','some avg10=nan\nfull avg10=0\n'),('pressure/memory','some avg10=-1\nfull avg10=0\n'),('meminfo','MemAvailable: 1 MB\n'),('meminfo','MemAvailable: 10 kB\nSwapTotal: 1 kB\nSwapFree: 2 kB\n')])
def test_malformed_linux_probe_throttles(tmp_path,monkeypatch,path,text):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    (tmp_path/path).write_text(text)
    m,errors=rg.measure(cfg)
    assert rg.decide(m,cfg,errors)[0]=='THROTTLE'


def test_unreadable_linux_probe_throttles(tmp_path,monkeypatch):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    (tmp_path/'pressure/memory').unlink();(tmp_path/'pressure/memory').mkdir()
    m,errors=rg.measure(cfg)
    assert rg.decide(m,cfg,errors)[0]=='THROTTLE'


@pytest.mark.parametrize('terms,verdict',[(15,'GO'),(16,'THROTTLE'),(23,'THROTTLE'),(24,'STOP')])
def test_linux_terminal_thresholds(tmp_path,monkeypatch,terms,verdict):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    m,e=rg.measure(cfg);m['live_terminals']=terms
    assert rg.decide(m,cfg,e)[0]==verdict


@pytest.mark.parametrize('tmp_free,expected,limit',[(14,'GO',None),(5,'THROTTLE',6),(2,'STOP',3)])
def test_linux_disk_paths_use_each_path_limit(tmp_path,monkeypatch,tmp_free,expected,limit):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    monkeypatch.setattr(rg.shutil,'disk_usage',
                        lambda p: SimpleNamespace(free=(tmp_free if p == '/tmp' else 159) * 1024**3))
    m,e=rg.measure(cfg)
    actual,reasons=rg.decide(m,cfg,e)
    assert m['disk_free_gib']==tmp_free
    assert actual==expected
    if limit:
        assert f'disk_free_gib={tmp_free:.1f}<{limit} (/tmp)' in reasons


def test_linux_users_path_keeps_its_rules(tmp_path,monkeypatch):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    monkeypatch.setattr(rg.shutil,'disk_usage',
                        lambda p: SimpleNamespace(free=(19 if p == '/home' else 14) * 1024**3))
    m,e=rg.measure(cfg)
    verdict,reasons=rg.decide(m,cfg,e)
    assert verdict=='STOP' and 'disk_free_gib=19.0<20 (/home)' in reasons


def test_linux_single_disk_path_remains_supported(tmp_path,monkeypatch):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    cfg['disk_path']='/home'
    cfg.pop('disk_paths')
    cfg['stop']['disk_free_gib_lt']=20
    cfg['throttle']['disk_free_gib_lt']=40
    m,e=rg.measure(cfg)
    assert m['disk_free_gib']==100
    assert rg.decide(m,cfg,e)[0]=='GO'


def test_linux_missing_disk_path_is_unmeasured(tmp_path,monkeypatch):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    missing=tmp_path/'missing-disk'
    cfg['disk_paths']={str(missing): {'stop_lt': 3, 'throttle_lt': 6}}
    def disk_usage(path):
        if path == str(missing):
            raise OSError('missing')
        return SimpleNamespace(free=100 * 1024**3)
    monkeypatch.setattr(rg.shutil,'disk_usage',disk_usage)
    m,e=rg.measure(cfg)
    verdict,reasons=rg.decide(m,cfg,e)
    assert verdict=='THROTTLE'
    assert f'unmeasured:disk_free_gib ({missing})' in reasons


def test_linux_config_and_override(tmp_path,monkeypatch,capsys):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    monkeypatch.setattr(rg,'measure',lambda c:({'memory_psi_some_avg10':0,'memory_psi_full_avg10':0,'memory_available_gib':32,'swap_used_gib':0,'disk_free_gib':100,'disk_free_gib_by_path':{'/home':100,'/tmp':100},'load1':1,'ncpu':16,'live_terminals':16},[]))
    assert rg.main([])==3
    cfg['throttle']['live_terminals_gte']=17
    p=tmp_path/'resources.json';p.write_text(json.dumps(cfg))
    assert rg.main(['--resources',str(p)])==0


def test_macos_golden_output(monkeypatch):
    rg=load('resource_gate');monkeypatch.setattr(rg.sys,'platform','darwin')
    for row in json.loads((KIT/'tests/fixtures/macos-resource-golden.json').read_text()):
        m={k:float('nan') if v=='NaN' else v for k,v in row['input'].items()}
        monkeypatch.setattr(rg,'measure',lambda cfg:(m,[]))
        out=io.StringIO()
        with contextlib.redirect_stdout(out): rc=rg.main(row['flags'])
        assert (rc,out.getvalue())==(row['rc'],row['stdout'])


def test_macos_same_probes(monkeypatch):
    rg=load('resource_gate');monkeypatch.setattr(rg.sys,'platform','darwin')
    calls=[]
    def probe(argv,timeout):
        calls.append((argv,timeout))
        return {'kern.memorystatus_vm_pressure_level':'1','vm.swapusage':'used = 1024.00M','memory_pressure':'free percentage: 60%'}[argv[-1]]
    monkeypatch.setattr(rg,'_run',probe)
    monkeypatch.setattr(rg,'_disk_gib',lambda p:100)
    monkeypatch.setattr(rg,'count_live_terminals',lambda *a:(1,None))
    cfg=json.loads((KIT/'resources.json').read_text());m,e=rg.measure(cfg)
    assert m['pressure_level']==1 and m['free_memory_pct']==60 and m['swap_used_gib']==1
    assert calls==[(['sysctl','-n','kern.memorystatus_vm_pressure_level'],5),(['memory_pressure'],20),(['sysctl','-n','vm.swapusage'],5)]


@pytest.mark.parametrize('some,full',[(101,0),(0,101)])
def test_out_of_range_psi_throttles(tmp_path,monkeypatch,some,full):
    rg,cfg,_=linux(tmp_path,monkeypatch,some=some,full=full)
    m,e=rg.measure(cfg)
    assert rg.decide(m,cfg,e)[0]=='THROTTLE'
    assert m['memory_psi_some_avg10'] is None and m['memory_psi_full_avg10'] is None


def test_linux_thresholds_are_configurable(tmp_path,monkeypatch):
    rg,cfg,_=linux(tmp_path,monkeypatch,some=11,full=11,available=8)
    cfg['throttle']['memory_psi_some_avg10_gt']=20
    cfg['stop']['memory_psi_full_avg10_gt']=20
    cfg['throttle']['memory_available_gib_lt']=6
    m,e=rg.measure(cfg)
    assert rg.decide(m,cfg,e)==('GO',[])


def test_linux_config_matches_box_and_common_rules():
    linux_cfg=json.loads((KIT/'resources.linux.json').read_text())
    mac=json.loads((KIT/'resources.json').read_text())
    assert linux_cfg['stop']=={'memory_psi_full_avg10_gt':10,'memory_available_gib_lt':4,'live_terminals_gte':24}
    assert linux_cfg['throttle']=={'memory_psi_some_avg10_gt':10,'memory_available_gib_lt':12,'swap_used_gib_gt':16,'load_per_cpu_gt':1.2,'live_terminals_gte':16}
    assert linux_cfg['disk_paths']=={'/home': {'stop_lt': 20, 'throttle_lt': 40},
                                     '/tmp': {'stop_lt': 3, 'throttle_lt': 6}}
    assert linux_cfg['disk_paths']['/home']['stop_lt']==mac['stop']['disk_free_gib_lt']
    assert linux_cfg['disk_paths']['/home']['throttle_lt']==mac['throttle']['disk_free_gib_lt']
    assert linux_cfg['throttle']['load_per_cpu_gt']==mac['throttle']['load_per_cpu_gt']


@pytest.mark.parametrize('key',['memory_psi_some_avg10','memory_psi_full_avg10','memory_available_gib','swap_used_gib','disk_free_gib','load1','ncpu','live_terminals'])
@pytest.mark.parametrize('value',[None,float('nan'),True,-1,'bad'])
def test_linux_invalid_measurement_is_closed(tmp_path,monkeypatch,key,value):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    m,e=rg.measure(cfg);m[key]=value
    if key=='disk_free_gib':
        m['disk_free_gib_by_path']={path:value for path in cfg['disk_paths']}
    verdict,reasons=rg.decide(m,cfg,e)
    assert verdict=='THROTTLE' and any(key in r for r in reasons)


@pytest.mark.parametrize('key,value,verdict',[('swap_used_gib',16,'GO'),('swap_used_gib',16.1,'THROTTLE'),('disk_free_gib',19.9,'STOP'),('disk_free_gib',20,'THROTTLE'),('disk_free_gib',39.9,'THROTTLE'),('disk_free_gib',40,'GO'),('load1',19.2,'GO'),('load1',19.2001,'THROTTLE')])
def test_linux_common_rules_match_macos(tmp_path,monkeypatch,key,value,verdict):
    rg,cfg,_=linux(tmp_path,monkeypatch)
    m,e=rg.measure(cfg);m[key]=value
    if key=='disk_free_gib':
        m['disk_free_gib_by_path']={'/home':value,'/tmp':100}
    assert rg.decide(m,cfg,e)[0]==verdict
