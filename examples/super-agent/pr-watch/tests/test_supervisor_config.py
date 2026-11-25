"""tick_supervisor.sh takes its paths from its own folder and the CAO API from the workplace config."""
import json
import shutil
import subprocess

from conftest import SOURCE

KIT_BIN = SOURCE.parent / 'bin'  # pr-watch sits inside the kit


def tree(tmp_path, defaults):
    wp = tmp_path / 'elsewhere' / 'wp'
    (wp / 'pr-watch' / 'state').mkdir(parents=True)
    (wp / 'bin').mkdir(parents=True)
    shutil.copyfile(SOURCE / 'tick_supervisor.sh', wp / 'pr-watch' / 'tick_supervisor.sh')
    shutil.copyfile(KIT_BIN / 'workplace.py', wp / 'bin' / 'workplace.py')
    (wp / 'workplace.json').write_text(defaults)
    return wp


def run(wp, **env):
    base = {'PATH': '/usr/bin:/bin', 'SHEPHERD_ID': 'aaaa1111', 'SENDER_ID': 'bbbb2222',
            'CAO_WORKPLACE_CONFIG': str(wp / 'none.json')}
    base.update(env)
    return subprocess.run(['bash', str(wp / 'pr-watch' / 'tick_supervisor.sh')], env=base,
                          capture_output=True, text=True, timeout=20)


def test_unreadable_config_stops_before_any_state_write(tmp_path):
    wp = tree(tmp_path, '{not json')
    r = run(wp)
    assert r.returncode == 2 and r.stdout.startswith('STOP: the workplace config is unreadable')
    assert list((wp / 'pr-watch' / 'state').iterdir()) == []


def test_non_loopback_api_in_the_config_is_refused(tmp_path):
    doc = json.loads((SOURCE.parent / 'workplace.json').read_text())
    doc['cao_api'] = 'http://10.1.2.3:9889'
    wp = tree(tmp_path, json.dumps(doc))
    r = run(wp)
    assert r.returncode == 2 and 'STOP' in r.stdout
    assert list((wp / 'pr-watch' / 'state').iterdir()) == []


def test_no_hardcoded_workplace_path():
    text = (SOURCE / 'tick_supervisor.sh').read_text()
    assert '/Users/' not in text and '9889' not in text
