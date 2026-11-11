import json

import pytest

from conftest import base_env, run, write_json, ledger

HEAD = "a" * 40
OLD = "b" * 40
FIX0 = "c" * 40
MERGE = "d" * 40
NEWFIX = "e" * 40

ARM, WAIT, HOLD, ESCALATE, DENY = 0, 3, 5, 6, 1
UPDATE = 0

IDS = {"ours": ["your-login"], "bots": ["copilot-pull-request-reviewer", "github-advanced-security",
                                     "github-actions"], "bot_suffixes": ["[bot]"]}
RULES = [{"type": "deletion", "parameters": {}},
         {"type": "required_status_checks",
          "parameters": {"strict_required_status_checks_policy": True,
                         "required_status_checks": [{"context": "Unit"}, {"context": "cla"}]}}]
REPO = "your-org/your-repo"
RULES_EP = f"repos/{REPO}/rules/branches/main"
CLASSIC_EP = f"repos/{REPO}/branches/main/protection"
NOT_PROTECTED = {"message": "Branch not protected",
                 "documentation_url": "https://docs.github.com/rest/branches/branch-protection#get-branch-protection",
                 "status": "404"}


def rules_env(body=None, status=200, endpoint=RULES_EP):
    return {"endpoint": endpoint, "http_status": status, "body": RULES if body is None else body}


def classic_env(body=None, status=404, endpoint=CLASSIC_EP):
    return {"endpoint": endpoint, "http_status": status, "body": NOT_PROTECTED if body is None else body}


def classic_200(contexts=None, checks=None):
    part = {"url": "https://api.github.com/x", "strict": True}
    if contexts is not None:
        part["contexts"] = contexts
    if checks is not None:
        part["checks"] = checks
    return classic_env({"url": "https://api.github.com/y", "required_status_checks": part}, status=200)


def pr(**over):
    d = {
        "number": 101, "author": {"login": "your-login"}, "baseRefName": "main",
        "isDraft": False, "headRefOid": HEAD, "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "Unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "Docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"__typename": "CheckRun", "name": "Lint", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"__typename": "StatusContext", "context": "cla", "state": "SUCCESS"},
        ],
        "additions": 40, "deletions": 10, "mergeStateStatus": "CLEAN", "autoMergeRequest": None,
    }
    d.update(over)
    return d


def review(login="maintainer-login", state="APPROVED", commit=HEAD, body="", at="2026-11-08T10:00:00Z",
           kind="User", rid=1001):
    return {"id": rid, "user": {"login": login, "type": kind}, "state": state, "commit_id": commit,
            "body": body, "submitted_at": at, "html_url": f"https://x/review/{login}-{at}"}


def comment(login="maintainer-login", body="", at="2026-11-08T10:30:00Z", updated=None, kind="User"):
    return {"user": {"login": login, "type": kind}, "body": body, "created_at": at,
            "updated_at": updated or at, "html_url": f"https://x/comment/{login}-{at}"}


def commit(sha, at, merge=False, message=None):
    parents = [{"sha": "1" * 40}, {"sha": "2" * 40}] if merge else [{"sha": "1" * 40}]
    if message is None:
        message = "Merge branch 'main' into fix/x" if merge else "fix: something"
    return {"sha": sha, "parents": parents,
            "commit": {"committer": {"date": at}, "author": {"date": at}, "message": message}}


COMMITS = [commit(OLD, "2026-11-07T08:00:00Z"), commit(HEAD, "2026-11-08T09:30:00Z")]


def gate(tmp_path, p, reviews, mandates=None, action="arm", commits=None, issue_comments=(),
         review_comments=(), required=None, ids=None, raw=None):
    """raw: {name: text} written verbatim instead of JSON for that input."""
    raw = raw or {}

    def put(name, data):
        f = tmp_path / f"{name}.json"
        if name in raw:
            f.write_text(raw[name])
        else:
            write_json(f, data)
        return f

    pf = tmp_path / "pr.json"
    if isinstance(p, str):
        pf.write_text(p)
    else:
        write_json(pf, p)
    args = ["--action", action, "--pr", pf, "--reviews", put("reviews", reviews),
            "--commits", put("commits", COMMITS if commits is None else commits),
            "--issue-comments", put("issue_comments", list(issue_comments)),
            "--review-comments", put("review_comments", list(review_comments)),
            "--identities", put("identities", IDS if ids is None else ids)]
    for i, doc in enumerate(required if required is not None else [rules_env(), classic_env()]):
        args += ["--required", put(f"required{i}", doc)]
    if mandates is not None:
        args += ["--mandates", write_json(tmp_path / "mandates.json", mandates)]
    r = run("automerge_gate.py", *args, env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def holds(tmp_path):
    f = tmp_path / "state" / "holds.json"
    return json.loads(f.read_text()) if f.exists() else {}


def expect(result, code, word):
    rc, out = result
    assert rc == code, out
    assert out.split()[0] == ("HOLD_FINDINGS" if word == "HOLD" else word), out
    return out


def test_happy_path_arms(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()]), ARM, "ARM")
    assert out == f"ARM {HEAD} lines=50"


def test_stale_approval_on_older_commit(tmp_path):
    out = expect(gate(tmp_path, pr(), [review(commit=OLD)]), WAIT, "WAIT")
    assert "stale" in out


def test_one_reviewer_current_another_stale(tmp_path):
    rs = [review(), review(login="other-reviewer", commit=OLD)]
    assert "stale" in expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")


def test_pending_check_run(tmp_path):
    p = pr()
    p["statusCheckRollup"].append({"__typename": "CheckRun", "name": "CodeQL",
                                   "status": "IN_PROGRESS", "conclusion": None})
    assert "pending" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_pending_status_context(tmp_path):
    p = pr()
    p["statusCheckRollup"].append({"__typename": "StatusContext", "context": "ext", "state": "PENDING"})
    assert "pending" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_completed_check_without_conclusion_is_pending(tmp_path):
    p = pr()
    p["statusCheckRollup"].append({"__typename": "CheckRun", "name": "x", "status": "COMPLETED",
                                   "conclusion": None})
    assert "pending:x" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_failed_check(tmp_path):
    p = pr()
    p["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    assert "failed" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_failed_status_context(tmp_path):
    p = pr()
    p["statusCheckRollup"][3]["state"] = "ERROR"
    assert "failed" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_no_checks_reported_waits(tmp_path):
    assert "no_checks" in expect(gate(tmp_path, pr(statusCheckRollup=[]), [review()]), WAIT, "WAIT")


def test_changes_requested_review(tmp_path):
    rs = [review(), review(login="other-reviewer", state="CHANGES_REQUESTED")]
    assert "changes_requested" in expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")


def test_changes_requested_decision(tmp_path):
    out = expect(gate(tmp_path, pr(reviewDecision="CHANGES_REQUESTED"), [review()]), WAIT, "WAIT")
    assert "changes_requested" in out


def test_review_required_decision(tmp_path):
    out = expect(gate(tmp_path, pr(reviewDecision="REVIEW_REQUIRED"), [review()]), WAIT, "WAIT")
    assert "review_decision" in out


def test_no_approval_at_all(tmp_path):
    assert "no_approval" in expect(gate(tmp_path, pr(), []), WAIT, "WAIT")


def test_latest_non_commented_review_wins(tmp_path):
    rs = [review(state="CHANGES_REQUESTED", commit=OLD, at="2026-11-07T01:00:00Z"),
          review(at="2026-11-08T01:00:00Z"),
          review(state="COMMENTED", at="2026-11-08T02:00:00Z")]
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_approval_then_changes_requested(tmp_path):
    rs = [review(at="2026-11-07T01:00:00Z"),
          review(state="CHANGES_REQUESTED", at="2026-11-08T01:00:00Z")]
    expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")


def test_reviews_sorted_by_time_not_list_order(tmp_path):
    rs = [review(state="CHANGES_REQUESTED", at="2026-11-08T05:00:00Z"),
          review(at="2026-11-08T01:00:00Z")]
    expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")


def test_dismissed_latest_review_is_not_approval(tmp_path):
    rs = [review(login="other-reviewer", state="DISMISSED"), review()]
    assert "not_approved:other-reviewer" in expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")


def test_draft_denied(tmp_path):
    assert "draft" in expect(gate(tmp_path, pr(isDraft=True), [review()]), DENY, "DENY")


def test_not_our_pr_denied(tmp_path):
    out = expect(gate(tmp_path, pr(author={"login": "other-author"}), [review()]), DENY, "DENY")
    assert "author" in out


def test_stacked_base_denied(tmp_path):
    out = expect(gate(tmp_path, pr(baseRefName="feat/x"), [review()]), DENY, "DENY")
    assert "base" in out


def never_mandates(numbers):
    """A mandates doc whose M6 lists never_prs; the sample's code floor is empty."""
    return {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE,
                          "conditions": {"never_prs": list(numbers)}}]}


