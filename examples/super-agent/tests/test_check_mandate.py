import copy
import json

import pytest

from conftest import KIT, base_env, run, write_json, ledger

REPO = "your-org/your-repo"
URL = "https://github.com/your-org/your-repo.git"


def doc(**top):
    d = {
        "policy_version": "test-1",
        "shepherd_mode": "observe",
        "paused": False, "paused_by": None, "paused_at": None, "reason": None,
        "forbidden_without_user": ["force_push", "open_pr", "close"],
        "push_urls": {REPO: URL},
        "mandates": [
            {"id": "M1", "holder": "shepherd", "action": "ff_fix_push",
             "scope": {"repo": REPO, "pr_author": "your-login", "base": "main"},
             "conditions": {}, "granted_by": "user", "granted_at": "2026-11-03",
             "source": "memory:test", "revoked": False},
            {"id": "M7", "holder": "conductor", "action": "approve_review_plan",
             "scope": {"repo": REPO, "pr_author": "*", "base": "main"},
             "conditions": {}, "granted_by": "user", "granted_at": "2026-11-07",
             "source": "memory:test", "revoked": False},
        ],
    }
    d.update(top)
    return d


def check(tmp_path, data, holder="shepherd", action="ff_fix_push", author="your-login",
          base="main", repo=None):
    path = tmp_path / "mandates.json"
    if isinstance(data, str):
        path.write_text(data)
    else:
        write_json(path, data)
    args = ["--mandates", path, "--holder", holder, "--action", action,
            "--pr-author", author, "--base", base]
    if repo:
        args += ["--repo", repo]
    r = run("check_mandate.py", *args, env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def act(d):
    d = copy.deepcopy(d)
    d["shepherd_mode"] = "act"
    return d


def test_act_mode_allows(tmp_path):
    assert check(tmp_path, act(doc())) == (0, "ALLOW M1")


def test_observe_mode_gives_observe(tmp_path):
    assert check(tmp_path, doc()) == (3, "OBSERVE M1")


def test_conductor_mandate_allows_even_in_observe(tmp_path):
    rc, out = check(tmp_path, doc(), holder="conductor", action="approve_review_plan",
                    author="someone")
    assert (rc, out) == (0, "ALLOW M7")


def test_paused_wins(tmp_path):
    d = act(doc(paused=True, paused_by="user", paused_at="2026-11-08T22:00Z", reason="x"))
    assert check(tmp_path, d) == (4, "PAUSED")
    assert check(tmp_path, d, holder="conductor", action="approve_review_plan")[0] == 4


def test_revoked(tmp_path):
    d = act(doc())
    d["mandates"][0]["revoked"] = True
    rc, out = check(tmp_path, d)
    assert rc == 1 and out.startswith("DENY revoked")


def test_missing_revoked_field_fails_closed(tmp_path):
    d = act(doc())
    del d["mandates"][0]["revoked"]
    rc, out = check(tmp_path, d)
    assert rc == 1 and out.startswith("DENY revoked")


def test_wrong_author(tmp_path):
    rc, out = check(tmp_path, act(doc()), author="other-author")
    assert rc == 1 and out.startswith("DENY wrong_author")


def test_wrong_base(tmp_path):
    rc, out = check(tmp_path, act(doc()), base="feat/stacked")
    assert rc == 1 and out.startswith("DENY wrong_base")


def test_wrong_repo(tmp_path):
    rc, out = check(tmp_path, act(doc()), repo="someone/fork")
    assert rc == 1 and out.startswith("DENY wrong_repo")


def test_default_repo_is_cao(tmp_path):
    assert check(tmp_path, act(doc()), repo=REPO) == (0, "ALLOW M1")


def test_wrong_holder(tmp_path):
    rc, out = check(tmp_path, act(doc()), holder="conductor")
    assert rc == 1 and out.startswith("DENY wrong_holder")


def test_unknown_action(tmp_path):
    rc, out = check(tmp_path, act(doc()), action="delete_branch")
    assert rc == 1 and out.startswith("DENY unknown_action")


def test_forbidden_action(tmp_path):
    rc, out = check(tmp_path, act(doc()), action="force_push")
    assert rc == 1 and out.startswith("DENY forbidden_without_user")


def test_forbidden_beats_a_mandate_for_the_same_action(tmp_path):
    d = act(doc())
    d["mandates"][0]["action"] = "force_push"
    rc, out = check(tmp_path, d, action="force_push")
    assert rc == 1 and out.startswith("DENY forbidden_without_user")


def test_not_granted_by_user(tmp_path):
    d = act(doc())
    d["mandates"][0]["granted_by"] = "conductor"
    rc, out = check(tmp_path, d)
    assert rc == 1 and out.startswith("DENY not_user_granted")


def test_unknown_mode_fails_closed(tmp_path):
    rc, out = check(tmp_path, doc(shepherd_mode="auto"))
    assert rc == 1 and out.startswith("DENY bad_mode")


def test_malformed_file_denies(tmp_path):
    rc, out = check(tmp_path, "{not json")
    assert rc == 1 and out.startswith("DENY")


def test_second_mandate_for_same_action_can_match(tmp_path):
    d = act(doc())
    extra = copy.deepcopy(d["mandates"][0])
    extra["id"] = "M1b"
    d["mandates"][0]["revoked"] = True
    d["mandates"].append(extra)
    assert check(tmp_path, d) == (0, "ALLOW M1b")


# --- the shipped mandates.json encodes the sample's standing OKs, unwidened ---

def shipped():
    return json.loads((KIT / "mandates.json").read_text())


def test_shipped_is_observe_and_unpaused():
    d = shipped()
    assert d["shepherd_mode"] == "observe" and d["paused"] is False
    for k in ("policy_version", "paused_by", "paused_at", "reason"):
        assert k in d


def test_shipped_mandates_well_formed():
    d = shipped()
    actions = {m["action"]: m for m in d["mandates"]}
    assert set(actions) == {"ff_fix_push", "factual_fix_reply", "rerun_failed_outage",
                            "main_merge_push", "update_branch", "arm_automerge",
                            "approve_review_plan", "clear_hold"}
    for m in d["mandates"]:
        # M7 belongs to the optional reviewer and ships revoked; every other OK ships active
        assert m["granted_by"] == "user" and m["revoked"] is (m["action"] == "approve_review_plan")
        assert m["scope"]["repo"] == REPO and m["scope"]["base"] == "main"
        assert m["source"] and m["granted_at"] and m["id"]
        if m["action"] == "approve_review_plan":
            assert m["holder"] == "conductor"
        else:
            assert m["holder"] == "shepherd" and m["scope"]["pr_author"] == "your-login"
    assert actions["arm_automerge"]["conditions"]["max_changed_lines"] == 1500
    assert actions["arm_automerge"]["conditions"]["never_prs"] == []
    assert actions["update_branch"]["conditions"]["never_prs"] == []
    plan = actions["approve_review_plan"]["conditions"]["workflows"]
    assert sorted(plan) == ["branch-review",
                            "pr-review"]


def test_shipped_forbidden_set():
    f = set(shipped()["forbidden_without_user"])
    assert {"force_push", "open_pr", "open_issue", "close", "merge_outside_gate",
            "security_decision", "other_github_text", "release", "consent_and_keys"} <= f
    assert not f & {m["action"] for m in shipped()["mandates"]}


@pytest.mark.parametrize("action", ["ff_fix_push", "main_merge_push", "arm_automerge"])
def test_shipped_file_observes_shepherd_actions(tmp_path, action):
    rc, out = check(tmp_path, shipped(), action=action)
    assert rc == 3 and out.startswith("OBSERVE ")


# --- pre-write re-check: policy version, head, GO ---

H1, H2, H3, H4 = "1" * 40, "2" * 40, "3" * 40, "4" * 40
FUTURE, PAST = "2999-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"
DEST = ("--push-dest", URL, "--push-url", URL, "--fetch-url", URL, "--url-rewrites", "", "--remote-config", "")
PIN = ("--branch", "fix/x", "--new-head", H3, "--local-head", H3, *DEST)


def recheck(tmp_path, data, *extra, action="ff_fix_push", go_rows=None):
    path = write_json(tmp_path / "mandates.json", data)
    if go_rows is not None:
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "go.jsonl").write_text("".join(json.dumps(r) + "\n" for r in go_rows))
    args = ["--mandates", path, "--holder", "shepherd", "--action", action,
            "--pr-author", "your-login", "--base", "main", *extra]
    r = run("check_mandate.py", *args, env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def go_row(**over):
    row = {"id": "101-ff_fix_push-11111111", "action": "ff_fix_push", "repo": REPO, "pr": "101",
           "branch": "fix/x", "head": H1, "new_head": H3, "body_sha256": None,
           "policy_version": "test-1", "granted_by": "conductor GO message",
           "at": "2026-11-08T12:00:00+00:00", "expires_at": FUTURE, "revoked": False,
           "writer_role": "conductor", "writer_terminal": "c0ffee01"}
    row.update(over)
    return row


GO_ARGS = ("--go-id", "101-ff_fix_push-11111111", "--pr", "101",
           "--expect-head", H1, "--remote-head", H1, "--expect-policy-version", "test-1", *PIN)


def test_policy_version_bump_denies(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), "--expect-policy-version", "test-0")
    assert rc == 1 and out.startswith("DENY policy_changed")


