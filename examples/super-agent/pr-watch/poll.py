#!/usr/bin/env python3
"""Read-only PR/issue babysitter poller.

Fetches each watched PR and issue via `gh api graphql`, diffs it against the
previous snapshot in state/, and prints the new events as JSON. It never
writes to GitHub. Usage: poll.py [--baseline]

Before it saves any snapshot (the new seen ids), a non-baseline poll appends its whole report to
the wake journal (digest_store.journal_add, atomic), so an interruption before the loop emits the
WAKE can never turn the next poll on unchanged data into QUIET: the loop replays it first.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from config import load
from digest_store import journal_add
from wake_rules import last_review_at, ours, timestamp

CONFIG = load()
HERE = str(CONFIG["watch_dir"])
STATE_DIR = os.path.join(HERE, "state")
OURS = set()  # filled from watch.json "identities.ours" in main()
BAD = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
EXCERPT = 1500

COMMENT = "nodes{databaseId author{login} createdAt body url}"
PR_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
 number title author{login} autoMergeRequest{enabledAt mergeMethod} state merged mergedAt mergedBy{login} isDraft mergeable mergeStateStatus reviewDecision
 headRefName headRefOid updatedAt
 reviewRequests(first:10){nodes{requestedReviewer{... on User{login} ... on Team{name}}}}
 comments(last:40){%s}
 reviews(last:40){nodes{databaseId author{login} state submittedAt body url commit{oid}}}
 reviewThreads(last:80){nodes{id isResolved isOutdated path line comments(last:30){%s}}}
 commits(last:1){nodes{commit{oid statusCheckRollup{state contexts(first:100){nodes{
   __typename ... on CheckRun{name conclusion status detailsUrl} ... on StatusContext{context state targetUrl}}}}}}}
 closingIssuesReferences(first:10){nodes{number}}
}}}""" % (COMMENT, COMMENT)
ISSUE_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){
 number title state stateReason updatedAt labels(first:30){nodes{name}} comments(last:40){%s}
}}}""" % COMMENT


def gql(query, owner, repo, number):
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"o={owner}", "-F", f"r={repo}",
         "-F", f"n={number}"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh exited {out.returncode}")
    return json.loads(out.stdout)["data"]["repository"]


def who(node):
    return (node.get("author") or {}).get("login") or "ghost"


def item(kind, node, **extra):
    a = who(node)
    body = node.get("body") or ""
    return {
        "kind": kind, "id": node["databaseId"], "author": a,
        "ours": a in OURS, "bot": a.endswith("[bot]") or a in {
            "copilot-pull-request-reviewer", "github-advanced-security", "github-actions"},
        "at": node.get("createdAt") or node.get("submittedAt"), "url": node.get("url"),
        "body": body[:EXCERPT] + ("…" if len(body) > EXCERPT else ""), **extra,
    }


def pr_snapshot(pr):
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    failing, pending = [], 0
    for c in (rollup.get("contexts") or {}).get("nodes", []):
        name = c.get("name") or c.get("context")
        concl = c.get("conclusion") or c.get("state")
        if concl in BAD:
            failing.append({"name": name, "url": c.get("detailsUrl") or c.get("targetUrl")})
        elif c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED" or concl in {"PENDING", "EXPECTED"}:
            pending += 1
    activity = [item("comment", c) for c in pr["comments"]["nodes"]]
    activity += [item("review", r, state=r["state"], commit=(r.get("commit") or {}).get("oid", ""))
                 for r in pr["reviews"]["nodes"]]
    threads = []
    for t in pr["reviewThreads"]["nodes"]:
        cs = t["comments"]["nodes"]
        threads.append({"id": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"],
                        "path": t["path"], "line": t["line"],
                        "last_author": who(cs[-1]) if cs else None,
                        "first_url": cs[0]["url"] if cs else None})
        activity += [item("thread_comment", c, path=t["path"], line=t["line"], thread=t["id"]) for c in cs]
    return {
        "fields": {
            "author": who(pr), "auto_merge": bool(pr.get("autoMergeRequest")),
            "state": pr["state"], "merged": pr["merged"], "mergedBy": (pr.get("mergedBy") or {}).get("login"),
            "draft": pr["isDraft"], "mergeable": pr["mergeable"], "mergeState": pr["mergeStateStatus"],
            "reviewDecision": pr["reviewDecision"], "head": pr["headRefOid"][:8], "branch": pr["headRefName"],
            "ci": rollup.get("state"), "ci_failing": sorted(f["name"] for f in failing), "ci_pending": pending,
            "requested": sorted((r["requestedReviewer"] or {}).get("login") or (r["requestedReviewer"] or {}).get("name") or "?"
                                for r in pr["reviewRequests"]["nodes"]),
            "closing_issues": sorted(n["number"] for n in pr["closingIssuesReferences"]["nodes"]),
        },
        "ci_failing_detail": failing,
        "unresolved_threads": [t for t in threads if not t["resolved"]],
        "activity": activity,
        "title": pr["title"],
    }


def issue_snapshot(iss):
    return {
        "fields": {"state": iss["state"], "stateReason": iss["stateReason"],
                   "labels": sorted(l["name"] for l in iss["labels"]["nodes"])},
        "activity": [item("comment", c) for c in iss["comments"]["nodes"]],
        "unresolved_threads": [], "title": iss["title"],
    }


def diff(prev, cur):
    events = []
    if prev is None:
        return events
    for k, v in cur["fields"].items():
        if prev["fields"].get(k) != v:
            events.append({"kind": "field", "field": k, "old": prev["fields"].get(k), "new": v})
    seen = {(a["kind"], a["id"]) for a in prev["activity"]}
    seen |= {tuple(x) for x in prev.get("seen", [])}
    events += [a for a in cur["activity"] if (a["kind"], a["id"]) not in seen]
    return events


def ready_to_merge(f):
    return (f.get("state") == "OPEN" and not f.get("draft") and f.get("reviewDecision") == "APPROVED"
            and f.get("ci") == "SUCCESS" and not f.get("ci_failing") and f.get("mergeable") == "MERGEABLE")


def save(path, data):
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def watch_targets(watch, ledger):
    prs = {str(n): dict(m) for n, m in watch.get("prs", {}).items()}
    for n, reviewed in ledger.get("prs", {}).items():
        # The ledger is authoritative even when a prior watch retired this PR.
        prs.setdefault(str(n), {})["reviewed"] = reviewed
    return [("pr", int(n), m) for n, m in prs.items()] + [
        ("issue", int(n), m) for n, m in watch.get("issues", {}).items()]


def review_cutoff(cur, meta):
    head = meta.get("last_reviewed_head", "")
    dates = [a["at"] for a in cur["activity"] if a.get("kind") == "review"
             and a.get("author") in OURS and a.get("at")
             and head and a.get("commit", "")[:8] == head[:8]]
    return max(dates, default=last_review_at(meta))


def reviewed_events(prev, cur, meta):
    old, new = meta.get("last_reviewed_head"), cur["fields"].get("head")
    events = diff(prev, cur)
    if prev is not None and prev.get("review_watch_head") == old:
        return events
    if old and new and old[:8] != new[:8] and not any(e.get("field") == "head" for e in events):
        events.append({"kind":"field", "field":"head", "old":old[:8], "new":new})
    cutoff = timestamp(review_cutoff(cur, meta))
    seen = {(a["kind"], a.get("id")) for a in events if a["kind"] != "field"}
    events += [a for a in cur["activity"] if a.get("author") not in OURS
               and (a["kind"], a.get("id")) not in seen
               and (cutoff is None or (timestamp(a.get("at")) is not None and timestamp(a["at"]) > cutoff))]
    return events


def main():
    baseline = "--baseline" in sys.argv
    with open(CONFIG["watch"]) as fh:
        watch = json.load(fh)
    OURS.clear()
    OURS.update(ours(watch))
    with open(CONFIG["ledger"]) as fh:
        ledger = json.load(fh)
    os.makedirs(STATE_DIR, exist_ok=True)
    owner, repo = watch["repo"].split("/")
    report = {"polled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": [], "errors": []}
    targets = watch_targets(watch, ledger)
    snapshots = []  # saved only after the report is in the wake journal
    for kind, number, meta in targets:
        path = os.path.join(STATE_DIR, f"{kind}-{number}.json")
        try:
            data = gql(PR_Q if kind == "pr" else ISSUE_Q, owner, repo, number)
            cur = pr_snapshot(data["pullRequest"]) if kind == "pr" else issue_snapshot(data["issue"])
        except Exception as exc:  # surface, never swallow
            report["errors"].append({"kind": kind, "number": number, "error": str(exc)[:500]})
            continue
        prev = None
        if os.path.exists(path):
            with open(path) as fh:
                prev = json.load(fh)
        if prev and kind == "pr":
            # GitHub recomputes mergeability lazily after main moves; keep the last known value.
            for k in ("mergeable", "mergeState"):
                if cur["fields"][k] == "UNKNOWN":
                    cur["fields"][k] = prev["fields"].get(k, "UNKNOWN")
        reviewed = meta.get("reviewed")
        events = [] if baseline else (reviewed_events(prev, cur, reviewed) if reviewed is not None else diff(prev, cur))
        # First encounters with already-closed ledger PRs are baselines, not new retirements.
        if prev is None and reviewed is not None and cur["fields"]["state"] != "OPEN":
            events = []
        f = cur["fields"]
        rtm = kind == "pr" and ready_to_merge(f)
        rtm_new = rtm and (prev is None or (prev.get("rtm_head") != f["head"]))
        cur["rtm_head"] = f["head"] if rtm else (prev or {}).get("rtm_head")
        cur["seen"] = sorted({(a["kind"], a["id"]) for a in cur["activity"]}
                             | {tuple(x) for x in (prev or {}).get("seen", [])})
        report["items"].append({
            "kind": kind, "number": number, "title": cur["title"][:80], "owner": meta.get("owner"),
            "first_seen": prev is None, "fields": f, "events": events,
            "author": f.get("author"),
            "reviewed_since": review_cutoff(cur, reviewed) if reviewed is not None else None,
            "last_reviewed_head": (reviewed or {}).get("last_reviewed_head"),
            "ready_to_merge": rtm, "ready_to_merge_new": rtm_new and not baseline,
            "unresolved_threads": len(cur["unresolved_threads"]),
            "ci_failing_detail": cur.get("ci_failing_detail", []),
        })
        if reviewed is not None:
            cur["review_watch_head"] = reviewed.get("last_reviewed_head")
        snapshots.append((path, cur))
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "-R", watch["repo"], "--author", "@me", "--state", "open",
             "--json", "number,title,url,createdAt,isDraft,author,state", "--limit", "50"],
            capture_output=True, text=True, timeout=120, check=True)
        report["unwatched_open_prs"] = [p for p in json.loads(out.stdout)
                                        if p["number"] not in {n for k,n,m in targets if k == "pr"}
                                        and p["number"] not in watch.get("ignore_prs", [])]
    except Exception as exc:
        report["errors"].append({"kind": "pr-list", "error": str(exc)[:500]})
    if not baseline:
        journal_add(CONFIG.get("journal") or os.path.join(STATE_DIR, "wake-journal.json"), report)
    for path, cur in snapshots:
        save(path, cur)
    json.dump(report, sys.stdout, indent=1)
    print()
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
