"""WAKE reasons are journaled before poll.py saves the new seen ids, and an
undelivered WAKE is replayed before the next poll, so an interruption never turns into QUIET."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import digest_store
import poll

SOURCE = Path(__file__).resolve().parents[1]
COMMENT = {'databaseId': 101, 'author': {'login': 'maintainer-login'}, 'createdAt': '2026-11-09T10:00:00Z',
           'body': 'please fix', 'url': 'https://example.invalid/c/101'}


def gh_pr(comments):
    return {'pullRequest': {
        'number': 102, 'title': 'ours', 'author': {'login': 'your-login'}, 'autoMergeRequest': None,
        'state': 'OPEN', 'merged': False, 'mergedBy': None, 'isDraft': False, 'mergeable': 'MERGEABLE',
        'mergeStateStatus': 'CLEAN', 'reviewDecision': 'REVIEW_REQUIRED', 'headRefOid': 'a' * 40,
        'headRefName': 'fix/x', 'reviewRequests': {'nodes': []}, 'comments': {'nodes': comments},
        'reviews': {'nodes': []}, 'reviewThreads': {'nodes': []},
        'commits': {'nodes': [{'commit': {'statusCheckRollup': {'state': 'PENDING', 'contexts': {'nodes': []}}}}]},
        'closingIssuesReferences': {'nodes': []}}}


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    """The actual poll.main and digest adapter on mocked GitHub data, all under tmp_path."""
    config = {'watch_dir': tmp_path, 'watch': tmp_path / 'watch.json', 'ledger': tmp_path / 'ledger.json',
              'queue': tmp_path / 'state' / 'digest-pending.json', 'digest': tmp_path / 'digest.md',
              'journal': tmp_path / 'state' / 'wake-journal.json'}
    config['watch'].write_text('{"repo":"owner/repo","prs":{"102":{}},"identities":{"ours":["your-login"]}}')
    config['ledger'].write_text('{"prs":{}}')
    monkeypatch.setattr(poll, 'CONFIG', config)
    monkeypatch.setattr(poll, 'HERE', str(tmp_path))
    monkeypatch.setattr(poll, 'STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(poll.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='[]'))
    monkeypatch.setattr(poll.sys, 'argv', ['poll.py'])
    gh = {'comments': []}
    monkeypatch.setattr(poll, 'gql', lambda q, o, r, n: gh_pr(list(gh['comments'])))
    n = {'tick': 0}

    def tick():
        """One loop tick up to (not including) emit_wake: poll.py > R; digest_store.py R."""
        assert poll.main() == 0
        n['tick'] += 1
        r = tmp_path / 'reports' / f"tick-{n['tick']}.json"
        r.parent.mkdir(exist_ok=True)
        r.write_text(capsys.readouterr().out)
        return digest_store.process(r, config)
    return SimpleNamespace(config=config, gh=gh, tick=tick, path=tmp_path)


def journal(w):
    return json.loads(w.config['journal'].read_text())


def test_interruption_before_emit_wake_replays_the_original_reason(world):
    """The reviewer's sequence: QUIET baseline, a new comment -> WAKE, interruption just before
    emit_wake, the next poll on unchanged data -> QUIET, yet the next tick gives that WAKE first."""
    assert world.tick()['mode'] == 'QUIET'
    assert journal(world) == []
    world.gh['comments'] = [COMMENT]
    woke = world.tick()
    assert woke['mode'] == 'WAKE' and any('comment:maintainer-login:101' in r for r in woke['reasons']), woke
    reason = ' | '.join(woke['reasons'])
    # interrupted: neither the WAKE line nor --delivered ran
    assert world.tick()['mode'] == 'QUIET'  # the seen ids were saved: unchanged data is quiet
    assert [e['wake'] for e in journal(world)] == [{'report': woke['report'], 'reason': reason}]
    assert digest_store.replay(world.config) == (woke['report'], reason)
    digest_store.delivered(Path(woke['report']), world.config)  # after the emission
    assert journal(world) == [] and digest_store.replay(world.config) is None


def test_journal_is_written_before_the_seen_ids(world, monkeypatch):
    world.tick()
    state = world.path / 'state' / 'pr-102.json'
    before = state.read_bytes()
    world.gh['comments'] = [COMMENT]

    def boom(path, report):
        raise OSError('disk full')
    monkeypatch.setattr(poll, 'journal_add', boom)
    with pytest.raises(OSError):
        poll.main()
    assert state.read_bytes() == before  # nothing saved: the comment is still new next time
    monkeypatch.undo()


def test_an_unprocessed_journaled_report_is_refiltered(world, capsys):
    world.tick()
    world.gh['comments'] = [COMMENT]
    assert poll.main() == 0  # interrupted right after poll.py: digest_store.py never ran
    capsys.readouterr()
    [entry] = journal(world)
    assert entry['wake'] is None and entry['report']['journal_id'] == entry['id']
    found = digest_store.replay(world.config)
    assert found is not None
    report, reason = found
    assert 'comment:maintainer-login:101' in reason and Path(report).exists()
    assert journal(world)[0]['wake'] == {'report': report, 'reason': reason}


def test_quiet_and_digest_entries_settle_and_baseline_is_not_journaled(world, monkeypatch, capsys):
    monkeypatch.setattr(poll.sys, 'argv', ['poll.py', '--baseline'])
    assert poll.main() == 0
    assert not world.config['journal'].exists()
    monkeypatch.setattr(poll.sys, 'argv', ['poll.py'])
    capsys.readouterr()
    assert world.tick()['mode'] == 'QUIET' and journal(world) == []
    assert digest_store.replay(world.config) is None


def test_crash_after_emission_repeats_at_most_once(world):
    world.tick()
    world.gh['comments'] = [COMMENT]
    woke = world.tick()
    first = digest_store.replay(world.config)  # emitted, then a crash before --delivered
    second = digest_store.replay(world.config)  # the next start emits it once more ...
    assert first == second == (woke['report'], ' | '.join(woke['reasons']))
    digest_store.delivered(Path(woke['report']), world.config)  # ... and retires it
    assert digest_store.replay(world.config) is None


def test_delivered_retires_the_referenced_retry_report_too(world):
    world.tick()
    world.gh['comments'] = [COMMENT]
    a = world.tick()
    b = world.tick()  # a second report in the same tick (the retry); QUIET settles itself
    assert b['mode'] == 'QUIET' and len(journal(world)) == 1
    world.config['journal'].write_text(json.dumps(journal(world) + [
        {'id': 'other', 'report': {}, 'wake': {'report': b['report'], 'reason': 'x'}}]))
    d = json.loads(Path(b['report']).read_text())
    d['journal_id'] = 'other'
    Path(b['report']).write_text(json.dumps(d))
    digest_store.delivered(Path(a['report']), world.config, [Path(b['report']), Path('replay')])
    assert journal(world) == []


def test_replay_emits_the_oldest_undelivered_wake_first(world):
    world.tick()
    world.gh['comments'] = [COMMENT]
    first = world.tick()
    world.gh['comments'] = [COMMENT, dict(COMMENT, databaseId=102, url='https://example.invalid/c/102')]
    second = world.tick()
    assert first['mode'] == second['mode'] == 'WAKE' and len(journal(world)) == 2
    assert digest_store.replay(world.config) == (first['report'], ' | '.join(first['reasons']))
    digest_store.delivered(Path(first['report']), world.config)
    assert digest_store.replay(world.config) == (second['report'], ' | '.join(second['reasons']))


# --- the real tick_loop.sh: replay first, never QUIET; a delivered WAKE is not repeated ---

def loop_setup(tmp_path, reports):
    for name in ['tick_loop.sh', 'config.py', 'digest_store.py', 'wake_rules.py']:
        shutil.copyfile(SOURCE / name, tmp_path / name)
    (tmp_path / 'watch.json').write_text('{"identities":{"ours":["your-login"]}}')
    (tmp_path / 'ledger.json').write_text('{"prs":{}}')
    (tmp_path / 'config.json').write_text(json.dumps({'watch_dir': '.', 'ledger_path': 'ledger.json'}))
    (tmp_path / 'mock-reports.json').write_text(json.dumps(reports))
    (tmp_path / 'poll.py').write_text('''import json
from pathlib import Path
from config import load
from digest_store import journal_add
p=Path('count');n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))
reports=json.loads(Path('mock-reports.json').read_text());d=reports[min(n,len(reports)-1)]
journal_add(load()['journal'], d)
print(json.dumps(d))
''')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path), 'PYTHON': sys.executable,
           'PR_WATCH_CONFIG': str(tmp_path / 'config.json'), 'INTERVAL': '0', 'RETRY_DELAY': '0',
           'CAO_TERMINAL_ID': 'test-owner', 'PYTHONDONTWRITEBYTECODE': '1'}
    return env


def loop(tmp_path, env):
    return subprocess.run(['bash', str(tmp_path / 'tick_loop.sh')], env=env, capture_output=True,
                          text=True, timeout=20)


OWN = {'polled_at': '2026-11-09T00:00Z', 'errors': [], 'unwatched_open_prs': [{'number': 2, 'author': 'your-login'}]}
QUIET = {'polled_at': '2026-11-09T00:30Z', 'errors': []}
OTHER = {'polled_at': '2026-11-09T01:00Z', 'errors': [], 'unwatched_open_prs': [{'number': 9, 'author': 'your-login'}]}


def interrupted_wake(tmp_path, env):
    """Run poll.py + digest_store.py exactly as one tick does, then stop before emit_wake."""
    r = tmp_path / 'reports' / 'tick-1.json'
    r.parent.mkdir(exist_ok=True)
    out = subprocess.run([sys.executable, str(tmp_path / 'poll.py')], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=20)
    r.write_text(out.stdout)
    res = subprocess.run([sys.executable, str(tmp_path / 'digest_store.py'), str(r)], cwd=tmp_path,
                         env=env, capture_output=True, text=True, timeout=20)
    mode, report, reason = res.stdout.rstrip('\n').split('\t')
    assert mode == 'WAKE' and reason == 'pr#2:new own PR'
    return report, reason


def test_loop_replays_the_undelivered_wake_before_polling(tmp_path):
    env = loop_setup(tmp_path, [OWN, OTHER])
    report, reason = interrupted_wake(tmp_path, env)
    p = loop(tmp_path, env)
    assert (p.returncode, p.stdout) == (0, f'WAKE {report} replay :: {reason}\n'), p.stdout + p.stderr
    assert (tmp_path / 'count').read_text() == '1'  # no new poll ran before the replay
    assert json.loads((tmp_path / 'state' / 'wake-journal.json').read_text()) == []
    p = loop(tmp_path, env)  # delivered: the next start polls and wakes only for the new event
    assert p.returncode == 0 and ':: pr#9:new own PR' in p.stdout and 'pr#2' not in p.stdout, p.stdout
    assert (tmp_path / 'count').read_text() == '2'


def test_loop_replays_a_report_that_was_never_filtered(tmp_path):
    env = loop_setup(tmp_path, [OWN, QUIET])
    subprocess.run([sys.executable, str(tmp_path / 'poll.py')], cwd=tmp_path, env=env,
                   capture_output=True, text=True, timeout=20)  # killed right after poll.py
    p = loop(tmp_path, env)
    assert p.returncode == 0 and p.stdout.endswith(' replay :: pr#2:new own PR\n'), p.stdout + p.stderr
    assert (tmp_path / 'count').read_text() == '1'


def test_loop_retry_wake_leaves_no_journal_entry(tmp_path):
    env = loop_setup(tmp_path, [dict(OWN, errors=[{'error': 'partial'}]), OTHER])
    p = loop(tmp_path, env)
    assert p.returncode == 0 and 'retry=' in p.stdout, p.stdout
    assert json.loads((tmp_path / 'state' / 'wake-journal.json').read_text()) == []
