#!/usr/bin/env python3
"""Is this outside action covered by a standing OK from the user? Pure and offline.

Usage:
  check_mandate.py --holder shepherd --action ff_fix_push --pr-author your-login --base main
                   [--repo your-org/your-repo] [--mandates PATH]
                   [--expect-policy-version V]
                   [--expect-head SHA --remote-head SHA]     (the caller runs ls-remote and
                                                              passes its value; this stays offline)
                   [--branch B --new-head SHA --local-head SHA]  (push actions: the caller
                                                              passes `git rev-parse <branch>`)
                   [--push-dest URL --push-url "$PU" --fetch-url "$FU" --url-rewrites "$RW"]
                                                             (push actions: the destination)
                   [--go-id WID (--pr N | --branch B) [--body-file F]] [--go-file PATH]

The pre-write re-check: publishers and armers run it IMMEDIATELY before each push, post or
arm command, with every option, and abort (STATUS: blocked) on anything but ALLOW:
  - paused                     -> PAUSED
  - mandate revoked or gone    -> DENY revoked:/unknown_action:
  - policy_version bumped      -> DENY policy_changed
  - head moved                 -> DENY head_moved
  - push: local branch moved   -> DENY local_head_moved (any action ending in _push: in the
                                  pre-write check, --new-head, --local-head and --branch are
                                  required, and the publisher then pushes exactly
                                  `git push <push_url> <new_head>:refs/heads/<branch>`)
  - push: destination unbound  -> DENY push_url_* / fetch_url_* / push_dest_*. In the
                                  pre-write check of a push action the caller passes the URL it
                                  will push to and ls-remote (--push-dest, the repo's entry in
                                  mandates.json "push_urls"), and what git reports for the worktree:
                                  --push-url "$(git remote get-url --push --all origin)",
                                  --fetch-url "$(git remote get-url --all origin)" and
                                  --url-rewrites "$(git config --get-regexp '^url\\.')". Each URL
                                  list must be exactly one URL equal to the authorised one (only a
                                  trailing ".git" is normalised), and there must be no
                                  insteadOf/pushInsteadOf rewrite at all; anything else is refused.
  - observe mode, no GO        -> OBSERVE (not ALLOW: do not write)
  - GO missing or not matching -> DENY go_missing / go_mismatch:<field>
GO records live in state/go.jsonl (bin/go_ledger.py add). Every row is validated by
bin/state_schema.py (grant or revoke row types): any invalid row, or a revocation of a GO that was
never granted, makes the whole ledger corrupt -> DENY state_corrupt:go.jsonl:<line>:<why>.
In observe mode a matching GO turns
OBSERVE into ALLOW <id> go=<wid>. A GO binds the action, repo, PR, branch, head (the remote
head expected just before the write), new_head (push actions), body sha256 (null when there is
no body), policy_version and expiry; a later `revoke` record cancels it. A record without
writer_role "conductor" (and a writer_terminal) is corrupt: only the conductor writes go.jsonl.
A one-off (the user's explicit OK, no mandate): a GO with granted_by "user" and a relayed_by
conductor id gives ALLOW oneoff go=<wid> for exactly that action and target, even for an action
in forbidden_without_user. Every other check (pause, policy, heads, revocation, expiry) still runs.

Prints exactly one line and exits:
  ALLOW <id>      0  covered; act
  OBSERVE <id>    3  covered, but the shepherd is in observe mode: send WOULD <id> and wait for GO
  PAUSED          4  a person paused all outside actions; do nothing outside
  DENY <reason>   1  not covered (or the file is unreadable): escalate to the conductor

Fails closed: any doubt is a DENY.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

DEFAULT_REPO = "your-org/your-repo"
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
EXIT = {"ALLOW": 0, "DENY": 1, "OBSERVE": 3, "PAUSED": 4}
SHA_RE = re.compile(r"[0-9a-f]{40}")
BRANCH_RE = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9_./-]{1,200}")


def is_push(action):
    return action.endswith("_push")


def norm_url(url):
    """The only normalisation: one trailing ".git" is dropped."""
    return url[:-4] if url.endswith(".git") else url


def check_destination(doc, repo, extra):
    """The push destination is the repo's authorised URL and nothing else. Pure."""
    urls = doc.get("push_urls")
    want = urls.get(repo) if isinstance(urls, dict) else None
    if not isinstance(want, str) or not want.strip():
        return f"push_url_unbound:{repo} (no mandates.json push_urls entry)"
    dest, rewrites = extra.get("push_dest"), extra.get("url_rewrites")
    observed = {"push_url": extra.get("push_url"), "fetch_url": extra.get("fetch_url")}
    if dest is None or rewrites is None or None in observed.values():
        return "push_dest_needs:--push-dest, --push-url, --fetch-url and --url-rewrites"
    if rewrites.strip():
        return "push_url_rewrite (an insteadOf/pushInsteadOf rule is configured)"
    for name, value in observed.items():
        lines = value.splitlines()
        if len(lines) != 1:
            return f"{name}_count:{len(lines)} (want exactly one URL)"
        if norm_url(lines[0]) != norm_url(want):
            return f"{name}_mismatch (not the authorised URL for {repo})"
    if norm_url(dest) != norm_url(want):
        return f"push_dest_mismatch (not the authorised URL for {repo})"
    return None