@pytest.mark.parametrize("n", [901, 902, 903])
def test_never_automerge_prs_denied(tmp_path, n):
    m = never_mandates([901, 902, 903])
    assert expect(gate(tmp_path, pr(number=n), [review()], mandates=m), DENY, "DENY") == f"DENY never_automerge_pr #{n}"


def test_the_sample_has_no_hidden_never_floor(tmp_path):
    expect(gate(tmp_path, pr(number=901), [review()]), ARM, "ARM")


def test_conflicting_waits(tmp_path):
    out = expect(gate(tmp_path, pr(mergeStateStatus="DIRTY"), [review()]), WAIT, "WAIT")
    assert "merge_state=DIRTY" in out


def test_size_over_cap_escalates(tmp_path):
    out = expect(gate(tmp_path, pr(additions=1400, deletions=101), [review()]), ESCALATE, "ESCALATE_SIZE")
    assert "1501" in out and "1500" in out


def test_size_at_cap_arms(tmp_path):
    expect(gate(tmp_path, pr(additions=1400, deletions=100), [review()]), ARM, "ARM")


SCOPE = {"repo": "your-org/your-repo", "pr_author": "your-login", "base": "main"}


def test_size_cap_read_from_mandate(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE,
                       "conditions": {"max_changed_lines": 30}}]}
    expect(gate(tmp_path, pr(), [review()], mandates=m), ESCALATE, "ESCALATE_SIZE")


def test_size_cap_defaults_to_1500_without_condition(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE, "conditions": {}}]}
    expect(gate(tmp_path, pr(additions=1501, deletions=0), [review()], mandates=m), ESCALATE, "ESCALATE_SIZE")
    expect(gate(tmp_path, pr(additions=1500, deletions=0), [review()], mandates=m), ARM, "ARM")


@pytest.mark.parametrize("body", [
    "Looks good. P3: rename the helper.",
    "nit: trailing space in README",
    "You should also cover the 424 case.",
    "One fix needed in the docstring.",
    "**P2-1** the relay leaks paths",
])
def test_approval_with_findings_holds(tmp_path, body):
    out = expect(gate(tmp_path, pr(), [review(body=body)]), HOLD, "HOLD")
    assert "maintainer-login" in out


@pytest.mark.parametrize("body", ["", "LGTM", "Looks good to me!", "  lgtm.  ", "Approved"])
def test_clean_approval_bodies_arm(tmp_path, body):
    expect(gate(tmp_path, pr(), [review(body=body)]), ARM, "ARM")


def test_unclassified_approval_body_needs_read(tmp_path):
    out = expect(gate(tmp_path, pr(), [review(body="Interesting approach overall.")]), HOLD, "HOLD")
    assert "NEEDS_READ" in out


def test_commented_review_on_head_with_findings_holds(tmp_path):
    rs = [review(), review(login="copilot-pull-request-reviewer", state="COMMENTED",
                           body="Consider this nit about naming", at="2026-11-08T11:00:00Z")]
    expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")


def test_findings_on_older_commit_do_not_hold_a_clean_reapproval(tmp_path):
    rs = [review(commit=OLD, body="P3: rename", at="2026-11-07T00:00:00Z"),
          review(at="2026-11-08T00:00:00Z")]
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_findings_hold_even_when_size_is_over_cap(tmp_path):
    expect(gate(tmp_path, pr(additions=5000), [review(body="nit: x")]), HOLD, "HOLD")


def test_auto_merge_already_on_waits(tmp_path):
    out = expect(gate(tmp_path, pr(autoMergeRequest={"mergeMethod": "SQUASH"}), [review()]), WAIT, "WAIT")
    assert "already" in out


def test_findings_with_auto_merge_armed_still_hold(tmp_path):
    p = pr(autoMergeRequest={"mergeMethod": "SQUASH"})
    assert "URGENT" in expect(gate(tmp_path, p, [review(body="P3: x")]), HOLD, "HOLD")


def test_malformed_pr_file_denies(tmp_path):
    expect(gate(tmp_path, "{oops", [review()]), DENY, "DENY")


def test_missing_field_denies(tmp_path):
    p = pr()
    del p["headRefOid"]
    expect(gate(tmp_path, p, [review()]), DENY, "DENY")



def test_authors_own_thread_replies_are_ignored(tmp_path):
    rs = [review(), review(login="your-login", state="COMMENTED", body="Fixed in c0de0001, thanks.",
                           at="2026-11-08T12:00:00Z")]
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_authors_own_review_is_not_an_approval(tmp_path):
    expect(gate(tmp_path, pr(), [review(login="your-login")]), WAIT, "WAIT")


def test_unreadable_mandates_file_denies(tmp_path):
    gate(tmp_path, pr(), [review()])
    args = ["--action", "arm", "--pr", tmp_path / "pr.json", "--reviews", tmp_path / "reviews.json",
            "--commits", tmp_path / "commits.json", "--issue-comments", tmp_path / "issue_comments.json",
            "--review-comments", tmp_path / "review_comments.json", "--identities",
            tmp_path / "identities.json", "--required", tmp_path / "required0.json",
            "--mandates", tmp_path / "missing.json"]
    r = run("automerge_gate.py", *args, env=base_env(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("DENY")


def test_mandate_without_scope_for_the_action_denies(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "conditions": {}}]}
    out = expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY")
    assert "no mandate scope" in out


def test_mandate_scope_author_must_match(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "conditions": {},
                       "scope": dict(SCOPE, pr_author="someone")}]}
    expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY")


# --- the hold-on-findings window, whatever commit a review sits on ---

def test_later_review_on_an_old_sha_holds(tmp_path):
    """The review's probe: clean approval on head, then a COMMENTED review on the old sha."""
    rs = [review(), review(login="other-reviewer", state="COMMENTED", commit=OLD,
                           body="P2: fix this before merge", at="2026-11-08T11:00:00Z")]
    out = expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")
    assert "other-reviewer" in out and "P2" in out


def test_old_sha_review_without_keywords_needs_read(tmp_path):
    rs = [review(), review(login="other-reviewer", state="COMMENTED", commit=OLD,
                           body="Have you considered the retry path?", at="2026-11-08T11:00:00Z")]
    assert "NEEDS_READ" in expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")


def test_review_before_the_window_does_not_hold(tmp_path):
    rs = [review(login="other-reviewer", state="COMMENTED", commit=OLD, body="P2: x",
                 at="2026-11-08T09:29:59Z"), review()]
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_review_at_the_window_start_holds(tmp_path):
    rs = [review(login="other-reviewer", state="COMMENTED", commit=OLD, body="P2: x",
                 at="2026-11-08T09:30:00Z"), review()]
    expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")


def test_issue_comment_in_window_holds(tmp_path):
    cs = [comment(body="One more nit: rename the flag.")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), HOLD, "HOLD")


def test_inline_review_comment_in_window_holds(tmp_path):
    cs = [comment(body="This should close the file handle.")]
    expect(gate(tmp_path, pr(), [review()], review_comments=cs), HOLD, "HOLD")


def test_inline_comment_without_keywords_needs_read(tmp_path):
    cs = [comment(body="What happens on a 404 here?")]
    assert "NEEDS_READ" in expect(gate(tmp_path, pr(), [review()], review_comments=cs), HOLD, "HOLD")


