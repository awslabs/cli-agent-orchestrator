import json
from pathlib import Path
from digest_store import process, delivered

def setup(tmp_path):
    config={k:tmp_path/v for k,v in [('watch','watch.json'),('ledger','ledger.json'),('queue','queue.json'),('digest','digest.md')]}
    config['watch'].write_text('{"identities":{"ours":["your-login"]}}');config['ledger'].write_text('{"prs":{}}')
    return config

def test_queue_catchup_and_once(tmp_path):
    c=setup(tmp_path)
    r=tmp_path/'report.json';r.write_text(json.dumps({'polled_at':'2026-11-08T00:00Z','new_issues':[{'number':1}]}))
    p=process(r,c);assert p['mode']=='DIGEST';assert len(json.loads(c['queue'].read_text()))==1
    process(r,c);assert len(json.loads(c['queue'].read_text()))==1
    r.write_text(json.dumps({'polled_at':'2026-11-09T00:01Z'}))
    p=process(r,c);assert p['mode']=='WAKE';assert p['reasons']==['digest']
    enriched=json.loads(Path(p['report']).read_text());assert len(enriched['digest_lines'])==1
    delivered(Path(p['report']),c)
    assert process(r,c)['mode']=='QUIET'

def test_real_wake_includes_pending_and_current_digest(tmp_path):
    c=setup(tmp_path);r=tmp_path/'report.json'
    r.write_text(json.dumps({'polled_at':'2026-11-09T00:00Z','new_issues':[{'number':1}]}));process(r,c)
    r.write_text(json.dumps({'polled_at':'2026-11-09T01:00Z','new_issues':[{'number':2}], 'unwatched_open_prs':[{'number':4,'author':'your-login'}]}))
    p=process(r,c);assert p['mode']=='WAKE';assert len(json.loads(Path(p['report']).read_text())['digest_lines'])==2
    delivered(Path(p['report']),c);assert json.loads(c['queue'].read_text())==[]

def test_delivery_only_clears_its_entries(tmp_path):
    c=setup(tmp_path);r=tmp_path/'report.json';r.write_text(json.dumps({'polled_at':'2026-11-09T00:00Z','new_issues':[{'number':1}]}));p=process(r,c)
    q=json.loads(c['queue'].read_text());q.append({'at':'2026-11-09T02:00Z','line':'later'});c['queue'].write_text(json.dumps(q))
    delivered(Path(p['report']),c);assert json.loads(c['queue'].read_text())==[q[-1]]

def test_enriched_report_replays_pure_policy(tmp_path):
    from wake_rules import decide
    c=setup(tmp_path);c['ledger'].write_text(json.dumps({'prs':{'5':{'reviews':[{'date':'2026-11-08'}]}}}))
    r=tmp_path/'report.json';r.write_text(json.dumps({'polled_at':'2026-11-09T01:00Z','items':[{'kind':'pr','number':5,'author':'other','fields':{'state':'OPEN'},'events':[{'kind':'field','field':'head','new':'abcd'}]}]}))
    p=process(r,c);d=json.loads(Path(p['report']).read_text());assert decide(d)['mode']==p['mode']=='WAKE'
    assert decide(d)['reasons']==p['reasons']