def check_extra(doc, action, extra, repo=None):
    """Pre-write re-checks. Return a DENY reason, or None. Pure."""
    want_v = extra.get("expect_policy_version")
    if want_v is not None and (doc.get("policy_version") is None or doc.get("policy_version") != want_v):
        return f"policy_changed:{doc.get('policy_version')}!={want_v}"
    exp, rem = extra.get("expect_head"), extra.get("remote_head")
    if (exp is None) != (rem is None):
        return "head_check_needs_both:--expect-head and --remote-head"
    if exp is not None:
        if not SHA_RE.fullmatch(exp) or not SHA_RE.fullmatch(rem):
            return "head_check_bad_sha (want 40 lowercase hex)"
        if exp != rem:
            return f"head_moved:expected={exp[:8]} remote={rem[:8]}"
    new, loc, br = extra.get("new_head"), extra.get("local_head"), extra.get("branch")
    pre_write = exp is not None or extra.get("go_id") is not None
    if is_push(action) and pre_write and (new is None or loc is None or not br):
        return "push_pin_needs:--new-head, --local-head and --branch"
    if (new is None) != (loc is None):
        return "push_pin_needs_both:--new-head and --local-head"
    if new is not None:
        if not SHA_RE.fullmatch(new) or not SHA_RE.fullmatch(loc):
            return "push_pin_bad_sha (want 40 lowercase hex)"
        if new != loc:
            return f"local_head_moved:new_head={new[:8]} local={loc[:8]}"
    if br is not None and not BRANCH_RE.fullmatch(br):
        return "bad_branch"
    if is_push(action) and pre_write:
        return check_destination(doc, repo, extra)
    return None


def check_go(doc, action, extra, records, repo, now):
    """Return (None, record) when the GO record matches exactly, else (DENY reason, None). Pure."""
    wid = extra["go_id"]
    if (extra.get("pr") is None and not extra.get("branch")) or extra.get("expect_head") is None:
        return "go_needs:--pr or --branch, and --expect-head", None
    # records come from SS.load_go: every row is a valid grant or revocation (else the whole
    # ledger is state_corrupt and nothing gets here), so no damaged row can be skipped.
    rows = [r for r in records if r["id"] == wid]
    if not rows:
        return f"go_missing:{wid}", None
    go = rows[-1]  # the latest record for this id wins (a later revocation cancels a GO)
    if go["revoked"] is not False:
        return f"go_mismatch:revoked:{wid}", None
    want = {"action": action, "repo": repo,
            "pr": None if extra.get("pr") is None else str(extra["pr"]),
            "branch": extra.get("branch"), "head": extra["expect_head"],
            "new_head": extra.get("new_head"), "body_sha256": extra.get("body_sha256"),
            "policy_version": doc.get("policy_version")}
    for field, value in want.items():
        got = go.get(field)
        got = str(got) if field == "pr" and got is not None else got
        if got != value:
            return f"go_mismatch:{field}:{wid}", None
    expires = datetime.fromisoformat(go["expires_at"].replace("Z", "+00:00"))  # zoned: SS.GO_GRANT
    if now >= expires:
        return f"go_mismatch:expired:{wid}", None
    return None, go


def is_oneoff(go):
    """A user one-off: granted_by exactly "user". The row is a validated SS.GO_GRANT, so a user
    grant always names the conductor that relayed it (relayed_by, a non-blank string)."""
    return go is not None and go["granted_by"] == "user"