def test_comment_before_the_window_does_not_hold(tmp_path):
    cs = [comment(body="P3: rename", at="2026-11-08T08:00:00Z")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), ARM, "ARM")


def test_comment_edited_inside_the_window_holds(tmp_path):
    cs = [comment(body="P3: rename", at="2026-11-08T08:00:00Z", updated="2026-11-08T10:00:00Z")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), HOLD, "HOLD")


def test_comment_without_timestamp_counts(tmp_path):
    c = comment(body="nit: x")
    del c["created_at"], c["updated_at"]
    expect(gate(tmp_path, pr(), [review()], issue_comments=[c]), HOLD, "HOLD")


def test_trivially_empty_comment_does_not_hold(tmp_path):
    cs = [comment(body="Thanks!"), comment(body="  "), comment(body="LGTM")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), ARM, "ARM")


def test_our_own_comments_do_not_hold(tmp_path):
    cs = [comment(login="your-login", body="Fixed in c0de0001; the P2 is addressed.")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs, review_comments=cs), ARM, "ARM")


@pytest.mark.parametrize("login,kind", [("github-actions", "User"), ("codecov[bot]", "Bot"),
                                        ("some-app", "Bot"), ("renovate[bot]", "User")])
def test_bot_comment_without_findings_is_ignored(tmp_path, login, kind):
    cs = [comment(login=login, kind=kind, body="Coverage report: 91% (+0.2)")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), ARM, "ARM")


def test_bot_comment_with_findings_holds(tmp_path):
    cs = [comment(login="copilot-pull-request-reviewer", body="nit: typo in the docstring")]
    expect(gate(tmp_path, pr(), [review()], review_comments=cs), HOLD, "HOLD")


def test_identities_are_an_input_not_hard_coded(tmp_path):
    ids = {"ours": ["your-login", "your-login-alt"], "bots": [], "bot_suffixes": ["[bot]"]}
    cs = [comment(login="your-login-alt", body="P2 fixed")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs, ids=ids), ARM, "ARM")
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), HOLD, "HOLD")


@pytest.mark.parametrize("ids", [{"bots": []}, {"ours": []}, {"ours": "your-login"}, [], {"ours": [""]}])
def test_bad_identities_deny(tmp_path, ids):
    out = expect(gate(tmp_path, pr(), [review()], ids=ids), DENY, "DENY")
    assert out.startswith("DENY malformed_input identities"), out


def test_main_merge_does_not_move_the_window(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True)]
    rs = [review(login="other-reviewer", state="COMMENTED", commit=HEAD, body="P3: rename",
                 at="2026-11-08T10:00:00Z"),
          review(commit=MERGE, at="2026-11-08T11:30:00Z")]
    out = expect(gate(tmp_path, pr(headRefOid=MERGE), rs, commits=commits), HOLD, "HOLD")
    assert "other-reviewer" in out


def test_main_merge_head_with_clean_window_arms(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True)]
    rs = [review(commit=MERGE, at="2026-11-08T11:30:00Z")]
    expect(gate(tmp_path, pr(headRefOid=MERGE), rs, commits=commits), ARM, "ARM")


def test_commits_file_out_of_sync_with_head_waits(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()], commits=COMMITS[:1]), WAIT, "WAIT")
    assert "inputs_out_of_sync" in out


def test_only_merge_commits_denies(tmp_path):
    commits = [commit(HEAD, "2026-11-08T09:30:00Z", merge=True)]
    expect(gate(tmp_path, pr(), [review()], commits=commits), DENY, "DENY")


def test_bad_commit_date_denies(tmp_path):
    commits = [commit(HEAD, "yesterday")]
    expect(gate(tmp_path, pr(), [review()], commits=commits), DENY, "DENY")


def test_paginated_pages_back_to_back(tmp_path):
    raw = {"reviews": json.dumps([review()]) + "\n" + json.dumps(
        [review(login="other-reviewer", state="COMMENTED", commit=OLD, body="P2: x",
                at="2026-11-08T11:00:00Z")])}
    expect(gate(tmp_path, pr(), [], raw=raw), HOLD, "HOLD")


def test_paginated_slurped_pages(tmp_path):
    late = comment(body="P3: one more")
    raw = {"issue_comments": json.dumps([[comment(body="LGTM")], [late]])}
    expect(gate(tmp_path, pr(), [review()], raw=raw), HOLD, "HOLD")


@pytest.mark.parametrize("text", ["", "{}", '{"message": "Not Found"}', "[1, 2]", "[[1]]"])
def test_bad_paginated_file_denies(tmp_path, text):
    expect(gate(tmp_path, pr(), [review()], raw={"review_comments": text}), DENY, "DENY")


# --- holds persist until a newer non-merge commit ---

def test_hold_is_persisted_and_honoured_after_the_comment_is_gone(tmp_path):
    cs = [comment(body="P2: the relay leaks paths")]
    expect(gate(tmp_path, pr(), [review()], issue_comments=cs), HOLD, "HOLD")
    h = holds(tmp_path)["101"]
    assert h["sha"] == HEAD and h["date"] == "2026-11-08T09:30:00Z" and h["recorded_at"]
    out = expect(gate(tmp_path, pr(), [review()]), HOLD, "HOLD")  # e.g. a restart, comment deleted
    assert "persisted hold" in out


def test_hold_survives_a_main_merge(tmp_path):
    expect(gate(tmp_path, pr(), [review()], issue_comments=[comment(body="P3: x")]), HOLD, "HOLD")
    commits = COMMITS + [commit(MERGE, "2026-11-08T12:00:00Z", merge=True)]
    rs = [review(commit=MERGE, at="2026-11-08T12:30:00Z")]
    out = expect(gate(tmp_path, pr(headRefOid=MERGE), rs, commits=commits), HOLD, "HOLD")
    assert "persisted hold" in out and holds(tmp_path)["101"]["sha"] == HEAD


def test_newer_non_merge_commit_clears_the_hold(tmp_path):
    expect(gate(tmp_path, pr(), [review()], issue_comments=[comment(body="P3: x")]), HOLD, "HOLD")
    commits = COMMITS + [commit(NEWFIX, "2026-11-08T12:00:00Z")]
    rs = [review(commit=NEWFIX, at="2026-11-08T12:30:00Z")]
    cs = [comment(body="P3: x")]  # the old finding predates the fix commit
    expect(gate(tmp_path, pr(headRefOid=NEWFIX), rs, commits=commits, issue_comments=cs), ARM, "ARM")
    assert "101" not in holds(tmp_path)


def test_different_but_older_non_merge_commit_keeps_the_hold(tmp_path):
    expect(gate(tmp_path, pr(), [review()], issue_comments=[comment(body="P3: x")]), HOLD, "HOLD")
    commits = [commit(FIX0, "2026-11-08T09:00:00Z")]  # rewritten history, older date
    out = expect(gate(tmp_path, pr(headRefOid=FIX0), [review(commit=FIX0)], commits=commits),
                 HOLD, "HOLD")
    assert "persisted hold" in out


def test_hold_for_another_pr_does_not_block(tmp_path):
    (tmp_path / "state").mkdir()
    write_json(tmp_path / "state" / "holds.json",
               {"102": {"sha": HEAD, "date": "2026-11-08T09:30:00Z", "head": HEAD, "detail": "x",
                         "recorded_at": "2026-11-08T10:00:00+00:00",
                         "items": [{"id": "review:9", "sha256": "0" * 64, "real": True, "body": "P3: x"}]}})
    expect(gate(tmp_path, pr(), [review()]), ARM, "ARM")


@pytest.mark.parametrize("text,why", [("{broken", "state_corrupt:holds.json:1:document:not_json"),
                                      ("[]", "state_corrupt:holds.json:1:document:not_object"),
                                      ('{"101": "yes"}', "state_corrupt:holds.json:101:row:not_object"),
                                      ('{"101": {"sha": "x"}}', "state_corrupt:holds.json:101:sha:format")])
def test_corrupt_holds_file_denies(tmp_path, text, why):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "holds.json").write_text(text)
    out = expect(gate(tmp_path, pr(), [review()]), DENY, "DENY")
    assert out.startswith(f"DENY {why}"), out


