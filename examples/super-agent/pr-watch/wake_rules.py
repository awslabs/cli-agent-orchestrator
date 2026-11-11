#!/usr/bin/env python3
"""Pure wake policy. JSON (stdin or path) -> WAKE reasons / DIGEST lines / QUIET.

Optional report keys: watch (its "identities" names our logins), reviewer_ledger, digest_pending. No file reads in
classify/decide; callers supply context. Unknown authors are never guessed.
"""
import json
import sys
from datetime import datetime, timezone

BAD = {'FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'ERROR'}
FEEDBACK = {'review', 'comment', 'thread_comment'}
ACTIONS = {'github-actions', 'github-actions[bot]'}
STATE = {'state', 'merged', 'draft'}


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def last_review_at(meta):
    explicit = meta.get('last_reviewed_at')
    if explicit:
        return explicit
    dates = [r.get('posted_at') or r.get('posted') or r.get('date') for r in meta.get('reviews', [])]
    return max((x for x in dates if x), default=None)


def ours(watch):
    """Our own GitHub logins, from watch.json "identities": {"ours": [...]}. Empty means none."""
    found = (watch.get('identities') or {}).get('ours') or []
    return {x for x in found if isinstance(x, str) and x}


def known_checks(watch, number):
    """Only explicit per-PR already-reported check names can suppress a failure.

    Format: known_ci_failures: {"123": ["check name"]}, or {"123":
    {"checks": [...], "reported": true}}. Prose CVE descriptions are not evidence.
    """
    record = watch.get('known_ci_failures', {}).get(str(number), [])
    if isinstance(record, dict):
        record = record.get('checks', []) if record.get('reported') is True else []
    return set(record) if isinstance(record, list) else set()


def classify(item, event, watch, reviewed):
    """Return (wake boolean, explanation), or (False, None) for own activity."""
    f = item.get('fields', {})
    us = ours(watch)
    own = item.get('author', f.get('author')) in us
    open_pr = item.get('kind') == 'pr' and f.get('state') == 'OPEN'
    meta = reviewed.get(str(item['number']))
    is_reviewed = meta is not None and item.get('kind') == 'pr'
    kind = event.get('kind')
    if kind in FEEDBACK:
        author = event.get('author')
        if author in us or (not author and event.get('ours')):
            return False, None
        ci_comment = kind in {'comment', 'thread_comment'} and author in ACTIONS
        if open_pr and own and not ci_comment:
            return True, 'feedback'
        if open_pr and is_reviewed:
            cutoff = timestamp(item.get('reviewed_since') or last_review_at(meta))
            at = timestamp(event.get('at'))
            if cutoff is None or at is None or at > cutoff:
                return True, 'reviewed feedback'
        return False, 'CI comment' if ci_comment else 'activity outside wake scope'
    if kind != 'field':
        return False, 'activity outside wake scope'
    name, new = event.get('field'), event.get('new')
    if item.get('kind') == 'pr':
        # Terminal transitions matter even though the current state is no longer OPEN.
        if own and name in STATE:
            return True, 'own state'
        if is_reviewed and (name == 'state' or (name == 'merged' and new)):
            return True, 'reviewed state'
        if open_pr and is_reviewed and name == 'head':
            return True, 'reviewed head'
        if open_pr and own:
            if name == 'reviewDecision':
                return True, 'review decision'
            if (name == 'mergeable' and new == 'CONFLICTING') or (name == 'mergeState' and new == 'DIRTY'):
                return True, 'conflict'
            if name == 'mergeState' and new == 'BEHIND' and (f.get('reviewDecision') == 'APPROVED' or f.get('auto_merge')):
                return True, 'update due'
            failures = set(f.get('ci_failing') or [])
            known = known_checks(watch, item['number'])
            if name == 'ci' and new in BAD:
                if not failures or not failures <= known:
                    return True, 'CI failure'
                return False, 'known CI failure already reported'
            if name == 'ci_failing' and new:
                if set(new) - known:
                    return True, 'CI failing checks'
                return False, 'known CI failure already reported'
            if name == 'ci' and new == 'SUCCESS' and f.get('reviewDecision') == 'APPROVED' and not failures and not f.get('ci_pending'):
                return True, 'approved all green'
    return False, 'change outside wake scope'


def decide(report):
    reasons, lines = [], []
    watch = report.get('watch', {})
    us = ours(watch)
    reviewed = report.get('reviewer_ledger', {}).get('prs', {})
    for p in report.get('unwatched_open_prs', []):
        author = p.get('author')
        if isinstance(author, dict):
            author = author.get('login')
        if author in us and p.get('state', 'OPEN') == 'OPEN':
            reasons.append(f"pr#{p['number']}:new own PR")
        else:
            lines.append(f"pr#{p['number']}:new PR outside wake scope")
    for p in report.get('new_issues', []):
        lines.append(f"issue#{p['number']}:new issue")
    for item in report.get('items', []):
        label = f"{item['kind']}#{item['number']}"
        f = item.get('fields', {})
        if item.get('ready_to_merge_new'):
            if item.get('kind') == 'pr' and item.get('author', f.get('author')) in us and f.get('state') == 'OPEN':
                reasons.append(f'{label}:ready_to_merge')
            else:
                lines.append(f'{label}:ready_to_merge outside wake scope')
        if item.get('kind') == 'issue' and item.get('first_seen'):
            lines.append(f'{label}:new issue')
        for event in item.get('events', []):
            wake, why = classify(item, event, watch, reviewed)
            if why is None:
                continue
            detail = (f"{event.get('field')}={event.get('new')}" if event.get('kind') == 'field'
                      else f"{event.get('kind')}:{event.get('author')}:{event.get('id', '')}")
            text = f'{label}:{detail} ({why})'
            (reasons if wake else lines).append(text)
    pending = report.get('digest_pending', [])
    now = timestamp(report.get('polled_at'))
    dates = [timestamp(p.get('at')) for p in pending]
    dates = [d for d in dates if d is not None]
    if now is not None and dates and (now - min(dates)).total_seconds() > 86400:
        reasons.append('digest')
    reasons = list(dict.fromkeys(reasons))
    lines = list(dict.fromkeys(lines))
    return {'mode': 'WAKE' if reasons else ('DIGEST' if lines else 'QUIET'), 'reasons': reasons, 'digest': lines}


def main():
    with open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin as fh:
        result = decide(json.load(fh))
    print(result['mode'] + (' ' + ' | '.join(result['reasons'] if result['mode'] == 'WAKE' else result['digest']) if result['mode'] != 'QUIET' else ''))

if __name__ == '__main__':
    main()