def test_policy_version_same_allows(tmp_path):
    assert recheck(tmp_path, act(doc()), "--expect-policy-version", "test-1") == (0, "ALLOW M1")


def test_head_moved_denies(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), "--expect-head", H1, "--remote-head", H2)
    assert rc == 1 and out.startswith("DENY head_moved")


def test_head_same_allows(tmp_path):
    assert recheck(tmp_path, act(doc()), "--expect-head", H1, "--remote-head", H1, *PIN) == (0, "ALLOW M1")


@pytest.mark.parametrize("args", [("--expect-head", H1), ("--remote-head", H1),
                                  ("--expect-head", "abc", "--remote-head", "abc"),
                                  ("--expect-head", "AB" * 20, "--remote-head", "AB" * 20)])
def test_half_or_bad_head_check_denies(tmp_path, args):
    rc, out = recheck(tmp_path, act(doc()), *args)
    assert rc == 1 and out.startswith("DENY head_check")


def test_paused_beats_everything_at_write_time(tmp_path):
    d = act(doc(paused=True, paused_by="user", paused_at="t", reason="r"))
    assert recheck(tmp_path, d, *GO_ARGS, go_rows=[go_row()])[0] == 4


def test_revoked_after_dispatch_denies_at_write_time(tmp_path):
    d = act(doc())
    d["mandates"][0]["revoked"] = True
    rc, out = recheck(tmp_path, d, "--expect-head", H1, "--remote-head", H1, *PIN)
    assert rc == 1 and out.startswith("DENY revoked")