def test_hold_blocks_update_branch_too(tmp_path):
    expect(gate(tmp_path, pr(), [review()], issue_comments=[comment(body="P3: x")]), HOLD, "HOLD")
    expect(gate(tmp_path, pr(mergeStateStatus="BEHIND"), [review()], action="update_branch"),
           HOLD, "HOLD")


# --- findings 2 + 11: arm and update_branch are separate actions ---

def test_behind_gives_arm_for_arm(tmp_path):
    """approved PRs here are usually BEHIND; requiring CLEAN deadlocks."""
    out = expect(gate(tmp_path, pr(mergeStateStatus="BEHIND"), [review()]), ARM, "ARM")
    assert out == f"ARM {HEAD} lines=50"


def test_behind_update_branch_is_eligible(tmp_path):
    out = expect(gate(tmp_path, pr(mergeStateStatus="BEHIND"), [review()], action="update_branch"),
                 UPDATE, "UPDATE_BRANCH")
    assert out == f"UPDATE_BRANCH {HEAD} lines=50"


def test_behind_with_auto_merge_already_on_can_update(tmp_path):
    p = pr(mergeStateStatus="BEHIND", autoMergeRequest={"mergeMethod": "SQUASH"})
    expect(gate(tmp_path, p, [review()], action="update_branch"), UPDATE, "UPDATE_BRANCH")


def test_behind_with_auto_merge_already_on_never_re_arms(tmp_path):
    p = pr(mergeStateStatus="BEHIND", autoMergeRequest={"mergeMethod": "SQUASH"})
    out = expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")
    assert "auto_merge_already_on" in out


def test_update_branch_when_not_behind_waits(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()], action="update_branch"), WAIT, "WAIT")
    assert "merge_state=CLEAN" in out


@pytest.mark.parametrize("bad", ["stale", "pending", "cr", "missing_required"])
def test_update_branch_keeps_every_other_condition(tmp_path, bad):
    p, rs = pr(mergeStateStatus="BEHIND"), [review()]
    if bad == "stale":
        rs = [review(commit=OLD)]
    elif bad == "pending":
        p["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
    elif bad == "cr":
        p["reviewDecision"] = "CHANGES_REQUESTED"
    else:
        p["statusCheckRollup"] = p["statusCheckRollup"][1:]
    expect(gate(tmp_path, p, rs, action="update_branch"), WAIT, "WAIT")


def test_update_branch_findings_hold(tmp_path):
    p = pr(mergeStateStatus="BEHIND")
    expect(gate(tmp_path, p, [review(body="nit: x")], action="update_branch"), HOLD, "HOLD")


def test_update_branch_over_cap_escalates(tmp_path):
    p = pr(mergeStateStatus="BEHIND", additions=5000)
    expect(gate(tmp_path, p, [review()], action="update_branch"), ESCALATE, "ESCALATE_SIZE")


def test_unknown_action_is_refused(tmp_path):
    rc, _ = gate(tmp_path, pr(), [review()], action="merge")
    assert rc == 2


# --- required contexts and merge state ---

@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN", "UNSTABLE", "DIRTY", None])
def test_unresolved_merge_state_waits_for_arm(tmp_path, state):
    out = expect(gate(tmp_path, pr(mergeStateStatus=state), [review()]), WAIT, "WAIT")
    assert f"merge_state={state}" in out


@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN", "UNSTABLE", "DIRTY", "CLEAN", "HAS_HOOKS"])
def test_update_branch_needs_behind(tmp_path, state):
    expect(gate(tmp_path, pr(mergeStateStatus=state), [review()], action="update_branch"), WAIT, "WAIT")


def test_has_hooks_arms(tmp_path):
    expect(gate(tmp_path, pr(mergeStateStatus="HAS_HOOKS"), [review()]), ARM, "ARM")


def test_partly_filled_rollup_missing_a_required_context_waits(tmp_path):
    """The review's probe: one successful check while a required context never reported."""
    p = pr(mergeStateStatus="BLOCKED", statusCheckRollup=[
        {"__typename": "CheckRun", "name": "Unit", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    out = expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")
    assert "missing_required:cla" in out and "merge_state=BLOCKED" in out


def test_missing_required_context_waits_even_when_clean(tmp_path):
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": "Unit"}, {"context": "cla"}, {"context": "CodeQL"}]}}]
    out = expect(gate(tmp_path, pr(), [review()], required=[rules_env(rules), classic_env()]), WAIT, "WAIT")
    assert out.endswith("missing_required:CodeQL")


def test_required_context_pending_waits(tmp_path):
    p = pr()
    p["statusCheckRollup"][3]["state"] = "EXPECTED"
    assert "pending:cla" in expect(gate(tmp_path, p, [review()]), WAIT, "WAIT")


def test_classic_protection_contexts_are_added(tmp_path):
    classic = classic_200(contexts=["Unit"], checks=[{"context": "Security Scan", "app_id": 1}])
    out = expect(gate(tmp_path, pr(), [review()], required=[rules_env(), classic]), WAIT, "WAIT")
    assert "missing_required:Security Scan" in out


def test_classic_contexts_alone_are_read(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()], required=[rules_env([]), classic_200(contexts=["Unit", "E2E"])]),
                 WAIT, "WAIT")
    assert out.endswith("missing_required:E2E")


def test_no_required_contexts_waits(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()], required=[rules_env([{"type": "deletion"}]), classic_env()]),
                 WAIT, "WAIT")
    assert out.endswith("required_contexts_empty")


def test_all_required_present_and_green_arms(tmp_path):
    out = expect(gate(tmp_path, pr(), [review()], required=[rules_env(), classic_200(contexts=["Unit", "cla"])]),
                 ARM, "ARM")
    assert out == f"ARM {HEAD} lines=50"


def test_envelopes_in_either_order_arm(tmp_path):
    expect(gate(tmp_path, pr(), [review()], required=[classic_env(), rules_env()]), ARM, "ARM")


# --- the inventory is proven complete, or WAIT required_contexts_unverified ---

@pytest.mark.parametrize("required,why", [
    # the review's probes: a rules 404 plus classic {"contexts": ["Unit"]} with Unit green
    ([rules_env({"message": "Not Found"}, status=404), classic_200(contexts=["Unit"])], "rules_status=404"),
    ([rules_env([{"type": "required_status_checks"}]), classic_200(contexts=["Unit"])], "rules_parameters"),
    ([rules_env([{"type": "required_status_checks", "parameters": {}}]), classic_env()], "rules_parameters"),
    ([rules_env([{"type": "required_status_checks", "parameters": {"required_status_checks": [{}]}}]),
      classic_env()], "rules_checks_entry"),
    ([rules_env([{"type": "required_status_checks", "parameters": {"required_status_checks": "Unit"}}]),
      classic_env()], "rules_checks_not_a_list"),
    ([rules_env(status=500, body={"message": "Server Error"}), classic_env()], "rules_status=500"),
    ([rules_env(status=None, body=None), classic_env()], "rules_status=None"),
    ([rules_env(status=True), classic_env()], "rules_status=True"),
    ([rules_env(status="200"), classic_env()], "rules_status='200'"),
    ([rules_env({"rules": []}), classic_env()], "rules_body"),
    ([rules_env([{"parameters": {}}]), classic_env()], "rules_entry"),
    ([rules_env()], "classic_missing"),
    ([classic_env()], "rules_missing"),
    ([rules_env(), rules_env(), classic_env()], "duplicate:rules"),
    ([rules_env(), classic_env({"message": "Not Found"})], "classic_404_not_branch_not_protected"),
    ([rules_env(), classic_env({"message": "Bad credentials"}, status=401)], "classic_status=401"),
    ([rules_env(), classic_env({"message": "Branch not protected"}, status=403)], "classic_status=403"),
    ([rules_env(), classic_env({"url": "x", "required_status_checks": None}, status=200)], "classic_body"),
    ([rules_env(), classic_env({"url": "x"}, status=200)], "classic_body"),
    ([rules_env(), classic_env({"contexts": ["Unit"]}, status=200)], "classic_body"),
    ([rules_env(), classic_200(contexts="Unit")], "classic_contexts_not_a_list"),
    ([rules_env(), classic_200(checks=[{"app_id": 1}])], "classic_checks_entry"),
    ([rules_env(endpoint="repos/someone/fork/rules/branches/main"), classic_env()], "unexpected_endpoint"),
    ([rules_env(), classic_env(endpoint=CLASSIC_EP + "/required_status_checks")], "unexpected_endpoint"),
    ([RULES, classic_env()], "not_an_envelope"),
    ([dict(rules_env(), extra=1), classic_env()], "not_an_envelope"),
])
def test_unverified_inventory_waits_never_arms(tmp_path, required, why):
    out = expect(gate(tmp_path, pr(), [review()], required=required), WAIT, "WAIT")
    assert f"required_contexts_unverified:{why}" in out, out


