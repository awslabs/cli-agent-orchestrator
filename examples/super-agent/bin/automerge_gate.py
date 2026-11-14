#!/usr/bin/env python3
"""May the shepherd arm squash auto-merge, or update the branch, on this PR right now?
Offline: every GitHub fact arrives as a file the shepherd fetched just before calling.

  automerge_gate.py --action arm|update_branch
      --pr FILE               gh pr view <n> -R your-org/your-repo --json number,author,
                              baseRefName,isDraft,headRefOid,reviewDecision,statusCheckRollup,
                              additions,deletions,mergeStateStatus,autoMergeRequest
      --reviews FILE          gh api --paginate --slurp repos/{repo}/pulls/<n>/reviews
      --commits FILE          gh api --paginate --slurp repos/{repo}/pulls/<n>/commits
      --issue-comments FILE   gh api --paginate --slurp repos/{repo}/issues/<n>/comments
      --review-comments FILE  gh api --paginate --slurp repos/{repo}/pulls/<n>/comments
      --required FILE         an envelope {"endpoint", "http_status", "body"} written by
                              bin/api_envelope.py from `gh api -i`; pass it twice, once for
                              repos/{repo}/rules/branches/main and once for
                              repos/{repo}/branches/main/protection (classic). Verified only when
                              rules is 200 with a valid rule shape, and classic is 200 with valid
                              required_status_checks contexts/checks or 404 "Branch not protected".
                              Anything else: WAIT required_contexts_unverified:<why>
      --identities FILE       {"ours": [logins], "bots": [logins], "bot_suffixes": ["[bot]"]},
                              built from watch.json "identities"
      --mandates FILE         (default: <kit>/mandates.json) for scope, max_changed_lines, never_prs
      --holds FILE            (default: <kit>/state/holds.json) persisted holds, read and written

Paginated files may hold one JSON array, an array of pages (--slurp), or pages back to back.

Prints one line and exits:
  ARM <head> lines=<n>            0  --action arm: arm now, pinned to <head> (CLEAN, HAS_HOOKS
                                     or BEHIND; the auto-merge rule's order: arm first, update_branch later)
  UPDATE_BRANCH <head> lines=<n>  0  --action update_branch: BEHIND, armed or not: update now
                                     (merge, never rebase), then STOP

An approval covers the head when each reviewer's latest non-COMMENTED review is APPROVED on the
last non-merge commit or a later one, and every commit after the last non-merge one is a merge of
main (this assumes branch protection keeps approvals on a merge from main; a fix push dismisses them).
  DENY <reason>                   1  out of scope, or unreadable input; never act here
  WAIT <reasons>                  3  in scope, not yet; re-check on a later wake
  HOLD_FINDINGS <detail>          5  a finding (or NEEDS_READ) inside the window, or a persisted hold
  ESCALATE_SIZE lines=<n> cap=<c> 6  otherwise eligible; the conductor takes a second look

The window (rules/hold-on-findings.md) starts at the committer date of the last NON-MERGE
commit of the PR: a main-merge or update-branch never clears an earlier finding. Inside it,
every review (any state, any commit_id) and every issue or review comment by someone other
than us counts unless its body is trivially empty: findings keywords give HOLD_FINDINGS,
anything else HOLD_FINDINGS NEEDS_READ. Bot posts hold only on findings keywords. A hold is
saved in holds.json keyed by PR and the last non-merge sha, and is honoured on every later run
(and after a restart) until a newer non-merge commit lands. holds.json is read and written through
bin/state_schema.py: any invalid hold or _cleared row makes the whole file corrupt
(DENY state_corrupt:holds.json:<row>:<why>), never "no hold" or "not cleared".
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _state_schema():
    """bin/state_schema.py, loaded once per process (every script shares its classes)."""
    path = os.path.join(KIT, "bin", "state_schema.py")
    mod = sys.modules.get("kit_state_schema")
    if mod is None or getattr(mod, "__file__", None) != path:
        spec = importlib.util.spec_from_file_location("kit_state_schema", path)
        if spec is None or spec.loader is None:
            raise ImportError("state_schema.py not found next to this script")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["kit_state_schema"] = mod
        spec.loader.exec_module(mod)
    return mod


SS = _state_schema()
NEVER_PRS: set = set()  # a code floor (empty in the sample); mandates.json never_prs adds to it
DEFAULT_CAP = 1500
OK_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
PENDING_STATES = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
FINDING = re.compile(r"\bP[0-3]\b|\bfix(?:es|ed|ing)?\b|\bshould\b|\bnits?\b", re.IGNORECASE)
CLEAN_BODIES = {"", "lgtm", "looks good", "looks good to me", "approved", "approve", "ship it",
                "thanks", "thank you"}
# REAL findings (no clear path; only a newer non-merge commit or the user's decline releases
# them) are exactly: (a) a non-zero severity count ("1 P3", "(0 P1, 2 P2)", "P2: 1"); (b) a
# severity-tagged item at the start of a line ("1. **P3:", "- P2 —", "P1:", "nit:"); (c) a
# Findings/Issues heading followed by a list item before the next heading. Any other mention of a
# tag ("The previous P3 is fixed.", "0 P1, 0 P2, 0 P3") still holds, but a conductor GO can clear it.
NONZERO_COUNT = re.compile(r"(?<![\w.])0*[1-9][0-9]*[ \t]*(?:x[ \t]*)?(?:P[0-3]s?|nits?)\b"
                           r"|\bP[0-3][ \t]*[:=][ \t]*0*[1-9][0-9]*(?![\w.])", re.IGNORECASE)
LINE_TAG = re.compile(r"^[ \t>]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+)?[*_`\[(]*"
                      r"(?:P[0-3]|(?P<nit>nits?))(?![A-Za-z0-9])(?P<close>[*_`\])]*)[ \t]*(?P<rest>.*)$",
                      re.IGNORECASE)
SEPARATORS = (":", "-", "\u2014", "\u2013", ".", ")")
HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*)$|^[ \t]*(?:\*\*|__)(?P<bold>[^*_]+?)(?:\*\*|__)[ \t]*:?[ \t]*$")
BARE_FINDINGS = re.compile(r"^[ \t]*(?:findings?|issues?)[ \t]*:[ \t]*$", re.IGNORECASE)
FINDINGS_TITLE = re.compile(r"^[ \t*_`]*(?:findings?|issues?)\b", re.IGNORECASE)
LIST_ITEM = re.compile(r"^[ \t>]*(?:[-*+]|[0-9]+[.)])[ \t]+\S")
FENCE = re.compile(r"^[ \t]{0,3}(?:```|~~~)")
ITEM_KINDS = ("review", "issue_comment", "review_comment")
CLEARED = "_cleared"
EXIT = {"ARM": 0, "UPDATE_BRANCH": 0, "DENY": 1, "WAIT": 3, "HOLD_FINDINGS": 5, "ESCALATE_SIZE": 6}
REQUIRED = ("number", "author", "baseRefName", "isDraft", "headRefOid", "reviewDecision",
            "statusCheckRollup", "additions", "deletions", "mergeStateStatus", "autoMergeRequest")
ACTIONS = {"arm": "arm_automerge", "update_branch": "update_branch"}
# Approved PRs are usually BEHIND, and on a repo where update-branch keeps
# the approval, so arm on BEHIND first (the auto-merge rule's order) and update the branch on a later step.
MERGE_STATE_OK = {"arm": {"CLEAN", "HAS_HOOKS", "BEHIND"}, "update_branch": {"BEHIND"}}
MAIN_MERGE = re.compile(r"^Merge (?:remote-tracking )?branch '(?:origin/)?main'|^Merge (?:origin/)?main\b")


class BadInput(ValueError):
    pass


def classify_body(body):
    """'clean', 'findings' or 'needs_read'."""
    text = (body or "").strip()
    if text.lower().rstrip(".!").strip() in CLEAN_BODIES:
        return "clean"
    if FINDING.search(text):
        return "findings"
    return "needs_read"


def normalise_body(body):
    return (body or "").replace("\r\n", "\n").rstrip("\n")


def body_sha256(body):
    """sha256 of the canonical body (CRLF -> LF, trailing newlines dropped), as bin/holds.py writes it."""
    return hashlib.sha256(normalise_body(body).encode("utf-8")).hexdigest()


def line_tagged(line):
    """(b): a severity tag opens the line (after list markers, emphasis or brackets) and is
    followed by a separator or a closing emphasis. "P1: 0" is a zero count, not an item."""
    m = LINE_TAG.match(line)
    if not m:
        return False
    rest = m.group("rest")
    if m.group("nit"):
        return bool(m.group("close")) or rest.startswith(SEPARATORS)
    if not (m.group("close") or rest.startswith(SEPARATORS)):
        return False
    return not re.match(r"^[:=][ \t]*0+(?![0-9])", rest)


def real_findings(body):
    """True when the body lists current findings: (a), (b) or (c) above. Pure."""
    text = normalise_body(body)
    if NONZERO_COUNT.search(text):
        return True
    in_findings = in_fence = False
    for line in text.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if line_tagged(line):
            return True
        h = None if in_fence else HEADING.match(line)  # a "# comment" in code is not a heading
        if h:
            in_findings = bool(FINDINGS_TITLE.match(h.group("text") or h.group("bold") or ""))
        elif not in_fence and BARE_FINDINGS.match(line):
            in_findings = True
        elif in_findings and LIST_ITEM.match(line):
            return True
    return False


def strict_int(value, minimum):
    """An int (never a bool, float or string) >= minimum."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def bad_item(item):
    """Why a review or comment can't be judged, or None. A missing or null body would read as
    "clean" and a missing author as an approver nobody can name: both fail closed (DENY)."""
    user = item.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str) or not user["login"].strip():
        return "user"
    if not isinstance(item.get("body"), str):
        return "body"
    return None