def test_observe_without_go_is_not_allow(tmp_path):
    rc, out = recheck(tmp_path, doc(), "--expect-head", H1, "--remote-head", H1, *PIN)
    assert rc == 3 and out.startswith("OBSERVE")


def test_observe_with_matching_go_allows(tmp_path):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row()])
    assert (rc, out) == (0, "ALLOW M1 go=101-ff_fix_push-11111111")


def test_go_missing_denies(tmp_path):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row(id="other")])
    assert rc == 1 and out.startswith("DENY go_missing")


def test_go_file_absent_denies(tmp_path):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS)
    assert rc == 1 and out.startswith("DENY go_missing")


@pytest.mark.parametrize("field,value", [("action", "main_merge_push"), ("pr", "102"),
                                         ("head", H2), ("policy_version", "test-0"),
                                         ("body_sha256", "e" * 64), ("repo", "someone/fork"),
                                         ("branch", "fix/y"), ("new_head", H4), ("new_head", None)])
def test_go_must_match_every_field(tmp_path, field, value):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row(**{field: value})])
    if (field, value) == ("new_head", None):  # a push GO without new_head is not a valid row
        assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:1:new_head:missing_for_push"), out
    else:
        assert rc == 1 and out.startswith(f"DENY go_mismatch:{field}"), out


def test_go_binds_the_body(tmp_path):
    body = tmp_path / "reply.md"
    body.write_text("Fixed both P3s.\n")
    import hashlib
    h = hashlib.sha256(body.read_bytes()).hexdigest()
    rows = [go_row(body_sha256=h)]
    assert recheck(tmp_path, doc(), *GO_ARGS, "--body-file", body, go_rows=rows)[0] == 0
    body.write_text("Fixed both P3s. Also something else.\n")
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, "--body-file", body, go_rows=rows)
    assert rc == 1 and out.startswith("DENY go_mismatch:body_sha256")


def test_go_without_body_refuses_a_body(tmp_path):
    body = tmp_path / "reply.md"
    body.write_text("x\n")
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, "--body-file", body, go_rows=[go_row()])
    assert rc == 1 and out.startswith("DENY go_mismatch:body_sha256")


def revoke_row(wid="101-ff_fix_push-11111111", **over):
    row = {"id": wid, "revoked": True, "by": "conductor", "at": "2026-11-08T13:00:00+00:00",
           "writer_role": "conductor", "writer_terminal": "c0ffee01"}
    row.update(over)
    return row


def test_go_revoked_later_denies(tmp_path):
    rows = [go_row(), revoke_row()]
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=rows)
    assert (rc, out) == (1, "DENY go_mismatch:revoked:101-ff_fix_push-11111111")


def test_go_without_granted_by_denies(tmp_path):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row(granted_by="")])
    assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:1:granted_by:empty")


def test_go_needs_pr_and_head(tmp_path):
    rc, out = recheck(tmp_path, doc(), "--go-id", "101-ff_fix_push-11111111", *PIN, go_rows=[go_row()])
    assert rc == 1 and out.startswith("DENY go_needs")


def test_go_checked_in_act_mode_too(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(head=H2)])
    assert rc == 1 and out.startswith("DENY go_mismatch:head")


def test_corrupt_go_file_denies(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "go.jsonl").write_text("{broken\n")
    rc, out = recheck(tmp_path, doc(), *GO_ARGS)
    assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:1:row:not_json")


def test_go_ledger_add_then_check(tmp_path):
    env = base_env(tmp_path)
    body = tmp_path / "reply.md"
    body.write_text("Fixed.\n")
    r = ledger("add", "--id", "101-factual_fix_reply-11111111", "--action",
            "factual_fix_reply", "--pr", "101", "--head", H1, "--policy-version", "test-1",
            "--granted-by", "conductor", "--body-file", body, env=env)
    assert r.returncode == 0 and r.stdout.startswith("ADDED"), r.stdout
    d = act(doc())
    d["mandates"][0]["action"] = "factual_fix_reply"
    args = ("--go-id", "101-factual_fix_reply-11111111", "--pr", "101", "--expect-head", H1,
            "--remote-head", H1, "--body-file", body)
    assert recheck(tmp_path, d, *args, action="factual_fix_reply")[0] == 0
    r = ledger("revoke", "--id", "101-factual_fix_reply-11111111", "--by", "conductor", env=env)
    assert r.returncode == 0
    rc, out = recheck(tmp_path, d, *args, action="factual_fix_reply")
    assert rc == 1 and out.startswith("DENY go_mismatch:revoked"), out


@pytest.mark.parametrize("args", [("--head", "abc"), ("--pr", "x12"), ("--granted-by", " ")])
def test_go_ledger_rejects_bad_input(tmp_path, args):
    base = {"--id": "w1", "--action": "a", "--pr": "1", "--head": H1, "--policy-version": "v",
            "--granted-by": "c"}
    base[args[0]] = args[1]
    flat = [x for kv in base.items() for x in kv]
    r = ledger("add", *flat, env=base_env(tmp_path))
    assert r.returncode == 2 and not (tmp_path / "state" / "go.jsonl").exists()
    want = "ERROR bad target" if args[0] == "--pr" else "ERROR bad head (40 lowercase hex) or granted_by"
    assert r.stdout.startswith(want), r.stdout  # the CLI's own message, before the schema's row_refused


