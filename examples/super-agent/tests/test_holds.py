import hashlib
import json

import pytest

from conftest import KIT, base_env, run, write_json

H = "a" * 40
REPO = "your-org/your-repo"
TARGET = ("--repo", REPO, "--pr-author", "your-login", "--base", "main")
BODY = "APPROVE (0 P1, 0 P2, 0 P3)\n\nRead the diff and re-ran the tests.\n\nFindings: None."


def pv():
    return json.loads((KIT / "mandates.json").read_text())["policy_version"]


def body_file(tmp_path, text=BODY, item="review:7"):
    src = write_json(tmp_path / "src.json", [[{"id": int(item.split(":")[1]), "body": text + "\r\n"}]])
    out = tmp_path / "body.md"
    r = run("holds.py", "body", "--from", src, "--item", item, "--out", out, env=base_env(tmp_path))
    assert r.returncode == 0 and r.stdout.startswith("BODY "), r.stdout
    return out


def go(tmp_path, **over):
    row = {"id": "w1", "action": "clear_hold", "repo": REPO, "pr": "101", "item": "review:7", "head": H,
           "body_sha256": hashlib.sha256((tmp_path / "body.md").read_bytes()).hexdigest(),
           "branch": None, "new_head": None,
           "policy_version": pv(), "granted_by": "conductor GO", "at": "2026-11-09T09:00:00+00:00",
           "expires_at": "2999-01-01T00:00:00+00:00", "revoked": False,
           "writer_role": "conductor", "writer_terminal": "c0ffee01"}
    row.update(over)
    (tmp_path / "state").mkdir(exist_ok=True)
    with open(tmp_path / "state" / "go.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")


def clear(tmp_path, bf, *extra, item="review:7", pr="101", reason="clean 0/0/0 approval", target=TARGET):
    return run("holds.py", "clear", *target, "--pr", pr, "--item", item, "--body-file", bf, "--reason", reason,
               "--go-id", "w1", *extra, env=base_env(tmp_path))


def cleared(tmp_path):
    f = tmp_path / "state" / "holds.json"
    return json.loads(f.read_text()).get("_cleared", []) if f.exists() else []


def test_body_writes_the_canonical_text(tmp_path):
    bf = body_file(tmp_path)
    assert bf.read_text() == BODY