def item_key(kind, item):
    return f"{kind}:{item.get('id')}"


def cleared_set(holds, number):
    """{(item_key, body_sha256)} cleared for this PR. holds comes from SS.load_holds: every
    _cleared row is complete (pr, item_id, body_sha256, reason, go_id, cleared_at)."""
    return {(r["item_id"], r["body_sha256"]) for r in holds.get(CLEARED, []) if r["pr"] == str(number)}


def is_cleared(kind, item, cleared):
    body = item.get("body")
    return ((item_key(kind, item), body_sha256(body)) in cleared and item.get("id") is not None
            and not real_findings(body))


def parse_time(value):
    if not isinstance(value, str) or not value:
        raise BadInput("missing timestamp")
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadInput(f"bad timestamp {value!r}") from exc
    if t.tzinfo is None:
        raise BadInput(f"timestamp without zone {value!r}")
    return t


def check_states(rollup):
    failed, pending = [], []
    for c in rollup:
        name = c.get("name") or c.get("context") or "?"
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            state = c.get("state")
            if state == "SUCCESS":
                continue
            (pending if state in PENDING_STATES else failed).append(name)
            continue
        if c.get("status") != "COMPLETED" or not c.get("conclusion"):
            pending.append(name)
        elif c.get("conclusion") not in OK_CONCLUSIONS:
            failed.append(name)
    return failed, pending