# --- clear_hold always needs the conductor's GO ---

def test_clear_hold_needs_go_even_in_act_mode(tmp_path):
    d = act(shipped())
    rc, out = check(tmp_path, d, action="clear_hold")
    assert rc == 3 and out == "OBSERVE M8-clear-hold"


def test_clear_hold_with_go_allows_in_act_mode(tmp_path):
    d = act(shipped())
    row = go_row(id="101-clear_hold-x", action="clear_hold", policy_version=d["policy_version"],
                 branch=None, new_head=None)
    args = ("--go-id", "101-clear_hold-x", "--pr", "101", "--expect-head", H1, "--remote-head", H1)
    assert recheck(tmp_path, d, *args, action="clear_hold", go_rows=[row]) == (0, "ALLOW M8-clear-hold")


def test_other_act_mode_mandates_still_allow_without_go(tmp_path):
    assert check(tmp_path, act(shipped()), action="ff_fix_push") == (0, "ALLOW M1-ff-fix-push")


def test_shipped_clear_hold_mandate_is_go_always():
    m = [x for x in shipped()["mandates"] if x["action"] == "clear_hold"][0]
    assert m["conditions"]["go_always_required"] is True and m["holder"] == "shepherd"


def test_go_ledger_records_the_item(tmp_path):
    env = base_env(tmp_path)
    r = ledger("add", "--id", "w", "--action", "clear_hold", "--pr", "101", "--head", H1,
            "--policy-version", "v", "--granted-by", "c", "--body-sha256", "e" * 64, "--item",
            "review:7", env=env)
    assert r.returncode == 0
    row = json.loads((tmp_path / "state" / "go.jsonl").read_text())
    assert row["item"] == "review:7" and row["body_sha256"] == "e" * 64
    r = ledger("add", "--id", "w2", "--action", "clear_hold", "--pr", "101", "--head", H1,
            "--policy-version", "v", "--granted-by", "c", "--item", "commit:7", env=env)
    assert r.returncode == 2


# --- a user one-off is a GO record, and the full check still runs right before the write ---

ONE = "1270-open_pr-x"


def oneoff_row(**over):
    row = {"id": ONE, "action": "open_pr", "repo": REPO, "pr": None, "branch": "fix/new",
           "head": H1, "new_head": None, "body_sha256": None, "policy_version": "test-1",
           "granted_by": "user", "relayed_by": "<old owner id>", "at": "2026-11-09T09:00:00+00:00",
           "expires_at": FUTURE, "revoked": False, "writer_role": "conductor", "writer_terminal": "c0ffee01"}
    row.update(over)
    return row


def oneoff(tmp_path, data, rows, body="Title\n\nBody.\n", *extra):
    import hashlib
    f = tmp_path / "pr-text.md"
    f.write_text(body)
    for r in rows:
        if r.get("body_sha256") == "AUTO":
            r["body_sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
    args = ("--go-id", ONE, "--branch", "fix/new", "--expect-head", H1, "--remote-head", H1,
            "--expect-policy-version", "test-1", "--body-file", f, *extra)
    return recheck(tmp_path, data, *args, action="open_pr", go_rows=rows)


def test_oneoff_user_go_allows_exactly_that_forbidden_action(tmp_path):
    assert oneoff(tmp_path, doc(), [oneoff_row(body_sha256="AUTO")]) == (0, f"ALLOW oneoff go={ONE}")
    assert oneoff(tmp_path, act(doc()), [oneoff_row(body_sha256="AUTO")]) == (0, f"ALLOW oneoff go={ONE}")


def test_without_a_go_open_pr_stays_forbidden(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), "--branch", "fix/new", "--expect-head", H1, "--remote-head", H1,
                      action="open_pr")
    assert rc == 1 and out == "DENY forbidden_without_user:open_pr"


@pytest.mark.parametrize("over,why", [
    ({"granted_by": "conductor <old owner id> GO"}, "DENY forbidden_without_user:open_pr"),
    ({"relayed_by": ""}, "DENY state_corrupt:go.jsonl:1:relayed_by:empty"),
    ({"relayed_by": None}, "DENY state_corrupt:go.jsonl:1:relayed_by:null"),
    ({"policy_version": "test-0"}, f"DENY go_mismatch:policy_version:{ONE}"),
    ({"body_sha256": "e" * 64}, f"DENY go_mismatch:body_sha256:{ONE}"),
    ({"branch": "fix/other"}, f"DENY go_mismatch:branch:{ONE}"),
    ({"repo": "someone/fork"}, f"DENY go_mismatch:repo:{ONE}"),
    ({"action": "close"}, f"DENY go_mismatch:action:{ONE}"),
    ({"head": H2}, f"DENY go_mismatch:head:{ONE}"),
    ({"expires_at": PAST}, f"DENY go_mismatch:expired:{ONE}"),
    ({"expires_at": None}, "DENY state_corrupt:go.jsonl:1:expires_at:null"),
    ({"expires_at": "2999-01-01T00:00:00"}, "DENY state_corrupt:go.jsonl:1:expires_at:no_zone"),
    ({"revoked": True}, "DENY state_corrupt:go.jsonl:1:action:unknown_field"),
])
def test_oneoff_is_bound_to_everything(tmp_path, over, why):
    row = oneoff_row(body_sha256="AUTO")
    row.update(over)
    assert oneoff(tmp_path, act(doc()), [row]) == (1, why)


