#!/usr/bin/env python3
"""Print a compact table + events from a poll.py report. Usage: summary.py report.json"""
import json
import sys

d = json.load(open(sys.argv[1]))
print("polled_at:", d["polled_at"], "errors:", d["errors"])
for p in d.get("unwatched_open_prs", []):
    print(f"NEW UNWATCHED PR #{p['number']} draft={p['isDraft']} {p['createdAt']} {p['title'][:70]} {p['url']}")
for i in d["items"]:
    f = i["fields"]
    n = i["number"]
    if i["kind"] == "pr":
        print(f"PR #{n:<4} {f['state']:<6} draft={f['draft']!s:<5} {f['mergeable']:<11} {f['mergeState']:<8} "
              f"rd={f['reviewDecision']!s:<17} ci={f['ci']} fail={f['ci_failing']} pend={f['ci_pending']} "
              f"unres={i['unresolved_threads']} rtm={i['ready_to_merge']}{'(NEW)' if i['ready_to_merge_new'] else ''} "
              f"req={f['requested']} head={f['head']}")
    else:
        print(f"IS #{n:<4} {f['state']:<6} labels={f['labels']}")
    for e in i["events"]:
        if e["kind"] == "field":
            print(f"    * {e['field']}: {e['old']} -> {e['new']}")
        else:
            tag = "ours" if e["ours"] else ("bot" if e["bot"] else "EXTERNAL")
            loc = f" {e.get('path')}:{e.get('line')}" if e["kind"] == "thread_comment" else ""
            st = f" [{e.get('state')}]" if e["kind"] == "review" else ""
            print(f"    + {e['kind']}{st} by {e['author']} ({tag}) {e['at']}{loc} {e['url']}")
            print("      " + (e["body"] or "")[:400].replace("\n", " "))

if d.get("digest_lines"):
    print("DIGEST (delivered with this wake):")
    for line in d["digest_lines"]:
        print("    " + line)
