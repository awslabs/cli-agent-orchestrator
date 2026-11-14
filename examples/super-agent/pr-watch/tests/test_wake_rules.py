import copy
import pytest
from wake_rules import decide

NOW = '2026-11-09T12:00:00+00:00'

WATCH = {'identities': {'ours': ['your-login']}}

def report(author='your-login', events=None, **fields):
    return {'polled_at': NOW, 'watch': WATCH, 'items': [{'kind': 'pr', 'number': 102, 'author': author,
        'fields': {'state': 'OPEN', 'ci': 'SUCCESS', 'ci_failing': [], 'ci_pending': 0,
                   'reviewDecision': 'REVIEW_REQUIRED', **fields}, 'events': events or []}]}

def field(name, new):
    return {'kind': 'field', 'field': name, 'old': None, 'new': new}

def mode(d):
    return decide(d)['mode']

@pytest.mark.parametrize('kind', ['review','comment','thread_comment'])
@pytest.mark.parametrize('author', ['maintainer-login','copilot-pull-request-reviewer','github-advanced-security','code-scanning[bot]'])
def test_feedback(kind, author):
    assert mode(report(events=[{'kind':kind,'author':author}])) == 'WAKE'

@pytest.mark.parametrize('author', ['your-login','github-actions','github-actions[bot]'])
def test_self_and_actions_comments(author):
    assert mode(report(events=[{'kind':'comment','author':author}])) != 'WAKE'

def test_actions_review_is_feedback():
    assert mode(report(events=[{'kind':'review','author':'github-actions'}])) == 'WAKE'

@pytest.mark.parametrize('name,new', [('reviewDecision','APPROVED'),('mergeable','CONFLICTING'),('mergeState','DIRTY'),('draft',True),('state','CLOSED'),('merged',True)])
def test_own_fields(name,new):
    assert mode(report(events=[field(name,new)])) == 'WAKE'

@pytest.mark.parametrize('bad', ['FAILURE','TIMED_OUT','CANCELLED','ACTION_REQUIRED','STARTUP_FAILURE','ERROR'])
def test_ci_failure(bad):
    assert mode(report(events=[field('ci',bad)], ci=bad)) == 'WAKE'

@pytest.mark.parametrize('approved,armed,expected', [(False,False,'DIGEST'),(True,False,'WAKE'),(False,True,'WAKE')])
def test_behind(approved,armed,expected):
    assert mode(report(events=[field('mergeState','BEHIND')],reviewDecision='APPROVED' if approved else None,auto_merge=armed)) == expected

@pytest.mark.parametrize('decision,failing,pending,expected', [('APPROVED',[],0,'WAKE'),('REVIEW_REQUIRED',[],0,'DIGEST'),('APPROVED',['unit'],0,'DIGEST'),('APPROVED',[],1,'DIGEST')])
def test_green(decision,failing,pending,expected):
    assert mode(report(events=[field('ci','SUCCESS')],reviewDecision=decision,ci_failing=failing,ci_pending=pending)) == expected

def test_ready_to_merge():
    d=report(); d['items'][0]['ready_to_merge_new']=True
    assert mode(d)=='WAKE'
    d['items'][0]['author']='other'; assert mode(d)=='DIGEST'

@pytest.mark.parametrize('event', [field('ci','FAILURE'),field('mergeState','DIRTY'),field('reviewDecision','APPROVED'),{'kind':'comment','author':'other'}])
def test_other_pr_digest(event):
    assert mode(report('other',[event])) == 'DIGEST'

@pytest.mark.parametrize('state', ['CLOSED','MERGED'])
def test_feedback_only_open(state):
    assert mode(report(events=[{'kind':'review','author':'other'}],state=state)) == 'DIGEST'

def test_unknown_author_cannot_be_ours():
    d=report(events=[field('reviewDecision','APPROVED')]); del d['items'][0]['author']
    assert mode(d)=='DIGEST'

@pytest.mark.parametrize('author,state,expected', [('your-login','OPEN','WAKE'),('other','OPEN','DIGEST'),('your-login','CLOSED','DIGEST')])
def test_new_pr(author,state,expected):
    assert mode({'watch':WATCH,'unwatched_open_prs':[{'number':7,'author':author,'state':state}]}) == expected

@pytest.mark.parametrize('event,expected', [(field('head','abcd'),'WAKE'),(field('ci','FAILURE'),'DIGEST'),(field('mergeState','BEHIND'),'DIGEST'),(field('state','CLOSED'),'WAKE'),(field('merged',True),'WAKE'),({'kind':'comment','author':'other','at':'2026-11-08T12:00Z'},'WAKE'),({'kind':'comment','author':'your-login','at':'2026-11-08T12:00Z'},'QUIET'),({'kind':'review','author':'other','at':'2026-11-07T12:00Z'},'DIGEST')])
def test_reviewed(event,expected):
    d=report('other',[event]); d['reviewer_ledger']={'prs':{'102':{'last_reviewed_at':'2026-11-08T00:00Z'}}}
    assert mode(d)==expected

