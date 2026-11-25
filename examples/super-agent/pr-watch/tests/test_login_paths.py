"""Every "is this the operator?" check follows the configured login (review P2-1).

The rest of the suite runs as the fixture login "your-login", so a check hardcoded back to that
literal would still pass there. Here the operator is "new-user", and "your-login" is someone else.
"""
import pytest

import config
import poll
from wake_rules import classify, decide

NEW, OTHER = 'new-user', 'your-login'
HEAD = 'abc12345deadbeef'
T0, T1, T2, T3 = ('2026-11-09T09:00:00Z', '2026-11-09T10:00:00Z', '2026-11-09T11:00:00Z',
                  '2026-11-09T12:00:00Z')


@pytest.fixture(autouse=True)
def operator_is_new_user(monkeypatch):
    monkeypatch.setattr(config, '_LOGIN', [NEW])  # what config.login() returns once it has read the file


def node(login, **extra):
    return {'databaseId': 7, 'author': {'login': login}, 'createdAt': T1, 'body': 'x', **extra}


def act(kind, author, at, aid, commit=''):
    return {'kind': kind, 'id': aid, 'author': author, 'at': at, 'commit': commit}


def report(author, events, **fields):
    return {'polled_at': '2026-11-09T12:30:00+00:00', 'items': [{'kind': 'pr', 'number': 5, 'author': author,
            'fields': {'state': 'OPEN', 'ci': 'SUCCESS', 'ci_failing': [], 'ci_pending': 0,
                       'reviewDecision': 'REVIEW_REQUIRED', **fields}, 'events': events}]}


# ---- poll.py

def test_poll_marks_only_the_configured_login_as_ours():
    assert poll.item('comment', node(NEW))['ours'] is True
    assert poll.item('comment', node(OTHER))['ours'] is False
    assert poll.item('comment', node('someone'))['ours'] is False


def test_review_cutoff_counts_only_the_operators_own_review():
    cur = {'activity': [act('review', NEW, T1, 1, HEAD), act('review', OTHER, T2, 2, HEAD)]}
    assert poll.review_cutoff(cur, {'last_reviewed_head': HEAD[:8]}) == T1


def test_reviewed_events_drop_the_operators_own_activity_and_keep_others_after_the_cutoff():
    cur = {'fields': {'head': HEAD},
           'activity': [act('comment', OTHER, T0, 10),           # before the cutoff: old news
                        act('review', NEW, T1, 11, HEAD),        # the operator's review sets the cutoff
                        act('comment', OTHER, T2, 12),           # someone else, after: an event
                        act('comment', NEW, T3, 13)]}            # the operator again: never an event
    events = poll.reviewed_events(None, cur, {'last_reviewed_head': HEAD[:8]})
    assert [e['id'] for e in events] == [12]


# ---- wake_rules.py

def test_own_pr_fields_follow_the_login():
    assert decide(report(NEW, [{'kind': 'field', 'field': 'reviewDecision', 'old': None, 'new': 'APPROVED'}]))['mode'] == 'WAKE'
    assert decide(report(OTHER, [{'kind': 'field', 'field': 'reviewDecision', 'old': None, 'new': 'APPROVED'}]))['mode'] == 'DIGEST'


def test_the_operators_own_comment_is_not_feedback_but_another_logins_is():
    item = report(NEW, [])['items'][0]
    assert classify(item, {'kind': 'comment', 'author': NEW}, {}, {}) == (False, None)
    assert classify(item, {'kind': 'comment', 'author': OTHER}, {}, {}) == (True, 'feedback')


def test_ready_to_merge_wakes_only_for_the_operators_pr():
    mine, theirs = report(NEW, []), report(OTHER, [])
    mine['items'][0]['ready_to_merge_new'] = theirs['items'][0]['ready_to_merge_new'] = True
    assert decide(mine)['reasons'] == ['pr#5:ready_to_merge']
    assert decide(theirs)['mode'] == 'DIGEST'


def test_new_own_pr_follows_the_login():
    d = {'polled_at': '2026-11-09T12:30:00+00:00', 'unwatched_open_prs': [{'number': 8, 'author': NEW},
                                                                          {'number': 9, 'author': OTHER}]}
    assert decide(d)['reasons'] == ['pr#8:new own PR']