@pytest.mark.parametrize("action,state", [("arm", "CLEAN"), ("update_branch", "BEHIND")])
def test_rules_404_never_arms_or_updates(tmp_path, action, state):
    req = [rules_env({"message": "Not Found"}, status=404), classic_200(contexts=["Unit"])]
    out = expect(gate(tmp_path, pr(mergeStateStatus=state), [review()], required=req, action=action),
                 WAIT, "WAIT")
    assert out == "WAIT required_contexts_unverified:rules_status=404"


def test_unparseable_required_file_denies(tmp_path):
    expect(gate(tmp_path, pr(), [review()], raw={"required0": "{not json"}), DENY, "DENY")


def test_envelope_from_gh_api_include_output(tmp_path):
    """api_envelope.py turns real `gh api -i` shapes into gate inputs: 200 rules + 404 classic arms."""
    env = base_env(tmp_path)
    rules_raw = tmp_path / "rules.raw"
    rules_raw.write_text("HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                         "X-Github-Request-Id: 1\r\n\r\n" + json.dumps(RULES))
    classic_raw = tmp_path / "classic.raw"
    classic_raw.write_text("HTTP/2.0 404 Not Found\nContent-Type: application/json\n\n"
                           + json.dumps(NOT_PROTECTED))
    outs = []
    for ep, raw, name in ((RULES_EP, rules_raw, "r.json"), (CLASSIC_EP, classic_raw, "c.json")):
        r = run("api_envelope.py", "--endpoint", ep, "--raw", raw, "--out", tmp_path / name, env=env)
        assert r.returncode == 0 and r.stdout.strip() in ("ENVELOPE 200", "ENVELOPE 404"), r.stdout
        outs.append(json.loads((tmp_path / name).read_text()))
    assert outs == [rules_env(), classic_env()]
    expect(gate(tmp_path, pr(), [review()], required=outs), ARM, "ARM")


@pytest.mark.parametrize("raw", ["", "not http\n\n[]", "HTTP/2.0 200 OK\n\n{broken",
                                 "HTTP/2.0 200 OK\nLink: <https://api.github.com/x?page=2>; rel=\"next\"\n\n[]",
                                 "HTTP/2.0 200 OK\n[]"])
def test_envelope_parser_fails_closed(tmp_path, raw):
    f = tmp_path / "x.raw"
    f.write_text(raw)
    r = run("api_envelope.py", "--endpoint", RULES_EP, "--raw", f, "--out", tmp_path / "e.json",
            env=base_env(tmp_path))
    assert r.returncode == 2 and r.stdout.strip() == "ENVELOPE null", r.stdout
    e = json.loads((tmp_path / "e.json").read_text())
    assert e == {"endpoint": RULES_EP, "http_status": None, "body": None}
    out = expect(gate(tmp_path, pr(), [review()], required=[e, classic_env()]), WAIT, "WAIT")
    assert "required_contexts_unverified:rules_status=None" in out


def test_review_on_head_counts_even_with_clock_skew(tmp_path):
    rs = [review(), review(login="other-reviewer", state="COMMENTED", body="P3: rename",
                           at="2026-11-08T09:00:00Z")]  # submitted "before" the head's committer date
    expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")


def test_scope_star_still_needs_an_ours_author(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "conditions": {},
                       "revoked": False, "granted_by": "user", "holder": "shepherd",
                       "scope": dict(SCOPE, pr_author="*")}]}
    expect(gate(tmp_path, pr(), [review()], mandates=m), ARM, "ARM")
    expect(gate(tmp_path, pr(author={"login": "other-author"}), [review()], mandates=m), DENY, "DENY")


# --- update-branch keeps the approval, a fix push dismisses it ---

MERGE2 = "f" * 40


def test_approval_on_last_non_merge_then_main_merge_head_arms(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True)]
    out = expect(gate(tmp_path, pr(headRefOid=MERGE), [review(commit=HEAD)], commits=commits), ARM, "ARM")
    assert out.startswith(f"ARM {MERGE} ")


def test_approval_survives_two_main_merges(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True),
                         commit(MERGE2, "2026-11-08T12:00:00Z", merge=True,
                                message="Merge remote-tracking branch 'origin/main' into fix/x")]
    rs = [review(commit=MERGE, at="2026-11-08T11:10:00Z")]
    expect(gate(tmp_path, pr(headRefOid=MERGE2), rs, commits=commits), ARM, "ARM")


def test_approval_then_non_merge_commit_is_stale(tmp_path):
    commits = COMMITS + [commit(NEWFIX, "2026-11-08T11:00:00Z")]
    out = expect(gate(tmp_path, pr(headRefOid=NEWFIX), [review(commit=HEAD)], commits=commits), WAIT, "WAIT")
    assert f"stale_approval:maintainer-login@{HEAD[:8]}" in out


def test_approval_before_the_last_non_merge_commit_is_stale_even_after_a_merge(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True)]
    out = expect(gate(tmp_path, pr(headRefOid=MERGE), [review(commit=OLD)], commits=commits), WAIT, "WAIT")
    assert "stale_approval" in out


@pytest.mark.parametrize("message", ["Merge branch 'feature/other' into fix/x", "",
                                     "Merge pull request #12 from someone/branch"])
def test_a_merge_that_is_not_from_main_does_not_carry_the_approval(tmp_path, message):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True, message=message)]
    out = expect(gate(tmp_path, pr(headRefOid=MERGE), [review(commit=HEAD)], commits=commits), WAIT, "WAIT")
    assert "stale_approval" in out


def test_behind_is_eligible_for_update_branch_too(tmp_path):
    expect(gate(tmp_path, pr(mergeStateStatus="BEHIND"), [review()], action="update_branch"),
           UPDATE, "UPDATE_BRANCH")


def test_update_branch_after_main_merge_keeps_the_approval(tmp_path):
    commits = COMMITS + [commit(MERGE, "2026-11-08T11:00:00Z", merge=True)]
    p = pr(headRefOid=MERGE, mergeStateStatus="BEHIND", autoMergeRequest={"mergeMethod": "SQUASH"})
    expect(gate(tmp_path, p, [review(commit=HEAD)], commits=commits, action="update_branch"),
           UPDATE, "UPDATE_BRANCH")


# --- FYI from the supervisor: required contexts are the 4 CodeQL checks; classic returns 404 ---

CODEQL = ["Analyze (python)", "Analyze (actions)", "Analyze (javascript-typescript)", "CodeQL"]
CODEQL_RULES = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
    {"context": c, "integration_id": 57789} for c in CODEQL]}}]
def codeql_pr(**over):
    return pr(statusCheckRollup=[{"__typename": "CheckRun", "name": c, "status": "COMPLETED",
                                  "conclusion": "SUCCESS"} for c in CODEQL], **over)


def test_codeql_rules_plus_classic_404_arms(tmp_path):
    expect(gate(tmp_path, codeql_pr(), [review()], required=[rules_env(CODEQL_RULES), classic_env()]), ARM, "ARM")


def test_classic_404_alone_waits_and_never_crashes(tmp_path):
    out = expect(gate(tmp_path, codeql_pr(), [review()], required=[classic_env()]), WAIT, "WAIT")
    assert "required_contexts_unverified:rules_missing" in out


def test_codeql_check_missing_waits(tmp_path):
    p = codeql_pr()
    p["statusCheckRollup"] = p["statusCheckRollup"][:3]
    out = expect(gate(tmp_path, p, [review()], required=[rules_env(CODEQL_RULES), classic_env()]), WAIT, "WAIT")
    assert "missing_required:CodeQL" in out