def decide(doc, holder, action, pr_author, base, repo, extra=None, go_records=(), now=None):
    """Return (word, detail). Pure: no I/O."""
    extra = extra or {}
    now = now or datetime.now(timezone.utc)
    if not isinstance(doc, dict) or not isinstance(doc.get("mandates"), list):
        return "DENY", "malformed_mandates"
    forbidden = doc.get("forbidden_without_user")  # missing or null would forbid nothing
    if not isinstance(forbidden, list) or not all(isinstance(x, str) and x for x in forbidden):
        return "DENY", "malformed_mandates:forbidden_without_user"
    if doc.get("paused") is not False:
        return "PAUSED", ""
    reason = check_extra(doc, action, extra, repo)
    if reason:
        return "DENY", reason
    go = None
    if extra.get("go_id") is not None:
        go_reason, go = check_go(doc, action, extra, go_records, repo, now)
        if go_reason:
            return "DENY", go_reason
    oneoff = ("ALLOW", f"oneoff go={extra.get('go_id')}") if is_oneoff(go) else None
    if action in forbidden:
        return oneoff or ("DENY", f"forbidden_without_user:{action}")
    candidates = [m for m in doc["mandates"] if isinstance(m, dict) and m.get("action") == action]
    if not candidates:
        return oneoff or ("DENY", f"unknown_action:{action}")
    first_reason = None
    for m in candidates:
        reason = _mismatch(m, holder, pr_author, base, repo)
        if reason is None:
            if holder == "conductor":
                return "ALLOW", m["id"]
            mode = doc.get("shepherd_mode")
            cond = m.get("conditions", {})  # absent: no conditions; null or a non-object: refused
            go_always = cond.get("go_always_required", False) if isinstance(cond, dict) else None
            if not isinstance(go_always, bool):  # "true" or a non-object would read as "no GO needed"
                return "DENY", f"malformed_mandates:conditions:{m.get('id')}"
            if go_always and go is None:
                return "OBSERVE", m["id"]  # e.g. clear_hold: the conductor's GO, even in act mode
            if mode == "observe":
                if go is not None:
                    return "ALLOW", f"{m['id']} go={extra['go_id']}"
                return "OBSERVE", m["id"]
            if mode == "act":
                return "ALLOW", m["id"]
            return "DENY", f"bad_mode:{mode}"
        first_reason = first_reason or reason
    return oneoff or ("DENY", first_reason)


def _mismatch(m, holder, pr_author, base, repo):
    mid = m.get("id", "?")
    if m.get("holder") != holder:
        return f"wrong_holder:{mid} holder={m.get('holder')}"
    if m.get("revoked") is not False:
        return f"revoked:{mid}"
    if m.get("granted_by") != "user":
        return f"not_user_granted:{mid}"
    scope = m.get("scope") or {}
    if scope.get("repo") != repo:
        return f"wrong_repo:{mid} repo={repo}"
    want_author = scope.get("pr_author")
    if want_author != "*" and want_author != pr_author:
        return f"wrong_author:{mid} author={pr_author}"
    if scope.get("base") != base:
        return f"wrong_base:{mid} base={base}"
    return None


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def load_go(path):
    """Rows of state/go.jsonl, every one validated by bin/state_schema.py. A missing file is no
    GO; any invalid row (even one for another GO id) raises SS.StateCorrupt: the whole ledger is
    corrupt, because a damaged row might be this GO's revocation."""
    return SS.load_go(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a standing OK in mandates.json.")
    ap.add_argument("--holder", required=True, choices=["conductor", "shepherd"])
    ap.add_argument("--action", required=True)
    ap.add_argument("--pr-author", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--mandates", default=os.path.join(KIT, "mandates.json"))
    ap.add_argument("--expect-policy-version")
    ap.add_argument("--expect-head")
    ap.add_argument("--remote-head")
    ap.add_argument("--go-id")
    ap.add_argument("--pr")
    ap.add_argument("--body-file")
    ap.add_argument("--go-file")
    ap.add_argument("--branch")
    ap.add_argument("--new-head")
    ap.add_argument("--local-head")
    ap.add_argument("--push-dest", help="push actions: the URL the push and ls-remote use")
    for f in ("--push-url", "--fetch-url"):  # repeated flags are joined: more than one URL refuses
        ap.add_argument(f, action="append", help="push actions: git remote get-url output")
    ap.add_argument("--url-rewrites", action="append", help="push actions: git config --get-regexp '^url\\.'")
    a = ap.parse_args(argv)
    joined = {k: None if v is None else "\n".join(v)
              for k, v in (("push_url", a.push_url), ("fetch_url", a.fetch_url), ("url_rewrites", a.url_rewrites))}
    extra = {"expect_policy_version": a.expect_policy_version, "expect_head": a.expect_head,
             "remote_head": a.remote_head, "go_id": a.go_id, "pr": a.pr, "body_sha256": None,
             "branch": a.branch, "new_head": a.new_head, "local_head": a.local_head,
             "push_dest": a.push_dest, **joined}
    try:
        with open(a.mandates) as fh:
            doc = json.load(fh)
        if a.body_file:
            with open(a.body_file, "rb") as fh:
                extra["body_sha256"] = hashlib.sha256(fh.read()).hexdigest()
        go_records = load_go(a.go_file or os.path.join(state_dir(), "go.jsonl")) if a.go_id else []
    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>
        word, detail = "DENY", str(exc)
    except (OSError, ValueError) as exc:
        word, detail = "DENY", f"unreadable_input:{type(exc).__name__}"
    else:
        word, detail = decide(doc, a.holder, a.action, a.pr_author, a.base, a.repo, extra, go_records)
    print(f"{word} {detail}".rstrip())
    return EXIT[word]


if __name__ == "__main__":
    sys.exit(main())