def test_oneoff_body_changed_after_dispatch_denies(tmp_path):
    rows = [oneoff_row(body_sha256="AUTO")]
    assert oneoff(tmp_path, doc(), rows)[0] == 0
    rc, out = oneoff(tmp_path, doc(), rows, "Title\n\nBody, edited.\n")
    assert (rc, out) == (1, f"DENY go_mismatch:body_sha256:{ONE}")


def test_oneoff_revoked_after_dispatch_denies(tmp_path):
    rows = [oneoff_row(body_sha256="AUTO"), revoke_row(ONE)]
    assert oneoff(tmp_path, doc(), rows) == (1, f"DENY go_mismatch:revoked:{ONE}")


def test_oneoff_policy_bump_after_dispatch_denies(tmp_path):
    """The review's probe: a policy bump after dispatch, paused false, must stop the write."""
    rows = [oneoff_row(body_sha256="AUTO")]
    rc, out = oneoff(tmp_path, doc(policy_version="test-2"), rows)
    assert rc == 1 and out.startswith("DENY policy_changed:test-2!=test-1")


def test_oneoff_pause_wins(tmp_path):
    d = doc(paused=True, paused_by="user", paused_at="t", reason="r")
    assert oneoff(tmp_path, d, [oneoff_row(body_sha256="AUTO")]) == (4, "PAUSED")


def test_oneoff_head_moved_denies(tmp_path):
    rc, out = recheck(tmp_path, doc(), "--go-id", ONE, "--branch", "fix/new", "--expect-head", H1,
                      "--remote-head", H2, action="open_pr", go_rows=[oneoff_row()])
    assert rc == 1 and out.startswith("DENY head_moved")


def test_oneoff_also_covers_an_uncovered_scope(tmp_path):
    """A push on another author's branch has no mandate; only an exact user one-off allows it."""
    rows = [go_row(granted_by="user", relayed_by="<old owner id>")]
    args = ("--go-id", "101-ff_fix_push-11111111", "--pr", "101", "--expect-head", H1,
            "--remote-head", H1, *PIN)
    p = write_json(tmp_path / "mandates.json", act(doc()))
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "go.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    r = run("check_mandate.py", "--mandates", p, "--holder", "shepherd", "--action", "ff_fix_push",
            "--pr-author", "other-author", "--base", "main", *args, env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "ALLOW oneoff go=101-ff_fix_push-11111111")
    rows[0]["granted_by"] = "conductor"
    (tmp_path / "state" / "go.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    r = run("check_mandate.py", "--mandates", p, "--holder", "shepherd", "--action", "ff_fix_push",
            "--pr-author", "other-author", "--base", "main", *args, env=base_env(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("DENY wrong_author")


def test_go_ledger_records_a_user_oneoff(tmp_path):
    env = base_env(tmp_path)
    f = tmp_path / "pr-text.md"
    f.write_text("Title\n\nBody.\n")
    base = ["add", "--id", ONE, "--action", "open_pr", "--branch", "fix/new", "--head", H1,
            "--policy-version", "test-1", "--granted-by", "user", "--body-file", f, "--expires-in", "30"]
    r = ledger(*base, env=env)
    assert r.returncode == 2 and "relayed-by" in r.stdout and not (tmp_path / "state" / "go.jsonl").exists()
    r = ledger(*base, "--relayed-by", "<old owner id>", env=env)
    assert r.returncode == 0, r.stdout
    row = json.loads((tmp_path / "state" / "go.jsonl").read_text())
    assert row["granted_by"] == "user" and row["relayed_by"] == "<old owner id>" and row["pr"] is None
    assert row["repo"] == REPO and row["branch"] == "fix/new" and row["new_head"] is None
    from datetime import datetime
    left = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["at"])
    assert left.total_seconds() == 30 * 60
    assert oneoff(tmp_path, doc(), [row]) == (0, f"ALLOW oneoff go={ONE}")


@pytest.mark.parametrize("args", [("--expires-in", "0"), ("--expires-in", "1441"), ("--repo", "nope"),
                                  ("--branch", "-x"), ("--branch", "a..b")])
def test_go_ledger_rejects_bad_target_or_expiry(tmp_path, args):
    base = {"--id": "w1", "--action": "factual_fix_reply", "--pr": "1", "--head": H1,
            "--policy-version": "v", "--granted-by": "c"}
    base[args[0]] = args[1]
    flat = [x for kv in base.items() for x in kv]
    r = ledger("add", *flat, env=base_env(tmp_path))
    assert r.returncode == 2 and not (tmp_path / "state" / "go.jsonl").exists(), r.stdout


def test_go_ledger_needs_a_target(tmp_path):
    r = ledger("add", "--id", "w", "--action", "x", "--head", H1, "--policy-version", "v",
            "--granted-by", "c", env=base_env(tmp_path))
    assert r.returncode == 2 and r.stdout.startswith("ERROR bad target: --pr"), r.stdout


# --- a push GO pins the exact commit to publish ---

def test_push_pin_local_branch_moved_denies(tmp_path):
    """Approved B (new_head), branch extended to unapproved C: refused before the push."""
    args = [*GO_ARGS]
    args[args.index("--local-head") + 1] = H4
    rc, out = recheck(tmp_path, doc(), *args, go_rows=[go_row()])
    assert rc == 1 and out == f"DENY local_head_moved:new_head={H3[:8]} local={H4[:8]}"


def test_push_pin_go_new_head_must_match(tmp_path):
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row(new_head=H4)])
    assert (rc, out) == (1, "DENY go_mismatch:new_head:101-ff_fix_push-11111111")