# --- clear-by-reading, bound to the exact body ---

MAINTAINER_CLEAN = ("APPROVE (0 P1, 0 P2, 0 P3)\n\nI re-ran the relay tests and read the diff; the isolation "
           "fix holds and the claims can't collide now.\n\nFindings: None.")
REAL_P3 = "APPROVE (0 P1, 0 P2, 1 P3)\n\nFindings:\n- P3: rename `_claim` to `_claim_once` for clarity."


def policy_version():
    from conftest import KIT
    return json.loads((KIT / "mandates.json").read_text())["policy_version"]


def clear_item(tmp_path, body, item="review:1001", go=True, pr_no="101"):
    """The real flow: holds.py body -> (conductor GO, copied by go_ledger) -> holds.py clear."""
    env = base_env(tmp_path)
    src = write_json(tmp_path / "src-reviews.json", [{"id": int(item.split(":")[1]), "body": body}])
    bf = tmp_path / "clear-body.md"
    r = run("holds.py", "body", "--from", src, "--item", item, "--out", bf, env=env)
    assert r.returncode == 0, r.stdout
    if go:
        r = ledger("add", "--id", f"{pr_no}-clear_hold-x", "--action", "clear_hold",
                "--pr", pr_no, "--head", HEAD, "--policy-version", policy_version(), "--granted-by",
                "conductor GO", "--body-file", bf, "--item", item, env=env)
        assert r.returncode == 0, r.stdout
    return run("holds.py", "clear", "--repo", "your-org/your-repo", "--pr-author", "your-login",
               "--base", "main", "--pr", pr_no, "--item", item, "--body-file", bf, "--reason",
               "structured clean approval: 0/0/0, Findings: None", "--go-id", f"{pr_no}-clear_hold-x",
               env=env)


def test_maintainer_clean_approval_holds_without_a_clear(tmp_path):
    out = expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")
    assert "item=review:1001" in out


def test_maintainer_clean_approval_arms_with_a_matching_clear(tmp_path):
    r = clear_item(tmp_path, MAINTAINER_CLEAN)
    assert r.returncode == 0 and r.stdout.startswith("CLEARED"), r.stdout
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), ARM, "ARM")


def test_clear_after_a_persisted_hold_releases_it(tmp_path):
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")
    assert holds(tmp_path)["101"]["items"] == [
        {"id": "review:1001", "sha256": holds(tmp_path)["101"]["items"][0]["sha256"], "real": False,
         "body": MAINTAINER_CLEAN}]
    assert clear_item(tmp_path, MAINTAINER_CLEAN).returncode == 0
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), ARM, "ARM")
    assert "101" not in holds(tmp_path)


def test_persisted_hold_with_an_uncleared_item_stays(tmp_path):
    rs = [review(body=MAINTAINER_CLEAN), review(login="other-reviewer", state="COMMENTED", rid=1002,
                                       body="Have you looked at the timeout?", at="2026-11-08T11:00:00Z")]
    expect(gate(tmp_path, pr(), rs), HOLD, "HOLD")
    assert clear_item(tmp_path, MAINTAINER_CLEAN).returncode == 0
    out = expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")
    assert "persisted hold" in out


def test_edited_body_holds_again(tmp_path):
    assert clear_item(tmp_path, MAINTAINER_CLEAN).returncode == 0
    edited = MAINTAINER_CLEAN.replace("Findings: None.", "Findings: None. Though you should rename one helper.")
    expect(gate(tmp_path, pr(), [review(body=edited)]), HOLD, "HOLD")


def test_clear_is_bound_to_the_item_id(tmp_path):
    assert clear_item(tmp_path, MAINTAINER_CLEAN).returncode == 0
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN, rid=1003)]), HOLD, "HOLD")


def test_clear_is_bound_to_the_pr(tmp_path):
    assert clear_item(tmp_path, MAINTAINER_CLEAN, pr_no="102").returncode == 0
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")


def test_clear_without_a_go_is_refused(tmp_path):
    r = clear_item(tmp_path, MAINTAINER_CLEAN, go=False)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED go_missing"), r.stdout
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")


def test_real_p3_finding_cannot_be_cleared(tmp_path):
    r = clear_item(tmp_path, REAL_P3)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED real_findings"), r.stdout
    expect(gate(tmp_path, pr(), [review(body=REAL_P3)]), HOLD, "HOLD")


def cleared_row(body, **over):
    import hashlib
    row = {"pr": "101", "item_id": "review:1001", "go_id": "forged", "reason": "x",
           "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "cleared_at": "2026-11-09T10:00:00+00:00"}
    row.update(over)
    return row


def test_gate_ignores_a_hand_written_clear_on_a_real_finding(tmp_path):
    (tmp_path / "state").mkdir()
    write_json(tmp_path / "state" / "holds.json", {"_cleared": [cleared_row(REAL_P3)]})
    expect(gate(tmp_path, pr(), [review(body=REAL_P3)]), HOLD, "HOLD")


def test_gate_refuses_a_clear_without_go_id(tmp_path):
    """Pre-brief-14 such a row was skipped (HOLD); now the whole holds.json is corrupt (DENY)."""
    (tmp_path / "state").mkdir()
    row = cleared_row(MAINTAINER_CLEAN)
    del row["go_id"]
    write_json(tmp_path / "state" / "holds.json", {"_cleared": [row]})
    out = expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), DENY, "DENY")
    assert out == "DENY state_corrupt:holds.json:_cleared[0]:go_id:missing"


def test_malformed_cleared_list_denies(tmp_path):
    (tmp_path / "state").mkdir()
    write_json(tmp_path / "state" / "holds.json", {"_cleared": {"x": 1}})
    out = expect(gate(tmp_path, pr(), [review()]), DENY, "DENY")
    assert "_cleared" in out


@pytest.mark.parametrize("body,real", [
    (MAINTAINER_CLEAN, False), ("APPROVE (P1=0, P2=0, P3=0). Findings: None.", False),
    ("No P1/P2 found. LGTM overall.", False), (REAL_P3, True), ("P2: the relay leaks paths", True),
    ("(0 P1, 0 P2, 2 P3)", True), ("nit: trailing space", True), ("**P2-1** leaks", True),
])
def test_real_findings_classifier_through_holds(tmp_path, body, real):
    """Exact outcomes both ways (09 vacuity note): a clean body is CLEARED, a real one REFUSED."""
    r = clear_item(tmp_path, body)
    if real:
        assert r.returncode == 1 and r.stdout.startswith("REFUSED real_findings"), r.stdout
    else:
        assert r.returncode == 0 and r.stdout.startswith("CLEARED #101 review:1001 body="), r.stdout
        expect(gate(tmp_path, pr(), [review(body=body)]), ARM, "ARM")


def test_forged_clear_never_releases_a_persisted_real_finding(tmp_path):
    expect(gate(tmp_path, pr(), [review(body=REAL_P3)]), HOLD, "HOLD")
    h = holds(tmp_path)
    assert h["101"]["items"][0]["real"] is True
    h["_cleared"] = [cleared_row(REAL_P3)]
    write_json(tmp_path / "state" / "holds.json", h)
    out = expect(gate(tmp_path, pr(), [review()]), HOLD, "HOLD")  # the review was edited away
    assert "persisted hold" in out
    out = expect(gate(tmp_path, pr(), [review(rid=1009)]), HOLD, "HOLD")  # or deleted: saved body is real
    assert "persisted hold" in out


# --- current findings versus historical references (review bodies in the shape real reviewers write) ---

from conftest import TESTS  # noqa: E402

PROBE_09 = "APPROVE (0 P1, 0 P2, 0 P3). The previous P3 is fixed. Findings: None."
CLEAN_APPROVAL = (TESTS / "fixtures" / "approval-clean.md").read_text(encoding="utf-8")
P3_REVIEW = (TESTS / "fixtures" / "review-p3.md").read_text(encoding="utf-8")
NONE_BUT_P2 = "APPROVE. Findings: None.\n\nP2: the relay test leaks a temp path."


