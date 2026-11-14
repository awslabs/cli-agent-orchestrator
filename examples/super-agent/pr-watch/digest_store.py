#!/usr/bin/env python3
"""Durable digest adapter; wake_rules itself stays pure. Single loop holds lock.

Wake journal, one atomic JSON list (config 'journal', default state/wake-journal.json):
poll.py appends {id, report, wake: null} BEFORE it saves the new seen ids; process() then either
records the WAKE {report, reason} in that entry or retires it (QUIET/DIGEST: its digest lines are
already in the queue); --delivered retires it only after the loop has emitted the WAKE; --replay
(the loop's first step on every start and tick) re-emits the oldest undelivered WAKE, re-filtering
a journaled report that was never processed. A crash between emission and --delivered repeats
that WAKE once on the next start; nothing is lost.
"""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from config import load
from wake_rules import decide, known_checks, last_review_at, ours


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def write_json(path, value):
    atomic(path, json.dumps(value, indent=1)+'\n')


def journal_path(config):
    return Path(config.get('journal') or Path(config['queue']).parent/'wake-journal.json')


def journal_add(path, report):
    """Append one poll report to the journal (atomically); the report gets its journal_id."""
    path = Path(path)
    entries = read(path, [])
    jid = report.get('polled_at', 'report') + '-' + uuid.uuid4().hex[:12]
    report['journal_id'] = jid
    entries.append({'id': jid, 'report': report, 'wake': None})
    write_json(path, entries)
    return jid


def journal_update(config, jid, wake=None, retire=False):
    path = journal_path(config)
    entries = read(path, [])
    out = []
    for e in entries:
        if e['id'] == jid:
            if retire:
                continue
            e = {**e, 'wake': wake}
        out.append(e)
    if out != entries:
        write_json(path, out)


def process(path, config):
    raw = read(path)
    raw['watch'] = read(config['watch'])
    raw['reviewer_ledger'] = read(config['ledger'])
    pending = read(config['queue'], [])
    result = decide(raw)
    seen = {p['line'] for p in pending}
    for line in result['digest']:
        if line not in seen:
            pending.append({'at':raw['polled_at'], 'line':line})
            seen.add(line)
    write_json(config['queue'], pending)
    raw['digest_pending'] = pending
    result = decide(raw)
    raw['filter_mode'] = result['mode']
    raw['filter_reasons'] = result['reasons']
    raw['digest_lines'] = [p['at']+' '+p['line'] for p in pending]
    # Keep only the policy context required to re-run the pure CLI on this report.
    raw['watch'] = {'identities': {'ours': sorted(ours(raw['watch']))}, 'known_ci_failures': {
        str(i['number']): sorted(known_checks(raw['watch'], i['number']))
        for i in raw.get('items', []) if i.get('kind') == 'pr'}}
    reviewed = raw['reviewer_ledger'].get('prs', {})
    raw['reviewer_ledger'] = {'prs': {
        str(i['number']): {'last_reviewed_at': last_review_at(reviewed[str(i['number'])])}
        for i in raw.get('items', []) if i.get('kind') == 'pr' and str(i['number']) in reviewed}}
    output = path.with_suffix('.wake.json')
    write_json(output, raw)
    atomic(config['digest'], '# PR watch digest\n\n' + ''.join('- '+line+'\n' for line in raw['digest_lines']))
    if raw.get('journal_id'):
        # The WAKE reason is durable before the loop emits it; anything else is settled here.
        if result['mode'] == 'WAKE':
            journal_update(config, raw['journal_id'],
                           wake={'report': str(output), 'reason': ' | '.join(result['reasons'])})
        else:
            journal_update(config, raw['journal_id'], retire=True)
    return {**result, 'report':str(output)}


def delivered(path, config, others=()):
    report = read(path)
    included = report['digest_pending']
    pending = read(config['queue'], [])
    write_json(config['queue'], [p for p in pending if p not in included])
    # Retire the journal entries only now, after the WAKE line was emitted.
    for p in (path, *others):
        jid = report.get('journal_id') if p == path else (read(Path(p)) or {}).get('journal_id')
        if jid:
            journal_update(config, jid, retire=True)


def replay(config):
    """The oldest undelivered WAKE as (report, reason), else None; re-filters unprocessed reports."""
    for entry in read(journal_path(config), []):
        if entry['wake'] is None:
            reports = Path(config.get('watch_dir') or journal_path(config).parent)/'reports'
            reports.mkdir(parents=True, exist_ok=True)
            source = reports/('replay-'+entry['id'].replace(':', '')+'.json')
            write_json(source, entry['report'])
            result = process(source, config)
            if result['mode'] != 'WAKE':
                continue
            return result['report'], ' | '.join(result['reasons'])
        return entry['wake']['report'], entry['wake']['reason']
    return None


def main():
    config = load()
    if sys.argv[1] == '--delivered':
        delivered(Path(sys.argv[2]), config, [Path(p) for p in sys.argv[3:] if p])
        return
    if sys.argv[1] == '--replay':
        found = replay(config)
        print('QUIET' if found is None else 'WAKE\t'+found[0]+'\t'+found[1].replace('\n',' ').replace('\t',' '))
        return
    result = process(Path(sys.argv[1]), config)
    # Shell-safe protocol: tab-delimited fields, no eval or command execution.
    reasons = ' | '.join(result['reasons']).replace('\n',' ').replace('\t',' ')
    print(result['mode']+'\t'+result['report']+'\t'+reasons)

if __name__ == '__main__': main()