@pytest.mark.parametrize("drop", ["--new-head", "--local-head", "--branch"])
@pytest.mark.parametrize("mode", ["act", "observe"])
def test_push_pre_write_check_needs_the_pin(tmp_path, drop, mode):
    args = list(GO_ARGS if mode == "observe" else ("--expect-head", H1, "--remote-head", H1, *PIN))
    i = args.index(drop)
    del args[i:i + 2]
    d = doc() if mode == "observe" else act(doc())
    rc, out = recheck(tmp_path, d, *args, go_rows=[go_row()])
    assert rc == 1 and out.startswith("DENY push_pin_needs"), out


@pytest.mark.parametrize("new,loc", [("abc", "abc"), ("", ""), ("A" * 40, "A" * 40)])
def test_push_pin_bad_sha_denies(tmp_path, new, loc):
    rc, out = recheck(tmp_path, act(doc()), "--expect-head", H1, "--remote-head", H1, "--branch", "fix/x",
                      "--new-head", new, "--local-head", loc)
    assert rc == 1 and out.startswith("DENY push_pin_bad_sha"), out


def test_push_go_ledger_needs_new_head_and_branch(tmp_path):
    env = base_env(tmp_path)
    base = ["add", "--id", "w", "--action", "ff_fix_push", "--pr", "101", "--head", H1,
            "--policy-version", "test-1", "--granted-by", "conductor GO"]
    for extra in ((), ("--new-head", H3), ("--branch", "fix/x"), ("--new-head", "abc", "--branch", "fix/x")):
        r = ledger(*base, *extra, env=env)
        assert r.returncode == 2 and r.stdout.startswith("ERROR a push GO needs --new-head"), r.stdout
    assert not (tmp_path / "state" / "go.jsonl").exists()
    assert ledger(*base, "--new-head", H3, "--branch", "fix/x", env=env).returncode == 0
    row = json.loads((tmp_path / "state" / "go.jsonl").read_text())
    assert row["new_head"] == H3 and row["branch"] == "fix/x"
    r = ledger("add", "--id", "w2", "--action", "factual_fix_reply", "--pr", "101", "--head", H1,
            "--policy-version", "v", "--granted-by", "c", "--new-head", H3, env=env)
    assert r.returncode == 2 and r.stdout.startswith("ERROR --new-head is for push actions only"), r.stdout


# --- only the conductor writes go.jsonl ---

@pytest.mark.parametrize("role", ["shepherd", "publisher", "Conductor", "conductor ", ""])
@pytest.mark.parametrize("cmd", ["add", "revoke"])
def test_go_ledger_refuses_a_non_conductor_writer(tmp_path, role, cmd):
    args = (["--id", "w", "--action", "factual_fix_reply", "--pr", "101", "--head", H1,
             "--policy-version", "test-1", "--granted-by", "user", "--relayed-by", "<old owner id>"]
            if cmd == "add" else ["--id", "w", "--by", "x"])
    r = ledger(cmd, *args, env=base_env(tmp_path), role=role)
    assert r.returncode == 2 and r.stdout.startswith("ERROR only the conductor writes go.jsonl"), r.stdout
    assert not (tmp_path / "state" / "go.jsonl").exists()


@pytest.mark.parametrize("cmd", ["add", "revoke"])
def test_go_ledger_needs_the_writer_role_and_terminal(tmp_path, cmd):
    grant = ["--id", "w", "--action", "factual_fix_reply", "--pr", "101", "--head", H1,
             "--policy-version", "test-1", "--granted-by", "c"]
    args = grant if cmd == "add" else ["--id", "w", "--by", "x"]
    led = tmp_path / "state" / "go.jsonl"
    if cmd == "revoke":  # a revoke names an existing grant
        assert ledger("add", *grant, env=base_env(tmp_path)).returncode == 0
    before = led.read_text() if led.exists() else None
    assert ledger(cmd, *args, env=base_env(tmp_path), role=None).returncode == 2       # flag missing
    for term in (None, "", "a b"):
        r = ledger(cmd, *args, env=base_env(tmp_path), terminal=term)
        assert r.returncode == 2 and "CAO_TERMINAL_ID" in r.stdout, r.stdout
    assert (led.read_text() if led.exists() else None) == before
    r = ledger(cmd, *args, env=base_env(tmp_path))
    assert r.returncode == 0, r.stdout
    row = json.loads(led.read_text().splitlines()[-1])
    assert (row["writer_role"], row["writer_terminal"]) == ("conductor", "c0ffee01")


@pytest.mark.parametrize("over,why", [({"writer_role": "shepherd"}, "writer_role:value"),
                                      ({"writer_role": None}, "writer_role:missing"),
                                      ({"writer_role": "Conductor"}, "writer_role:value"),
                                      ({"writer_terminal": ""}, "writer_terminal:format"),
                                      ({"writer_terminal": None}, "writer_terminal:missing")])