@pytest.mark.parametrize('hours,expected', [(23,'QUIET'),(24,'QUIET'),(25,'WAKE')])
def test_digest_age(hours,expected):
    from datetime import datetime,timedelta
    old=datetime.fromisoformat(NOW)-timedelta(hours=hours)
    d={'polled_at':NOW,'digest_pending':[{'at':old.isoformat(),'line':'noise'}]}
    assert mode(d)==expected
    if expected=='WAKE': assert decide(d)['reasons']==['digest']

def test_empty_and_delivered_digest():
    assert mode({})=='QUIET'
    assert mode({'polled_at':NOW,'digest_pending':[]})=='QUIET'

@pytest.mark.parametrize('names,expected', [(['known'],'DIGEST'),(['known','new'],'WAKE')])
def test_known_checks(names,expected):
    d=report(events=[field('ci','FAILURE'),field('ci_failing',names)],ci='FAILURE',ci_failing=names)
    d['watch']={**WATCH,'known_ci_failures':{'102':['known']}}
    assert mode(d)==expected

def test_prose_does_not_suppress_failure():
    d=report(events=[field('ci','FAILURE')],ci='FAILURE',ci_failing=['Security Scan'])
    d['watch']={**WATCH,'known_ci_failures':{'CVE-example':'Security Scan already reported'}}
    assert mode(d)=='WAKE'

def test_known_checks_scoped_to_pr():
    d=report(events=[field('ci_failing',['known'])],ci_failing=['known']);d['watch']={**WATCH,'known_ci_failures':{'99':['known']}}
    assert mode(d)=='WAKE'

def test_issue_and_new_issue_digest():
    d=report(events=[{'kind':'comment','author':'other'}]);d['items'][0]['kind']='issue'
    assert mode(d)=='DIGEST'
    assert mode({'new_issues':[{'number':8}]})=='DIGEST'

def test_ours_only_quiet():
    assert mode(report(events=[{'kind':'comment','author':'your-login'}]))=='QUIET'

def test_approval_with_a_p3_and_a_reviewed_head():
    approval={'kind':'review','id':1,'author':'maintainer-login','state':'APPROVED','at':'2026-11-08T12:00Z',
              'body':'APPROVE (0 P1, 0 P2, 1 P3)\n\n1. **P3:** one sentence needs a small correction.'}
    d=report(events=[approval],reviewDecision='APPROVED');assert mode(d)=='WAKE'
    assert any('1 P3' in e.get('body','') for e in d['items'][0]['events'])
    d=report('other-author',[field('head','c0de0002')]);assert mode(d)=='DIGEST'
    d['reviewer_ledger']={'prs':{'102':{}}};assert mode(d)=='WAKE'

def test_no_identities_means_nothing_is_ours():
    d=report(events=[{'kind':'comment','author':'your-login'}]);d['watch']={}
    assert mode(d)=='DIGEST'

def test_pure():
    d=report(events=[field('ci','ERROR')]);before=copy.deepcopy(d);decide(d);assert d==before

@pytest.mark.parametrize('reported,expected', [(True,'DIGEST'),(False,'WAKE')])
def test_structured_known_reported(reported,expected):
    d=report(events=[field('ci_failing',['unit'])],ci_failing=['unit']);d['watch']={**WATCH,'known_ci_failures':{'102':{'checks':['unit'],'reported':reported}}}
    assert mode(d)==expected

def test_oldest_digest():
    d={'polled_at':NOW,'digest_pending':[{'at':'2026-11-08T10:00Z','line':'old'},{'at':'2026-11-09T11:00Z','line':'new'}]}
    assert decide(d)['reasons']==['digest']

def test_reviewed_closed_feedback_digest():
    d=report('other',[{'kind':'comment','author':'other'}],state='CLOSED');d['reviewer_ledger']={'prs':{'102':{}}}
    assert mode(d)=='DIGEST'

def test_issue_field_does_not_wake():
    d=report(events=[field('state','CLOSED')]);d['items'][0]['kind']='issue';assert mode(d)=='DIGEST'

def test_first_seen_issue_digest():
    d=report();d['items'][0]['kind']='issue';d['items'][0]['first_seen']=True;assert mode(d)=='DIGEST'

def test_unknown_self_activity_flag():
    assert mode(report(events=[{'kind':'comment','ours':True}]))=='QUIET'

def test_reviewed_unknown_timestamp_conservative():
    d=report('other',[{'kind':'comment','author':'other'}]);d['reviewer_ledger']={'prs':{'102':{'last_reviewed_at':'2026-11-08T00:00Z'}}}
    assert mode(d)=='WAKE'

def test_reviewed_at_cutoff_is_not_new():
    d=report('other',[{'kind':'comment','author':'other','at':'2026-11-08T00:00Z'}]);d['reviewer_ledger']={'prs':{'102':{'last_reviewed_at':'2026-11-08T00:00Z'}}}
    assert mode(d)=='DIGEST'
