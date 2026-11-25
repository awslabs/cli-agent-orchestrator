"""The operator's login comes from the workplace config, never from the code (portable bootstrap)."""
import json
import os
import subprocess
import sys

from conftest import SOURCE

REPORT = {'polled_at': '2026-11-17T00:00Z', 'errors': [],
          'unwatched_open_prs': [{'number': 5, 'author': 'new-user'}, {'number': 6, 'author': 'your-login'}]}


def run(tmp_path, login, *args, script='wake_rules.py'):
    local = tmp_path / 'local.json'
    local.write_text(json.dumps({'github_login': login}))
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1',
           'CAO_WORKPLACE_CONFIG': str(local), 'PR_WATCH_CONFIG': str(tmp_path / 'config.json'),
           'SUPER_AGENT_STATE_DIR': os.environ['SUPER_AGENT_STATE_DIR']}
    return subprocess.run([sys.executable, str(SOURCE / script), *args], capture_output=True, text=True,
                          env=env, timeout=20)


def test_own_prs_follow_the_configured_login(tmp_path):
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(REPORT))
    out = run(tmp_path, 'new-user', str(report)).stdout
    assert out.startswith('WAKE ') and 'pr#5:new own PR' in out and 'pr#6' not in out.split('WAKE ', 1)[1]
    out = run(tmp_path, 'your-login', str(report)).stdout
    assert 'pr#6:new own PR' in out and 'pr#5:new own PR' not in out


def test_unset_login_stops_the_rules_and_the_config_check(tmp_path):
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(REPORT))
    r = run(tmp_path, None, str(report))
    assert r.returncode != 0 and 'workplace_login_unset' in r.stderr and r.stdout == ''
    r = run(tmp_path, None, script='config.py')
    assert r.returncode == 1 and r.stdout == '' and 'configuration_error: workplace_login_unset' in r.stderr
    assert run(tmp_path, 'new-user', script='config.py').stdout.strip() == str(tmp_path)


def test_the_loop_refuses_to_start_without_a_login(tmp_path):
    local = tmp_path / 'local.json'
    local.write_text('{"github_login": null}')
    env = {'PATH': '/usr/bin:/bin', 'PYTHON': sys.executable, 'PR_WATCH_CONFIG': str(tmp_path / 'config.json'),
           'SUPER_AGENT_STATE_DIR': os.environ['SUPER_AGENT_STATE_DIR'], 'PYTHONDONTWRITEBYTECODE': '1',
           'CAO_WORKPLACE_CONFIG': str(local), 'INTERVAL': '0', 'RETRY_DELAY': '0'}
    r = subprocess.run(['bash', str(SOURCE / 'tick_loop.sh')], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and r.stdout == 'WAKE none :: configuration_error\n'
    assert not (tmp_path / 'reports').exists() and not (tmp_path / 'state' / 'tick_loop.lock').exists()