def test_go_written_by_another_role_is_refused(tmp_path, over, why):
    row = go_row(**over)
    for k, v in over.items():
        if v is None:
            del row[k]
    rc, out = recheck(tmp_path, doc(), *GO_ARGS, go_rows=[row])
    assert (rc, out) == (1, f"DENY state_corrupt:go.jsonl:1:{why}")
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[row])
    assert (rc, out) == (1, f"DENY state_corrupt:go.jsonl:1:{why}")


def test_forged_user_oneoff_by_the_shepherd_is_refused(tmp_path):
    """The flagged gap: a shepherd-written record claiming granted_by=user never allows."""
    rows = [oneoff_row(body_sha256="AUTO", writer_role="shepherd", writer_terminal="feedface")]
    assert oneoff(tmp_path, doc(), rows) == (1, "DENY state_corrupt:go.jsonl:1:writer_role:value")
    rows = [oneoff_row(body_sha256="AUTO")]
    assert oneoff(tmp_path, doc(), rows) == (0, f"ALLOW oneoff go={ONE}")


# --- the push destination is bound to the repo's authorised URL ---

def dest_check(tmp_path, d=None, **over):
    """An act-mode pre-write push check; over replaces one destination flag's value (None drops it)."""
    flags = {"--push-dest": URL, "--push-url": URL, "--fetch-url": URL, "--url-rewrites": "", "--remote-config": ""}
    flags.update({"--" + k.replace("_", "-"): v for k, v in over.items()})
    args = ["--repo", REPO, "--expect-head", H1, "--remote-head", H1, "--branch", "fix/x",
            "--new-head", H3, "--local-head", H3]
    for k, v in flags.items():
        if v is not None:
            args += [k, v]
    return recheck(tmp_path, d or act(doc()), *args)


def test_f1_exact_destination_allows(tmp_path):
    assert dest_check(tmp_path) == (0, "ALLOW M1")


@pytest.mark.parametrize("flag", ["push_dest", "push_url", "fetch_url", "url_rewrites"])
def test_f1_every_destination_flag_is_required(tmp_path, flag):
    assert dest_check(tmp_path, **{flag: None}) == (
        1, "DENY push_dest_needs:--push-dest, --push-url, --fetch-url and --url-rewrites")


def test_f1_destination_needed_in_observe_mode_with_a_go_too(tmp_path):
    args = [a for a in GO_ARGS]
    i = args.index("--push-url")
    del args[i:i + 2]
    rc, out = recheck(tmp_path, doc(), *args, go_rows=[go_row()])
    assert (rc, out) == (1, "DENY push_dest_needs:--push-dest, --push-url, --fetch-url and --url-rewrites")


@pytest.mark.parametrize("flag", ["push_url", "fetch_url"])
@pytest.mark.parametrize("value,want", [
    ("https://github.com/attacker/cli-agent-orchestrator.git", "mismatch"),
    ("https://github.com/your-org/your-repo.git/", "mismatch"),
    ("HTTPS://github.com/your-org/your-repo.git", "mismatch"),
    ("git@github.com:your-org/your-repo.git", "mismatch"),
    (f"{URL}\nhttps://github.com/attacker/x.git", "count:2"),
    (f"{URL}\n{URL}", "count:2"),
    ("", "count:0")])
def test_f1_observed_url_must_be_exactly_one_authorised_url(tmp_path, flag, value, want):
    rc, out = dest_check(tmp_path, **{flag: value})
    assert rc == 1 and out.startswith(f"DENY {flag}_{want}"), out


def test_f1_repeated_flag_counts_as_two_urls(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), "--expect-head", H1, "--remote-head", H1, "--branch", "fix/x",
                      "--new-head", H3, "--local-head", H3, *DEST, "--push-url", URL)
    assert (rc, out) == (1, "DENY push_url_count:2 (want exactly one URL)")


@pytest.mark.parametrize("value", ["https://github.com/attacker/cli-agent-orchestrator.git", URL + "/", ""])
def test_f1_push_dest_must_be_the_authorised_url(tmp_path, value):
    rc, out = dest_check(tmp_path, push_dest=value)
    assert (rc, out) == (1, f"DENY push_dest_mismatch (not the authorised URL for {REPO})")


@pytest.mark.parametrize("flag", ["push_dest", "push_url", "fetch_url"])
def test_f1_only_a_trailing_dot_git_is_normalised(tmp_path, flag):
    assert dest_check(tmp_path, **{flag: URL[:-4]}) == (0, "ALLOW M1")
    d = act(doc(push_urls={REPO: URL[:-4]}))
    assert dest_check(tmp_path, d) == (0, "ALLOW M1")


@pytest.mark.parametrize("rw", ["url.https://evil.example/.pushinsteadof https://github.com/",
                                "url./tmp/x.git.insteadof https://github.com/your-org/your-repo.git",
                                " x"])
def test_f1_any_url_rewrite_refuses(tmp_path, rw):
    assert dest_check(tmp_path, url_rewrites=rw) == (
        1, "DENY push_url_rewrite (an insteadOf/pushInsteadOf rule is configured)")


@pytest.mark.parametrize("urls", [None, {}, {"other/repo": URL}, {REPO: ""}, {REPO: None}, [URL]])
def test_f1_unbound_repo_refuses(tmp_path, urls):
    d = act(doc())
    if urls is None:
        del d["push_urls"]
    else:
        d["push_urls"] = urls
    rc, out = dest_check(tmp_path, d)
    assert (rc, out) == (1, f"DENY push_url_unbound:{REPO} (no mandates.json push_urls entry)")