def test_fixtures_are_pinned():
    import hashlib
    # sha256 of tests/fixtures/*: two review bodies in the shape real reviewers write
    assert hashlib.sha256(CLEAN_APPROVAL.encode()).hexdigest() == \
        "ec217835397d9c4f109f5c10c0c220fea00b5137d1f86e8557c67c3e7a27cbc7"
    assert hashlib.sha256(P3_REVIEW.encode()).hexdigest() == \
        "7319e09cfb6b04f975658a7b679a73a31ae1a88ac25688482447396aeee6eab2"


@pytest.mark.parametrize("body,real", [
    (PROBE_09, False), (CLEAN_APPROVAL, False), (P3_REVIEW, True), (NONE_BUT_P2, True),
    ("APPROVE (0 P1, 1 P2, 0 P3)", True), ("Counts: P2: 1, P3: 0", True),
    ("1. **P3: wording**", True), ("- P2 \u2014 leaks", True), ("P1:", True), ("  * _nit_: x", True),
    ("### Findings\n\n1. rename the helper", True), ("**Issues**\n- the cache", True),
    ("Findings:\n- something", True), ("P1: 0\nP2: 0\nP3: 0", False), ("P3 is fixed now.", False),
    ("0 P1, 0 P2, 0 P3", False), ("### Findings\n\nNone.\n\n### Notes\n1. not blocking", False),
    ("### Findings\n```\n# a code comment\n- y\n```", True), ("```\n- y\n```\n### Findings\n\nNone.", False),
])
def test_d1_classifier_exact(tmp_path, body, real):
    r = clear_item(tmp_path, body)
    if real:
        assert (r.returncode, r.stdout.split()[:2]) == (1, ["REFUSED", "real_findings:"]), r.stdout
    else:
        assert r.returncode == 0 and r.stdout.startswith("CLEARED #101 review:1001"), r.stdout


@pytest.mark.parametrize("body", [PROBE_09, CLEAN_APPROVAL])
def test_d1_clean_reapproval_holds_then_a_conductor_go_clears_it(tmp_path, body):
    out = expect(gate(tmp_path, pr(), [review(body=body)]), HOLD, "HOLD")
    assert "item=review:1001" in out
    r = clear_item(tmp_path, body)
    assert r.returncode == 0 and r.stdout.startswith("CLEARED #101 review:1001"), r.stdout
    assert expect(gate(tmp_path, pr(), [review(body=body)]), ARM, "ARM") == f"ARM {HEAD} lines=50"
    assert "101" not in holds(tmp_path)


@pytest.mark.parametrize("body", [P3_REVIEW, NONE_BUT_P2])
def test_d1_real_findings_have_no_clear_path(tmp_path, body):
    expect(gate(tmp_path, pr(), [review(body=body)]), HOLD, "HOLD")
    r = clear_item(tmp_path, body)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED real_findings"), r.stdout
    expect(gate(tmp_path, pr(), [review(body=body)]), HOLD, "HOLD")


def test_d1_full_p3_round_with_a_historical_reference(tmp_path):
    """Real P3 review -> held -> fix push (non-merge) -> clean re-approval that mentions the old
    P3 -> held, clearable -> conductor GO -> ARM. The old review stays outside the new window."""
    first = review(state="APPROVED", commit=HEAD, body=P3_REVIEW, rid=2001, at="2026-11-08T10:00:00Z")
    expect(gate(tmp_path, pr(), [first]), HOLD, "HOLD")
    commits = COMMITS + [commit(NEWFIX, "2026-11-08T12:00:00Z")]
    again = review(state="APPROVED", commit=NEWFIX, body=PROBE_09, rid=1001, at="2026-11-08T13:00:00Z")
    p = pr(headRefOid=NEWFIX)
    out = expect(gate(tmp_path, p, [first, again], commits=commits), HOLD, "HOLD")
    assert "item=review:1001" in out and "review:2001" not in out
    assert clear_item(tmp_path, PROBE_09).stdout.startswith("CLEARED")
    assert expect(gate(tmp_path, p, [first, again], commits=commits), ARM, "ARM") == f"ARM {NEWFIX} lines=50"


def test_d1_persisted_real_flag_is_not_trusted_after_an_edit(tmp_path):
    """A hold saved with real:true (old classifier or an old body) is re-judged from the item's
    current body: once the reviewer's current text is clean and cleared, it releases."""
    expect(gate(tmp_path, pr(), [review(body=REAL_P3)]), HOLD, "HOLD")
    h = holds(tmp_path)
    assert h["101"]["items"][0]["real"] is True
    assert clear_item(tmp_path, PROBE_09).stdout.startswith("CLEARED")  # the edited body
    expect(gate(tmp_path, pr(), [review(body=PROBE_09)]), ARM, "ARM")


def test_d1_persisted_real_true_on_a_clean_body_is_recomputed(tmp_path):
    import hashlib
    (tmp_path / "state").mkdir()
    sha = hashlib.sha256(PROBE_09.encode()).hexdigest()
    write_json(tmp_path / "state" / "holds.json", {"101": {
        "sha": HEAD, "date": "2026-11-08T09:30:00Z", "head": HEAD, "detail": "old",
        "recorded_at": "2026-11-08T10:00:00+00:00",
        "items": [{"id": "review:1001", "sha256": sha, "real": True, "body": PROBE_09}]}})
    assert clear_item(tmp_path, PROBE_09).stdout.startswith("CLEARED")
    expect(gate(tmp_path, pr(), [review(body=PROBE_09)]), ARM, "ARM")


def test_d1_gone_item_is_judged_from_the_saved_body(tmp_path):
    expect(gate(tmp_path, pr(), [review(body=PROBE_09)]), HOLD, "HOLD")
    assert holds(tmp_path)["101"]["items"][0]["body"] == PROBE_09
    assert clear_item(tmp_path, PROBE_09).stdout.startswith("CLEARED")
    expect(gate(tmp_path, pr(), [review(rid=1009)]), ARM, "ARM")  # review:1001 was deleted


def test_d1_gone_item_without_a_saved_body_is_a_corrupt_hold(tmp_path):
    """a held item without its saved body is a damaged holds.json: the clear is
    REFUSED and the gate DENYs (it never releases)."""
    expect(gate(tmp_path, pr(), [review(body=PROBE_09)]), HOLD, "HOLD")
    h = holds(tmp_path)
    del h["101"]["items"][0]["body"]
    write_json(tmp_path / "state" / "holds.json", h)
    r = clear_item(tmp_path, PROBE_09)
    assert r.stdout == "REFUSED state_corrupt:holds.json:101:items:[0].body:missing\n", r.stdout
    out = expect(gate(tmp_path, pr(), [review(rid=1009)]), DENY, "DENY")
    assert out == "DENY state_corrupt:holds.json:101:items:[0].body:missing"


# --- strict scalars ---

@pytest.mark.parametrize("field,value", [
    ("number", "901"), ("number", "101"), ("number", True), ("number", 101.0), ("number", 0),
    ("number", -1), ("number", None), ("additions", -2000), ("deletions", -1), ("additions", "40"),
    ("additions", 40.0), ("deletions", True), ("additions", None),
])
@pytest.mark.parametrize("action,state", [("arm", "CLEAN"), ("update_branch", "BEHIND")])
def test_d6_malformed_scalars_never_arm(tmp_path, field, value, action, state):
    p = pr(mergeStateStatus=state, **{field: value})
    out = expect(gate(tmp_path, p, [review()], action=action), DENY, "DENY")
    assert out.startswith(f"DENY malformed_input {field}="), out


def test_d6_review_probe_negative_additions(tmp_path):
    p = pr(additions=-2000, deletions=3000)
    assert expect(gate(tmp_path, p, [review()]), DENY, "DENY").startswith("DENY malformed_input additions")


def test_d6_string_number_cannot_dodge_the_never_floor(tmp_path):
    m = never_mandates([901])
    assert expect(gate(tmp_path, pr(number="901"), [review()], mandates=m), DENY, "DENY").startswith(
        "DENY malformed_input number")
    assert expect(gate(tmp_path, pr(number=901), [review()], mandates=m), DENY, "DENY") == "DENY never_automerge_pr #901"