class Unverified(ValueError):
    pass


def _contexts(rows, where):
    if not isinstance(rows, list):
        raise Unverified(f"{where}_not_a_list")
    out = set()
    for c in rows:
        ctx = c.get("context") if isinstance(c, dict) else c if where.endswith("contexts") else None
        if not isinstance(ctx, str) or not ctx:
            raise Unverified(f"{where}_entry")
        out.add(ctx)
    return out


def required_contexts(envelopes, repo):
    """Union of required status-check contexts, from exactly one rules envelope and one classic
    envelope, each {endpoint, http_status, body}. Raises Unverified when the inventory is not
    proven complete. Pure."""
    want = {f"repos/{repo}/rules/branches/main": "rules",
            f"repos/{repo}/branches/main/protection": "classic"}
    got = {}
    for env in envelopes:
        if not isinstance(env, dict) or set(env) != {"endpoint", "http_status", "body"}:
            raise Unverified("not_an_envelope")
        ep = env["endpoint"].lstrip("/") if isinstance(env["endpoint"], str) else None
        if ep not in want:
            raise Unverified(f"unexpected_endpoint:{env['endpoint']}")
        if want[ep] in got:
            raise Unverified(f"duplicate:{want[ep]}")
        if not strict_int(env["http_status"], 100):
            raise Unverified(f"{want[ep]}_status={env['http_status']!r}")
        got[want[ep]] = env
    for name in ("rules", "classic"):
        if name not in got:
            raise Unverified(f"{name}_missing")
    rules, classic = got["rules"], got["classic"]
    if rules["http_status"] != 200:
        raise Unverified(f"rules_status={rules['http_status']}")
    if not isinstance(rules["body"], list):
        raise Unverified("rules_body")
    out = set()
    for rule in rules["body"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise Unverified("rules_entry")
        if rule["type"] == "required_status_checks":
            params = rule.get("parameters")
            if not isinstance(params, dict) or "required_status_checks" not in params:
                raise Unverified("rules_parameters")
            out |= _contexts(params["required_status_checks"], "rules_checks")
    body = classic["body"]
    if classic["http_status"] == 404:
        if not (isinstance(body, dict) and body.get("message") == "Branch not protected"):
            raise Unverified("classic_404_not_branch_not_protected")
    elif classic["http_status"] == 200:
        part = body.get("required_status_checks") if isinstance(body, dict) else None
        if not isinstance(part, dict) or not ("contexts" in part or "checks" in part):
            raise Unverified("classic_body")
        if "contexts" in part:
            out |= _contexts(part["contexts"], "classic_contexts")
        if "checks" in part:
            out |= _contexts(part["checks"], "classic_checks")
    else:
        raise Unverified(f"classic_status={classic['http_status']}")
    return out


def login_of(item):
    return (item.get("user") or {}).get("login")


def is_bot(item, ids):
    login = login_of(item) or ""
    return ((item.get("user") or {}).get("type") == "Bot" or login in ids["bots"]
            or any(login.endswith(s) for s in ids["bot_suffixes"]))


def last_non_merge(commits, head):
    if not commits:
        raise BadInput("no commits")
    for c in commits:
        if not isinstance(c, dict) or not isinstance(c.get("parents"), list) or not c.get("sha"):
            raise BadInput("malformed commit")
    if commits[-1]["sha"] != head:
        return None  # the commits file and the PR file disagree: refetch
    for i in range(len(commits) - 1, -1, -1):
        c = commits[i]
        if len(c["parents"]) == 1:
            date = ((c.get("commit") or {}).get("committer") or {}).get("date")
            return {"sha": c["sha"], "date": date, "at": parse_time(date), "index": i}
    raise BadInput("no non-merge commit")


def is_main_merge(c):
    message = ((c.get("commit") or {}).get("message") or "").splitlines()
    return len(c["parents"]) >= 2 and bool(message) and bool(MAIN_MERGE.match(message[0]))


def covered_commits(commits, anchor):
    """Commits an approval may sit on and still cover the head: the head itself, and walking back
    to the last non-merge commit, each earlier commit as long as everything after it is a merge of
    main (update-branch keeps the approval here; a fix push dismisses it). Pure."""
    covered = set()
    for j in range(len(commits) - 1, anchor["index"] - 1, -1):
        covered.add(commits[j]["sha"])
        if j == anchor["index"] or not is_main_merge(commits[j]):
            break
    return covered


def window_items(reviews, issue_comments, review_comments):
    """(kind, item, time-or-None). time None means unknown, which counts as inside the window."""
    out = []
    for r in reviews:
        if r.get("state") == "PENDING":
            continue  # an unsubmitted draft review; nobody else can see it
        out.append(("review", r, r.get("submitted_at")))
    for kind, rows in (("issue_comment", issue_comments), ("review_comment", review_comments)):
        for c in rows:
            stamps = [s for s in (c.get("created_at"), c.get("updated_at")) if s]
            out.append((kind, c, max(stamps) if stamps else None))
    return out


def scan_window(items, start, head, ids, cleared=frozenset()):
    """Return (findings, needs_read, held_items). Pure. A cleared item (same id, same body sha256,
    no severity-tagged finding) does not hold."""
    findings, needs, held = [], [], []
    for kind, item, stamp in items:
        login = login_of(item)
        if login in ids["ours"]:
            continue
        inside = stamp is None or parse_time(stamp) >= start
        if kind == "review" and item.get("commit_id") == head:
            inside = True
        if not inside:
            continue
        verdict = classify_body(item.get("body"))
        if verdict == "clean" or (verdict == "needs_read" and is_bot(item, ids)):
            continue
        if is_cleared(kind, item, cleared):
            continue
        held.append({"id": item_key(kind, item), "sha256": body_sha256(item.get("body")),
                     "real": real_findings(item.get("body")),
                     "body": normalise_body(item.get("body"))})
        where = f"{kind}:{login}" + (f":{item.get('state')}" if kind == "review" else "")
        if verdict == "findings":
            lines = [ln.strip() for ln in (item.get("body") or "").splitlines() if FINDING.search(ln)]
            findings.append(f"{where} {' | '.join(lines)[:200]} item={item_key(kind, item)} "
                            f"url={item.get('html_url')}")
        else:
            needs.append(f"{where} item={item_key(kind, item)} url={item.get('html_url')}")
    return findings, needs, held


def persisted_item_cleared(rec, current, cleared):
    """One item of a persisted hold. "Real" is always recomputed from the item's current body
    (or, when it is gone from the inputs, from the body saved with the hold); a persisted
    real flag is never trusted. Pure."""
    cur = current.get(rec["id"])  # rec is a validated SS.HELD_ITEM
    if cur is not None:
        body = cur.get("body")
    elif body_sha256(rec["body"]) == rec["sha256"]:
        body = rec["body"]
    else:
        return False  # gone, and nothing to re-judge: stays held
    return (rec["id"], body_sha256(body)) in cleared and not real_findings(body)


def decide(action, pr, reviews, commits, issue_comments, review_comments, required_docs, ids,
           limits, holds):
    """Return (word, detail, hold_change). hold_change is None, ("set", record) or ("clear",)."""
    missing = [k for k in REQUIRED if k not in pr]
    if missing:
        return "DENY", f"malformed_input missing={','.join(missing)}", None
    for name, rows in (("reviews", reviews), ("commits", commits),
                       ("issue_comments", issue_comments), ("review_comments", review_comments)):
        if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
            return "DENY", f"malformed_input {name}", None
        if name != "commits":
            for i, x in enumerate(rows):
                why = bad_item(x)
                if why:
                    return "DENY", f"malformed_input {name}[{i}].{why}", None
    if pr["autoMergeRequest"] is not None and not isinstance(pr["autoMergeRequest"], dict):
        return "DENY", f"malformed_input autoMergeRequest={pr['autoMergeRequest']!r}"[:120], None
    if not strict_int(pr["number"], 1):
        return "DENY", f"malformed_input number={pr['number']!r}", None
    for k in ("additions", "deletions"):
        if not strict_int(pr[k], 0):
            return "DENY", f"malformed_input {k}={pr[k]!r}", None
    head = pr["headRefOid"]
    number = pr["number"]
    author = (pr.get("author") or {}).get("login")
    scope_author = limits["pr_author"]
    if author not in ids["ours"] or (scope_author != "*" and author != scope_author):
        return "DENY", f"not_our_pr author={author}", None
    if pr["baseRefName"] != "main":
        return "DENY", f"base_not_main base={pr['baseRefName']}", None
    if pr["isDraft"] is not False:
        return "DENY", "draft", None
    if number in (NEVER_PRS | limits["never_prs"]):
        return "DENY", f"never_automerge_pr #{number}", None

    armed = pr.get("autoMergeRequest") is not None
    urgent = "URGENT auto-merge already armed; " if armed else ""
    anchor = last_non_merge(commits, head)
    if anchor is None:
        return "WAIT", f"inputs_out_of_sync commits_tail!=head={head[:8]}", None

    # Persisted hold: clears when a newer non-merge commit has landed, or when every item it
    # was saved for has been cleared by reading (bin/holds.py, conductor GO, same body sha256).
    change = None
    cleared = cleared_set(holds, number)
    items_now = window_items(reviews, issue_comments, review_comments)
    current = {item_key(kind, it): it for kind, it, _ in items_now if it.get("id") is not None}
    held = holds.get(str(number))
    if held is not None:  # a validated SS.HOLD: sha, date, head, detail, items (>= 1), recorded_at
        newer = held["sha"] != anchor["sha"] and anchor["at"] > parse_time(held["date"])
        read_clear = all(persisted_item_cleared(i, current, cleared) for i in held["items"])
        if not newer and not read_clear:
            return ("HOLD_FINDINGS", f"{urgent}persisted hold on non-merge {held['sha'][:8]} since "
                    f"{held['recorded_at']}: {held['detail'][:200]}", None)
        change = ("clear",)

    # hold-on-findings window: everything since the last non-merge commit, on any commit.
    findings, needs, held_items = scan_window(items_now, anchor["at"], head, ids, cleared)
    if findings or needs:
        detail = (f"{urgent}" + (" ; ".join(findings) if findings else
                                 "NEEDS_READ " + " ; ".join(needs)))[:600]
        record = {"sha": anchor["sha"], "date": anchor["date"], "head": head, "detail": detail,
                  "items": held_items}
        return "HOLD_FINDINGS", detail, ("set", record)

    waits = []
    if action == "arm" and armed:
        waits.append("auto_merge_already_on")
    if pr["reviewDecision"] == "CHANGES_REQUESTED":
        waits.append("changes_requested")
    elif pr["reviewDecision"] != "APPROVED":
        waits.append(f"review_decision={pr['reviewDecision']}")

    # Ordered by the real time; a submitted review without a readable time can't be ordered
    # (it would sort first and lose to an older approval): parse_time raises -> DENY.
    others = sorted((r for r in reviews if login_of(r) not in ids["ours"] and r.get("state") != "PENDING"),
                    key=lambda r: parse_time(r.get("submitted_at")))
    covered = covered_commits(commits, anchor)
    latest = {}
    for r in others:
        if r.get("state") in ("COMMENTED", "PENDING"):
            continue
        latest[login_of(r)] = r
    if not latest:
        waits.append("no_approval")
    for login, r in sorted(latest.items(), key=lambda kv: str(kv[0])):
        if r.get("state") == "CHANGES_REQUESTED":
            waits.append(f"changes_requested:{login}")
        elif r.get("state") != "APPROVED":
            waits.append(f"not_approved:{login}={r.get('state')}")
        elif r.get("commit_id") not in covered:
            waits.append(f"stale_approval:{login}@{(r.get('commit_id') or '')[:8]}")

    rollup = pr["statusCheckRollup"] or []
    if not rollup:
        waits.append("no_checks")
    failed, pending = check_states(rollup)
    if failed:
        waits.append("failed:" + ",".join(failed))
    if pending:
        waits.append("pending:" + ",".join(pending))
    try:
        required = required_contexts(required_docs, limits["repo"])
    except Unverified as exc:
        waits.append(f"required_contexts_unverified:{exc}")
    else:
        if not required:
            waits.append("required_contexts_empty")
        present = {c.get("name") or c.get("context") for c in rollup}
        absent = sorted(required - present)
        if absent:
            waits.append("missing_required:" + ",".join(absent))
    state = pr["mergeStateStatus"]
    if state not in MERGE_STATE_OK[action]:
        waits.append(f"merge_state={state}")
    if waits:
        return "WAIT", " ".join(waits), change

    lines = pr["additions"] + pr["deletions"]
    if lines > limits["cap"]:
        return "ESCALATE_SIZE", f"lines={lines} cap={limits['cap']}", change
    word = "ARM" if action == "arm" else "UPDATE_BRANCH"
    return word, f"{head} lines={lines}", change


def load_limits(path, action):
    with open(path) as fh:
        doc = json.load(fh)
    cap, never, author, repo = DEFAULT_CAP if action == "update_branch" else None, set(), None, None
    for m in doc.get("mandates") or []:
        if m.get("action") in ACTIONS.values():
            cond = m.get("conditions", {})
            if not isinstance(cond, dict):  # null or a list would read as "no cap, no never_prs"
                raise BadInput(f"conditions of {m.get('id')} is not an object")
            listed = cond.get("never_prs", [])  # a null or a string ("901") would add nothing
            if not isinstance(listed, list) or not all(strict_int(n, 1) for n in listed):
                raise BadInput(f"never_prs of {m.get('id')} is not a list of PR numbers")
            never |= set(listed)
            active = (m.get("revoked") is False and m.get("granted_by") == "user"
                      and m.get("holder") == "shepherd")
            # A revoked arm cap still restricts updates; revocation must never widen the cap.
            if (active or action == "update_branch") and m.get("action") == "arm_automerge":
                limit = cond.get("max_changed_lines", DEFAULT_CAP)
                if not strict_int(limit, 0):
                    raise BadInput(f"max_changed_lines of {m.get('id')} is not an integer")
                cap = limit if cap is None else min(cap, limit)
            if active and m.get("action") == ACTIONS[action]:
                author = (m.get("scope") or {}).get("pr_author")
                repo = (m.get("scope") or {}).get("repo")
    if not author or not isinstance(repo, str) or not repo:
        raise BadInput(f"no mandate scope for {ACTIONS[action]}")
    return {"cap": DEFAULT_CAP if cap is None else cap, "never_prs": never,
            "pr_author": author, "repo": repo}


def load_identities(path):
    with open(path) as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise BadInput("identities is not an object")
    ours, bots = doc.get("ours"), doc.get("bots", [])
    suffixes = doc.get("bot_suffixes", ["[bot]"])
    for v in (ours, bots, suffixes):
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise BadInput("identities lists must hold non-empty strings")
    if not ours:
        raise BadInput("identities.ours is empty")
    return {"ours": set(ours), "bots": set(bots), "bot_suffixes": tuple(suffixes)}


def load_pages(path):
    """One JSON array, an array of pages (gh --slurp), or pages back to back -> one list."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    dec, pos, values = json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        value, pos = dec.raw_decode(text, pos)
        values.append(value)
    if not values:
        raise BadInput(f"empty file {os.path.basename(path)}")
    if not all(isinstance(v, list) for v in values):
        raise BadInput(f"not a list {os.path.basename(path)}")
    rows = [x for v in values for x in v]
    if rows and all(isinstance(x, list) for x in rows):  # --slurp: a list of pages
        rows = [x for page in rows for x in page]
    return rows


def load_doc(path):
    """One JSON document (a required-contexts envelope)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def read_holds(path):
    """holds.json, every hold and _cleared row validated (SS.load_holds); {} when absent. Any
    invalid row raises SS.StateCorrupt: the whole file is corrupt (DENY), never "no hold"."""
    return SS.load_holds(path)


def write_hold(path, number, change):
    """Apply a hold change under an exclusive lock, with an atomic replace."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        holds = read_holds(path)
        if change[0] == "set":
            rec = dict(change[1])
            rec["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            prior = holds.get(str(number))
            if prior is not None and prior["sha"] == rec["sha"]:
                rec["recorded_at"] = prior["recorded_at"]
            holds[str(number)] = rec
        else:
            holds.pop(str(number), None)
        SS.check_holds_write(path, holds)  # never write a hold the readers would reject
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(holds, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Auto-merge / update-branch gate (the auto-merge and hold-on-findings rules).")
    ap.add_argument("--action", required=True, choices=sorted(ACTIONS))
    for f in ("--pr", "--reviews", "--commits", "--issue-comments", "--review-comments",
              "--identities"):
        ap.add_argument(f, required=True)
    ap.add_argument("--required", action="append", required=True)
    ap.add_argument("--mandates", default=os.path.join(KIT, "mandates.json"))
    ap.add_argument("--holds", default=None)
    a = ap.parse_args(argv)
    holds_path = a.holds or os.path.join(state_dir(), "holds.json")
    try:
        limits = load_limits(a.mandates, a.action)
        ids = load_identities(a.identities)
        with open(a.pr) as fh:
            pr = json.load(fh)
        if not isinstance(pr, dict):
            raise BadInput("pr file is not an object")
        reviews = load_pages(a.reviews)
        commits = load_pages(a.commits)
        issue_comments = load_pages(a.issue_comments)
        review_comments = load_pages(a.review_comments)
        required_docs = [load_doc(p) for p in a.required]
        holds = read_holds(holds_path)
        word, detail, change = decide(a.action, pr, reviews, commits, issue_comments,
                                      review_comments, required_docs, ids, limits, holds)
        if change is not None:
            write_hold(holds_path, pr["number"], change)
    except SS.StateCorrupt as exc:  # state_corrupt:holds.json:<row>:<why> (or row_refused:...)
        word, detail = "DENY", str(exc)
    except BadInput as exc:
        word, detail = "DENY", f"malformed_input {exc}"
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        word, detail = "DENY", f"unreadable_input:{type(exc).__name__}"
    print(f"{word} {detail}".rstrip())
    return EXIT[word]


if __name__ == "__main__":
    sys.exit(main())