def test_f1_destination_is_per_repo(tmp_path):
    """--repo other/x with a mandate for it: its own URL, never the CAO one."""
    d = act(doc(push_urls={REPO: URL, "other/x": "https://github.com/other/x.git"}))
    d["mandates"][0]["scope"]["repo"] = "other/x"
    path = write_json(tmp_path / "mandates.json", d)
    base = ["--mandates", path, "--holder", "shepherd", "--action", "ff_fix_push", "--pr-author", "your-login",
            "--base", "main", "--repo", "other/x", "--expect-head", H1, "--remote-head", H1,
            "--branch", "fix/x", "--new-head", H3, "--local-head", H3, "--url-rewrites", "", "--remote-config", ""]
    r = run("check_mandate.py", *base, "--push-dest", URL, "--push-url", URL, "--fetch-url", URL,
            env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (1, "DENY push_url_mismatch (not the authorised URL for other/x)")
    o = "https://github.com/other/x.git"
    r = run("check_mandate.py", *base, "--push-dest", o, "--push-url", o, "--fetch-url", o, env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "ALLOW M1")


def test_f1_non_push_actions_need_no_destination(tmp_path):
    d = act(doc())
    d["mandates"][0]["action"] = "factual_fix_reply"
    rc, out = recheck(tmp_path, d, "--expect-head", H1, "--remote-head", H1, action="factual_fix_reply")
    assert (rc, out) == (0, "ALLOW M1")


def test_f1_destination_checked_before_the_go_and_mandate(tmp_path):
    """A user one-off push is bound to the same destination."""
    rows = [go_row(granted_by="user", relayed_by="<old owner id>")]
    args = [a for a in GO_ARGS]
    args[args.index("--push-url") + 1] = "https://github.com/attacker/x.git"
    rc, out = recheck(tmp_path, doc(), *args, go_rows=rows)
    assert rc == 1 and out.startswith("DENY push_url_mismatch"), out


# --- strictness sweep: ledger rows and mandate fields never read as "nothing there" ---

@pytest.mark.parametrize("bad", [[1], None, "x", 7], ids=["list", "null", "string", "int"])
def test_sweep_non_object_go_ledger_row_denies(tmp_path, bad):
    """Pre-fix a damaged row (it might be this GO's revocation) was skipped: the GO still allowed."""
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(), bad])
    assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:2:row:not_object"), out
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[bad, go_row()])
    assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:1:row:not_object"), out


def test_sweep_intact_go_ledger_still_allows(tmp_path):
    assert recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(id="other"), go_row()])[0] == 0


def with_forbidden_mandate(**top):
    d = act(doc(**top))
    d["mandates"].append({"id": "MF", "holder": "shepherd", "action": "force_push",
                          "scope": {"repo": REPO, "pr_author": "your-login", "base": "main"},
                          "conditions": {}, "granted_by": "user", "granted_at": "2026-11-09",
                          "source": "memory:test", "revoked": False})
    return d


@pytest.mark.parametrize("value", ["missing", None, "force_push", [None], [1], [""]],
                         ids=["missing", "null", "string", "null_item", "int_item", "empty_item"])
def test_sweep_unreadable_forbidden_list_denies(tmp_path, value):
    """Pre-fix a missing or null forbidden_without_user forbade nothing: a mandate for force_push
    then allowed it."""
    d = with_forbidden_mandate()
    if value == "missing":
        del d["forbidden_without_user"]
    else:
        d["forbidden_without_user"] = value
    assert check(tmp_path, d, action="force_push") == (1, "DENY malformed_mandates:forbidden_without_user")
    assert check(tmp_path, d, action="ff_fix_push") == (1, "DENY malformed_mandates:forbidden_without_user")


def test_sweep_forbidden_list_controls(tmp_path):
    assert check(tmp_path, with_forbidden_mandate(), action="force_push") == \
        (1, "DENY forbidden_without_user:force_push")
    assert check(tmp_path, with_forbidden_mandate(forbidden_without_user=[]), action="force_push") == (0, "ALLOW MF")


@pytest.mark.parametrize("conditions", [{"go_always_required": "true"}, {"go_always_required": 1},
                                        {"go_always_required": None}, None, [], "go_always_required"],
                         ids=["str_true", "int_one", "null_flag", "null_conditions", "list", "string"])
def test_sweep_ill_typed_go_always_required_denies(tmp_path, conditions):
    """Pre-fix "true" (or a damaged conditions object) read as "no GO needed": clear_hold ALLOW in act mode."""
    d = act(shipped())
    [m for m in d["mandates"] if m["action"] == "clear_hold"][0]["conditions"] = conditions
    assert check(tmp_path, d, action="clear_hold") == (1, "DENY malformed_mandates:conditions:M8-clear-hold")


def test_sweep_go_always_required_controls(tmp_path):
    d = act(shipped())
    m8 = [m for m in d["mandates"] if m["action"] == "clear_hold"][0]
    assert check(tmp_path, d, action="clear_hold") == (3, "OBSERVE M8-clear-hold")
    m8["conditions"]["go_always_required"] = False
    assert check(tmp_path, d, action="clear_hold") == (0, "ALLOW M8-clear-hold")
    del m8["conditions"]
    assert check(tmp_path, d, action="clear_hold") == (0, "ALLOW M8-clear-hold")
