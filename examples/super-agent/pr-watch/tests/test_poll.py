import json
import poll

def test_ledger_union_and_metadata():
    w={'prs':{'7':{'owner':'ours'}},'issues':{},'ignore_prs':[8]}
    l={'prs':{'7':{'last_reviewed_head':'a'},'8':{'last_reviewed_head':'b','reviews':[{'date':'2026-11-08'}]}}}
    targets=poll.watch_targets(w,l)
    assert [n for k,n,m in targets]==[7,8]
    assert targets[0][2]['owner']=='ours'
    assert targets[1][2]['reviewed']['last_reviewed_head']=='b'

def test_initial_reviewed_events_once():
    meta={'last_reviewed_head':'oldhead','reviews':[{'date':'2026-11-08'}]}
    cur={'fields':{'head':'newhead'},'activity':[{'kind':'comment','id':1,'author':'other','at':'2026-11-08T01:00Z'},{'kind':'comment','id':2,'author':'other','at':'2026-11-07T01:00Z'}]}
    events=poll.reviewed_events(None,cur,meta)
    assert [e.get('field',e['kind']) for e in events]==['head','comment']
    cur['review_watch_head']='oldhead'
    assert poll.reviewed_events(cur,cur,meta)==[]

def test_own_review_exact_cutoff(monkeypatch):
    monkeypatch.setattr(poll,'OURS',{'your-login'})
    meta={'last_reviewed_head':'abcd','reviews':[{'date':'2026-11-08'}]}
    cur={'fields':{'head':'abcd'},'activity':[{'kind':'review','id':1,'author':'your-login','commit':'abcd','at':'2026-11-08T10:00Z'}, {'kind':'comment','id':2,'author':'other','at':'2026-11-08T09:00Z'}, {'kind':'comment','id':3,'author':'other','at':'2026-11-08T11:00Z'}]}
    assert [e['id'] for e in poll.reviewed_events(None,cur,meta)]==[3]

def test_existing_snapshot_ledger_adoption():
    meta={'last_reviewed_head':'oldhead','reviews':[{'date':'2026-11-08'}]}
    cur={'fields':{'head':'newhead'},'activity':[]}
    assert poll.reviewed_events(cur,cur,meta)==[{'kind':'field','field':'head','old':'oldhead','new':'newhead'}]
    cur['review_watch_head']='oldhead';assert poll.reviewed_events(cur,cur,meta)==[]

def test_main_mocked_github_preserves_schema_and_ledger_adoption(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(poll,'CONFIG',{'watch':tmp_path/'watch.json','ledger':tmp_path/'ledger.json'})
    monkeypatch.setattr(poll,'HERE',str(tmp_path));monkeypatch.setattr(poll,'STATE_DIR',str(tmp_path/'state'))
    (tmp_path/'watch.json').write_text('{"repo":"owner/repo","prs":{"102":{}},"ignore_prs":[106],"identities":{"ours":["your-login"]}}')
    (tmp_path/'ledger.json').write_text('{"prs":{"106":{"last_reviewed_head":"oldhead","reviews":[{"date":"2026-11-08"}]}}}')
    def graphql(query,owner,repo,number):
        f={'mergeable':'MERGEABLE','mergeState':'CLEAN','reviewDecision':'APPROVED','ci':'SUCCESS'}
        return {'pullRequest':{'number':number,'title':'fixture','author':{'login':'your-login' if number==102 else 'other-author'},'autoMergeRequest':{'enabledAt':'2026-11-08T00:00Z'},
           'state':'OPEN','merged':False,'mergedBy':None,'isDraft':False,'mergeable':f['mergeable'],'mergeStateStatus':f['mergeState'],'reviewDecision':f['reviewDecision'],
           'headRefOid':'newhead1','headRefName':'branch','reviewRequests':{'nodes':[]},'comments':{'nodes':[]},'reviews':{'nodes':[]},'reviewThreads':{'nodes':[]},
           'commits':{'nodes':[{'commit':{'statusCheckRollup':{'state':f['ci'],'contexts':{'nodes':[]}}}}]},'closingIssuesReferences':{'nodes':[]}}}
    monkeypatch.setattr(poll,'gql',graphql)
    monkeypatch.setattr(poll.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='[]'))
    monkeypatch.setattr(poll.sys,'argv',['poll.py'])
    assert poll.main()==0;d=json.loads(capsys.readouterr().out)
    assert {i['number'] for i in d['items']}=={102,106}
    own=next(i for i in d['items'] if i['number']==102);assert own['author']=='your-login';assert own['fields']['auto_merge']
    reviewed=next(i for i in d['items'] if i['number']==106);assert any(e.get('field')=='head' for e in reviewed['events'])
    assert poll.main()==0;d=json.loads(capsys.readouterr().out)
    assert all(not i['events'] for i in d['items'])