def test_clear_with_a_matching_go(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    r = clear(tmp_path, bf)
    assert r.returncode == 0 and r.stdout.startswith("CLEARED #101 review:7"), r.stdout
    row = cleared(tmp_path)[0]
    assert row["pr"] == "101" and row["item_id"] == "review:7" and row["go_id"] == "w1"
    assert row["body_sha256"] == hashlib.sha256(BODY.encode()).hexdigest() and row["cleared_at"]


def test_clear_keeps_existing_holds(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    write_json(tmp_path / "state" / "holds.json", {"102": {
        "sha": H, "date": "2026-11-08T09:30:00Z", "head": H, "detail": "P3: x item=review:9",
        "recorded_at": "2026-11-08T10:00:00+00:00",
        "items": [{"id": "review:9", "sha256": "0" * 64, "real": True, "body": "P3: x"}]}})
    assert clear(tmp_path, bf).returncode == 0
    assert "102" in json.loads((tmp_path / "state" / "holds.json").read_text())


def test_clear_without_go_refused(tmp_path):
    r = clear(tmp_path, body_file(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("REFUSED go_missing") and not cleared(tmp_path)


@pytest.mark.parametrize("field,value", [("action", "arm_automerge"), ("pr", "102"), ("item", "review:8"),
                                         ("body_sha256", "0" * 64), ("policy_version", "old"),
                                         ("revoked", True), ("granted_by", ""),
                                         ("expires_at", "2020-01-01T00:00:00+00:00"),
                                         ("expires_at", None), ("expires_at", "2999-01-01T00:00:00")])
def test_clear_needs_every_go_field(tmp_path, field, value):
    """A well-formed GO that does not match is go_mismatch; a GO row the schema rejects makes the
    whole ledger corrupt: either way nothing is cleared."""
    bf = body_file(tmp_path)
    go(tmp_path, **{field: value})
    r = clear(tmp_path, bf)
    corrupt = {("revoked", True): "action:unknown_field", ("granted_by", ""): "granted_by:empty",
               ("expires_at", None): "expires_at:null", ("expires_at", "2999-01-01T00:00:00"): "expires_at:no_zone"}
    why = corrupt.get((field, value))
    if why:
        assert (r.returncode, r.stdout) == (1, f"REFUSED state_corrupt:go.jsonl:1:{why}\n"), r.stdout
    else:
        assert r.returncode == 1 and r.stdout.startswith("REFUSED go_mismatch"), r.stdout
    assert not cleared(tmp_path)


def revoke(tmp_path, wid="w1"):
    with open(tmp_path / "state" / "go.jsonl", "a") as fh:
        fh.write(json.dumps({"id": wid, "revoked": True, "by": "conductor", "at": "2026-11-09T10:00:00+00:00",
                             "writer_role": "conductor", "writer_terminal": "c0ffee01"}) + "\n")


def test_latest_go_record_wins(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    revoke(tmp_path)
    # the latest row for w1 is the revocation (no head): refused before any decision
    assert clear(tmp_path, bf).stdout == "REFUSED go_needs:--pr or --branch, and --expect-head\n"


def test_edited_body_file_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    bf.write_text(BODY + " Edited.")
    r = clear(tmp_path, bf)
    assert r.returncode == 1 and "go_mismatch:body_sha256" in r.stdout


def test_non_canonical_body_file_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    bf.write_text(BODY + "\n")
    r = clear(tmp_path, bf)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED body_not_canonical")


def test_real_finding_refused_even_with_a_go(tmp_path):
    bf = body_file(tmp_path, "APPROVE (0 P1, 0 P2, 1 P3)\n\nFindings:\n- P3: rename the helper.")
    go(tmp_path)
    r = clear(tmp_path, bf)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED real_findings") and not cleared(tmp_path)


def test_paused_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    d = json.loads((KIT / "mandates.json").read_text())
    d["paused"] = True
    m = write_json(tmp_path / "m.json", d)
    r = clear(tmp_path, bf, "--mandates", m)
    assert (r.returncode, r.stdout) == (1, "REFUSED paused\n") and not cleared(tmp_path)


def test_revoked_clear_hold_mandate_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    d = json.loads((KIT / "mandates.json").read_text())
    for x in d["mandates"]:
        if x["action"] == "clear_hold":
            x["revoked"] = True
    r = clear(tmp_path, bf, "--mandates", write_json(tmp_path / "m.json", d))
    assert r.returncode == 1 and "no_clear_hold_mandate" in r.stdout


@pytest.mark.parametrize("args", [{"item": "review:x"}, {"item": "commit:7"}, {"pr": "12a"},
                                  {"reason": ""}, {"reason": "two\nlines"}])
def test_bad_arguments_are_errors(tmp_path, args):
    bf = body_file(tmp_path)
    go(tmp_path)
    r = clear(tmp_path, bf, **args)
    assert r.returncode == 2 and not cleared(tmp_path)


def test_body_item_missing_is_an_error(tmp_path):
    src = write_json(tmp_path / "src.json", [{"id": 1, "body": "x"}])
    r = run("holds.py", "body", "--from", src, "--item", "review:2", "--out", tmp_path / "b",
            env=base_env(tmp_path))
    assert r.returncode == 2


@pytest.mark.parametrize("over,why", [({"writer_role": "shepherd"}, "writer_role:value"),
                                      ({"writer_role": None}, "writer_role:null"),
                                      ({"writer_terminal": ""}, "writer_terminal:format")])
def test_clear_refuses_a_go_not_written_by_the_conductor(tmp_path, over, why):
    bf = body_file(tmp_path)
    go(tmp_path, **over)
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, f"REFUSED state_corrupt:go.jsonl:1:{why}\n")
    assert not cleared(tmp_path)


# --- clearing runs the full check_mandate decision (scope, GO repo, policy, pause) ---

def mandates_with(tmp_path, **scope):
    d = json.loads((KIT / "mandates.json").read_text())
    for x in d["mandates"]:
        if x["action"] == "clear_hold":
            x["scope"].update(scope)
    return d


def test_f2_wrong_repo_go_is_refused(tmp_path):
    """a conductor-written clear_hold GO for other/repo must not clear a CAO hold."""
    bf = body_file(tmp_path)
    go(tmp_path, repo="other/repo")
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED go_mismatch:repo:w1\n") and not cleared(tmp_path)


def test_f2_go_without_repo_is_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path, repo=None)
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED state_corrupt:go.jsonl:1:repo:null\n") and not cleared(tmp_path)


@pytest.mark.parametrize("target,want", [
    (("--repo", "other/repo", "--pr-author", "your-login", "--base", "main"), "wrong_repo:M8-clear-hold repo=other/repo"),
    (("--repo", REPO, "--pr-author", "other-author", "--base", "main"), "wrong_author:M8-clear-hold author=other-author"),
    (("--repo", REPO, "--pr-author", "your-login", "--base", "release"), "wrong_base:M8-clear-hold base=release")])
def test_f2_target_outside_m8_scope_is_refused(tmp_path, target, want):
    """A GO that matches the out-of-scope target exactly still can't widen M8's scope."""
    bf = body_file(tmp_path)
    go(tmp_path, repo=target[1])
    r = clear(tmp_path, bf, target=target)
    assert (r.returncode, r.stdout) == (1, f"REFUSED {want}\n") and not cleared(tmp_path)


@pytest.mark.parametrize("scope,want", [
    ({"repo": "other/repo"}, "wrong_repo:M8-clear-hold repo=your-org/your-repo"),
    ({"pr_author": "someone"}, "wrong_author:M8-clear-hold author=your-login"),
    ({"base": "release"}, "wrong_base:M8-clear-hold base=main"),
    ({"repo": "other/repo", "pr_author": "someone", "base": "release"},
     "wrong_repo:M8-clear-hold repo=your-org/your-repo")])
def test_f2_narrowed_m8_scope_with_a_matching_new_go_is_refused(tmp_path, scope, want):
    """the user narrows M8 and bumps the policy; a fresh conductor GO at the new version
    for CAO #101 must still be refused."""
    bf = body_file(tmp_path)
    d = mandates_with(tmp_path, **scope)
    d["policy_version"] = "narrowed-1"
    m = write_json(tmp_path / "m.json", d)
    go(tmp_path, policy_version="narrowed-1")
    r = clear(tmp_path, bf, "--mandates", m)
    assert (r.returncode, r.stdout) == (1, f"REFUSED {want}\n") and not cleared(tmp_path)


def test_f2_policy_bump_voids_an_old_go(tmp_path):
    bf = body_file(tmp_path)
    d = json.loads((KIT / "mandates.json").read_text())
    d["policy_version"] = "bumped-1"
    go(tmp_path)
    r = clear(tmp_path, bf, "--mandates", write_json(tmp_path / "m.json", d))
    assert (r.returncode, r.stdout) == (1, "REFUSED go_mismatch:policy_version:w1\n")


def test_f2_paused_after_the_go_is_refused_in_act_mode_too(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    d = json.loads((KIT / "mandates.json").read_text())
    d["shepherd_mode"], d["paused"] = "act", True
    r = clear(tmp_path, bf, "--mandates", write_json(tmp_path / "m.json", d))
    assert (r.returncode, r.stdout) == (1, "REFUSED paused\n") and not cleared(tmp_path)


def test_f2_act_mode_with_a_go_clears(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path)
    d = json.loads((KIT / "mandates.json").read_text())
    d["shepherd_mode"] = "act"
    r = clear(tmp_path, bf, "--mandates", write_json(tmp_path / "m.json", d))
    assert r.returncode == 0 and r.stdout.startswith("CLEARED #101 review:7"), r.stdout


def test_f2_user_oneoff_go_does_not_replace_a_revoked_m8(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path, granted_by="user", relayed_by="<old owner id>")
    d = json.loads((KIT / "mandates.json").read_text())
    for x in d["mandates"]:
        if x["action"] == "clear_hold":
            x["revoked"] = True
    r = clear(tmp_path, bf, "--mandates", write_json(tmp_path / "m.json", d))
    assert (r.returncode, r.stdout) == (1, "REFUSED no_clear_hold_mandate\n") and not cleared(tmp_path)


def test_f2_go_with_a_bad_head_is_refused(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path, head="abc")
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED state_corrupt:go.jsonl:1:head:format\n"), r.stdout


def test_f2_item_binding_stays_on_top(tmp_path):
    bf = body_file(tmp_path)
    go(tmp_path, item="review:8")
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED go_mismatch:item:w1\n") and not cleared(tmp_path)


@pytest.mark.parametrize("drop", ["--repo", "--pr-author", "--base"])
def test_f2_target_flags_are_required(tmp_path, drop):
    bf = body_file(tmp_path)
    go(tmp_path)
    t = list(TARGET)
    i = t.index(drop)
    del t[i:i + 2]
    r = clear(tmp_path, bf, target=tuple(t))
    assert r.returncode == 2 and not cleared(tmp_path)


def test_f2_go_record_names_the_item(tmp_path):
    """holds.py reads the item from the one record for this GO id, the record the decision checks.
    A second grant with the same id is a corrupt ledger, never a newer record that wins."""
    bf = body_file(tmp_path)
    go(tmp_path, item="review:8")
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED go_mismatch:item:w1\n"), r.stdout
    go(tmp_path)
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout) == (1, "REFUSED state_corrupt:go.jsonl:2:id:duplicate\n"), r.stdout


# --- strictness sweep: a null body is not an empty one ---

@pytest.mark.parametrize("row", [{"id": 7, "body": None}, {"id": 7}, {"id": 7, "body": ["x"]}],
                         ids=["null", "missing", "list"])
def test_sweep_body_of_an_item_without_a_string_body_is_an_error(tmp_path, row):
    """Pre-fix a null body was written as an empty canonical body (BODY e3b0c442...), ready to clear."""
    src = write_json(tmp_path / "src.json", [[row]])
    out = tmp_path / "b.md"
    r = run("holds.py", "body", "--from", src, "--item", "review:7", "--out", out, env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR item review:7 has no string body") and not out.exists()


def test_sweep_empty_string_body_is_still_written(tmp_path):
    src = write_json(tmp_path / "src.json", [[{"id": 7, "body": ""}]])
    r = run("holds.py", "body", "--from", src, "--item", "review:7", "--out", tmp_path / "b.md", env=base_env(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == "BODY " + hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("bad", ["[1]", "null", '"x"'])
def test_sweep_non_object_go_row_refuses_the_clear(tmp_path, bad):
    """holds.latest_go skips it, but the shared decide() refuses: it may be the GO's revocation."""
    bf = body_file(tmp_path)
    go(tmp_path)
    with open(tmp_path / "state" / "go.jsonl", "a") as fh:
        fh.write(bad + "\n")
    r = clear(tmp_path, bf)
    assert (r.returncode, r.stdout.strip()) == (1, "REFUSED state_corrupt:go.jsonl:2:row:not_object")
    assert not cleared(tmp_path)