def test_d6_zero_lines_is_fine(tmp_path):
    assert expect(gate(tmp_path, pr(additions=0, deletions=0), [review()]), ARM, "ARM") == f"ARM {HEAD} lines=0"


def test_d1_gone_item_with_a_tampered_saved_body_stays_held(tmp_path):
    expect(gate(tmp_path, pr(), [review(body=REAL_P3)]), HOLD, "HOLD")
    h = holds(tmp_path)
    h["101"]["items"][0]["body"] = PROBE_09  # sha256 still names the real P3 body
    write_json(tmp_path / "state" / "holds.json", h)
    assert clear_item(tmp_path, PROBE_09).stdout.startswith("CLEARED")
    import hashlib
    h = holds(tmp_path)
    h["_cleared"].append(dict(h["_cleared"][0], body_sha256=hashlib.sha256(REAL_P3.encode()).hexdigest()))
    write_json(tmp_path / "state" / "holds.json", h)
    assert "persisted hold" in expect(gate(tmp_path, pr(), [review(rid=1009)]), HOLD, "HOLD")


# --- strictness sweep: missing / null / ill-typed GitHub fields never read as "clean" ---

def no_body(item):
    d = dict(item)
    del d["body"]
    return d


@pytest.mark.parametrize("make", [
    lambda: ([dict(review(), body=None)], ()),
    lambda: ([no_body(review())], ()),
    lambda: ([review()], [dict(comment(), body=None)]),
    lambda: ([review()], [no_body(comment())]),
    lambda: ([review(), dict(review(login="reviewer2", state="COMMENTED", rid=1002), body=None)], ()),
], ids=["review_body_null", "review_body_missing", "comment_body_null", "comment_body_missing",
        "commented_review_body_null"])
def test_sweep_null_or_missing_body_denies(tmp_path, make):
    """Pre-fix a null/missing body read as "" = clean: no hold, then ARM."""
    reviews, comments = make()
    out = expect(gate(tmp_path, pr(), reviews, issue_comments=comments), DENY, "DENY")
    assert out.startswith("DENY malformed_input ") and out.endswith(".body"), out
    assert holds(tmp_path) == {}


@pytest.mark.parametrize("user", [None, {}, {"login": None}, {"login": ""}, {"login": " "}, {"login": 5}],
                         ids=["null", "empty", "login_null", "login_empty", "login_blank", "login_int"])
def test_sweep_review_without_identity_denies(tmp_path, user):
    """Pre-fix an APPROVED review by nobody (login None) satisfied 'an approval exists' -> ARM."""
    out = expect(gate(tmp_path, pr(), [dict(review(), user=user)]), DENY, "DENY")
    assert out == "DENY malformed_input reviews[0].user", out


def test_sweep_review_without_user_key_denies(tmp_path):
    r = review()
    del r["user"]
    assert expect(gate(tmp_path, pr(), [r]), DENY, "DENY") == "DENY malformed_input reviews[0].user"


@pytest.mark.parametrize("field", ["issue_comments", "review_comments"])
def test_sweep_comment_without_identity_denies(tmp_path, field):
    """A clean-bodied comment by nobody used to be skipped as clean; it can't be judged."""
    kw = {field: [dict(comment(), user=None)]}
    out = expect(gate(tmp_path, pr(), [review()], **kw), DENY, "DENY")
    assert out == f"DENY malformed_input {field}[0].user", out


@pytest.mark.parametrize("at", [None, "", 5, "yesterday", "2026-11-08T10:00:00"],
                         ids=["null", "empty", "int", "word", "no_zone"])
def test_sweep_submitted_review_without_a_time_cannot_be_ordered(tmp_path, at):
    """Pre-fix a CHANGES_REQUESTED review without submitted_at sorted first, so an older
    approval became the reviewer's latest -> ARM."""
    rs = [review(state="CHANGES_REQUESTED", at=at, rid=1), review(at="2026-11-08T09:00:00Z", rid=2)]
    out = expect(gate(tmp_path, pr(), rs), DENY, "DENY")
    assert out.startswith("DENY malformed_input ") and "timestamp" in out, out


def test_sweep_reviews_are_ordered_by_real_time_not_text(tmp_path):
    """12:00+02:00 is 10:00Z, before 11:00Z: the later CHANGES_REQUESTED wins. A text sort put
    the approval last -> ARM."""
    rs = [review(state="CHANGES_REQUESTED", at="2026-11-08T11:00:00Z", rid=1),
          review(at="2026-11-08T12:00:00+02:00", rid=2)]
    out = expect(gate(tmp_path, pr(), rs), WAIT, "WAIT")
    assert "changes_requested:maintainer-login" in out, out
    rs = [review(state="CHANGES_REQUESTED", at="2026-11-08T12:00:00+02:00", rid=1),
          review(at="2026-11-08T11:00:00Z", rid=2)]                       # control: approval is later
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_sweep_pending_review_needs_no_time(tmp_path):
    """GitHub's unsubmitted PENDING review has no submitted_at; it is skipped, as before."""
    rs = [review(login="reviewer2", state="PENDING", at=None, rid=3), review()]
    expect(gate(tmp_path, pr(), rs), ARM, "ARM")


def test_sweep_missing_auto_merge_field_denies(tmp_path):
    """Pre-fix a pr.json without autoMergeRequest read as 'not armed' -> ARM on an armed PR."""
    p = pr()
    del p["autoMergeRequest"]
    out = expect(gate(tmp_path, p, [review()]), DENY, "DENY")
    assert out == "DENY malformed_input missing=autoMergeRequest", out
    expect(gate(tmp_path, pr(), [review()]), ARM, "ARM")                     # null: not armed
    expect(gate(tmp_path, pr(autoMergeRequest={"mergeMethod": "SQUASH"}), [review()]), WAIT, "WAIT")


@pytest.mark.parametrize("value", [False, "", 0, [], "SQUASH"])
def test_sweep_ill_typed_auto_merge_field_denies(tmp_path, value):
    out = expect(gate(tmp_path, pr(autoMergeRequest=value), [review()]), DENY, "DENY")
    assert out.startswith("DENY malformed_input autoMergeRequest="), out


@pytest.mark.parametrize("never", ["101", None, [101.0], ["101"], [True]],
                         ids=["string", "null", "float", "str_item", "bool_item"])
def test_sweep_ill_typed_never_prs_denies(tmp_path, never):
    """Pre-fix never_prs "101" was read as {1, 2, 5, 9}: PR 101 armed."""
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE,
                       "conditions": {"never_prs": never}}]}
    out = expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY")
    assert out.startswith("DENY malformed_input never_prs of M6"), out


def test_sweep_never_prs_list_still_denies_and_others_arm(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE,
                       "conditions": {"never_prs": [101]}}]}
    assert expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY") == "DENY never_automerge_pr #101"
    m["mandates"][0]["conditions"]["never_prs"] = [7]
    expect(gate(tmp_path, pr(), [review()], mandates=m), ARM, "ARM")


@pytest.mark.parametrize("cap", ["30", None, 30.5, True, -1])
def test_sweep_ill_typed_size_cap_denies(tmp_path, cap):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE,
                       "conditions": {"max_changed_lines": cap}}]}
    out = expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY")
    assert out.startswith("DENY malformed_input max_changed_lines of M6"), out


@pytest.mark.parametrize("cond", [None, [], "x"], ids=["null", "list", "string"])
def test_sweep_ill_typed_conditions_denies(tmp_path, cond):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE, "conditions": cond}]}
    out = expect(gate(tmp_path, pr(), [review()], mandates=m), DENY, "DENY")
    assert out == "DENY malformed_input conditions of M6 is not an object", out


def test_sweep_absent_conditions_keep_the_defaults(tmp_path):
    m = {"mandates": [{"id": "M6", "action": "arm_automerge", "revoked": False,
                       "granted_by": "user", "holder": "shepherd", "scope": SCOPE}]}
    expect(gate(tmp_path, pr(additions=1501, deletions=0), [review()], mandates=m), ESCALATE, "ESCALATE_SIZE")
    expect(gate(tmp_path, pr(), [review()], mandates=m), ARM, "ARM")
