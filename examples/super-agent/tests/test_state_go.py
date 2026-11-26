"""go.jsonl: grant and revoke rows, through check_mandate.py, holds.py and go_ledger.py."""
import json

import pytest

from state_helpers import *  # noqa: F401,F403


GRANT_WRONG = {"id": 7, "action": 7, "repo": 7, "pr": 101, "branch": 7, "head": 7, "new_head": 7,
               "body_sha256": 7, "policy_version": 7, "granted_by": 7, "at": 7, "expires_at": 7,
               "revoked": "false", "writer_role": 7, "writer_terminal": 7}


GRANT_NULLABLE = ("pr", "branch", "new_head", "body_sha256")


GRANT_CASES = variants(go_row(), GRANT_WRONG, GRANT_NULLABLE, discriminator="revoked") + [
    ("relayed_by-wrong_type", dict(go_row(), relayed_by=7), "relayed_by:"),
    ("relayed_by-null", dict(go_row(), relayed_by=None), "relayed_by:"),
    ("item-wrong_type", dict(go_row(), item=7), "item:"),
    ("item-null", dict(go_row(), item=None), "item:"),
    ("unknown_field", dict(go_row(), extra=1), "extra:unknown_field"),
]


def test_grant_control_allows(tmp_path):
    assert recheck(tmp_path, doc(), *GO_ARGS, go_rows=[go_row()]) == (0, f"ALLOW M1 go={WID}")


@pytest.mark.parametrize("case", GRANT_CASES, ids=[c[0] for c in GRANT_CASES])
def test_grant_row_field_fails_closed(tmp_path, case):
    _, row, why = case
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[row])
    assert rc == 1 and out.startswith(f"DENY state_corrupt:go.jsonl:1:{why}"), out


@pytest.mark.parametrize("over,why", [
    ({"pr": None, "branch": None, "action": "factual_fix_reply", "new_head": None}, "target:missing"),
    ({"branch": None}, "new_head:missing_for_push"),
    ({"action": "factual_fix_reply"}, "new_head:not_a_push"),
    ({"granted_by": "user"}, "relayed_by:missing_for_user"),
    ({"pr": "0"}, "pr:format"), ({"head": "A" * 40}, "head:format"), ({"at": "yesterday"}, "at:format"),
    ({"expires_at": "2999-01-01T00:00:00"}, "expires_at:no_zone"),
    ({"revoked": 0}, "row_type:unknown (revoked must be true or false)"),
    ({"writer_role": "Conductor"}, "writer_role:value"), ({"granted_by": "  "}, "granted_by:empty"),
    ({"policy_version": "a\nb"}, "policy_version:format"),
    ({"writer_terminal": ""}, "writer_terminal:format"), ({"writer_terminal": "a b"}, "writer_terminal:format"),
])
def test_grant_row_cross_field_and_format_checks(tmp_path, over, why):
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(**over)])
    assert (rc, out) == (1, f"DENY state_corrupt:go.jsonl:1:{why}"), out


REVOKE_WRONG = {"id": 7, "revoked": "true", "by": 7, "at": 7, "writer_role": 7, "writer_terminal": 7}


REVOKE_CASES = variants(revoke_row(), REVOKE_WRONG, discriminator="revoked") + [
    ("id-other", revoke_row("never-granted"), "id:no_such_grant"),
    ("id-empty", revoke_row(""), "id:format"),
    ("unknown_field", dict(revoke_row(), action="ff_fix_push"), "action:unknown_field"),
]


def test_revoke_control_denies_revoked(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(), revoke_row()])
    assert (rc, out) == (1, f"DENY go_mismatch:revoked:{WID}")


@pytest.mark.parametrize("case", REVOKE_CASES, ids=[c[0] for c in REVOKE_CASES])
def test_revoke_row_field_fails_closed(tmp_path, case):
    _, row, why = case
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[go_row(), row])
    assert rc == 1 and out.startswith(f"DENY state_corrupt:go.jsonl:2:{why}"), out


def test_revoke_before_its_grant_is_corrupt(tmp_path):
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=[revoke_row(), go_row()])
    assert (rc, out) == (1, "DENY state_corrupt:go.jsonl:1:id:no_such_grant")


@pytest.mark.parametrize("text,why", [("\n", "1:row:blank_line"), (" \n", "1:row:blank_line"),
                                      ('{"id": "x"', "1:row:not_json"), ("NaN\n", "1:row:not_json"),
                                      ("[]\n", "1:row:not_object")])
def test_go_ledger_damaged_lines(tmp_path, text, why):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "go.jsonl").write_text(json.dumps(go_row()) + "\n" + text)
    rc, out = recheck(tmp_path, act(doc()), *GO_ARGS)
    n, rest = why.split(":", 1)
    assert (rc, out) == (1, f"DENY state_corrupt:go.jsonl:{int(n) + 1}:{rest}"), out


def test_go_ledger_duplicate_field_is_corrupt(tmp_path):
    (tmp_path / "state").mkdir(exist_ok=True)
    line = json.dumps(go_row())[:-1] + ', "revoked": true}'
    (tmp_path / "state" / "go.jsonl").write_text(line + "\n")
    assert recheck(tmp_path, act(doc()), *GO_ARGS) == (1, "DENY state_corrupt:go.jsonl:1:row:not_json")


def test_holds_clear_reads_the_same_ledger_strictly(tmp_path):
    """holds.py clear refuses on any corrupt go.jsonl row, even one for another GO id."""
    r = clear_item(tmp_path, MAINTAINER_CLEAN)
    assert r.stdout.startswith("CLEARED"), r.stdout
    with open(tmp_path / "state" / "go.jsonl", "a") as fh:
        fh.write(json.dumps(revoke_row("101-clear_hold-x", by=None)) + "\n")
    r = clear_item(tmp_path, MAINTAINER_CLEAN, go=False)
    assert r.returncode == 1 and r.stdout.startswith("REFUSED state_corrupt:go.jsonl:"), r.stdout


def shipped_arm_check(tmp_path, wid):
    r = run("check_mandate.py", "--holder", "shepherd", "--action", "arm_automerge", "--pr-author", "your-login",
            "--base", "main", "--go-id", wid, "--pr", "101", "--expect-head", HEAD, "--remote-head", HEAD,
            "--expect-policy-version", pv(), env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def arm_go(tmp_path, wid):
    r = ledger("add", "--id", wid, "--action", "arm_automerge", "--pr", "101", "--head", HEAD,
               "--policy-version", pv(), "--granted-by", "conductor GO", env=base_env(tmp_path))
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("bad", ["removed", None, 7, "", "other-id"], ids=["removed", "null", "int", "empty", "other_id"])
def test_f5_partial_revocation_denies(tmp_path, bad):
    """real grant -> ALLOW; real revoke -> DENY revoked; the revoke row without a valid id
    used to be skipped, so the GO allowed again. Now the ledger is corrupt: DENY."""
    arm_go(tmp_path, "w-arm")
    rc, out = shipped_arm_check(tmp_path, "w-arm")
    assert rc == 0 and out.startswith("ALLOW M6") and out.endswith("go=w-arm"), out
    assert ledger("revoke", "--id", "w-arm", "--by", "conductor", env=base_env(tmp_path)).returncode == 0
    assert shipped_arm_check(tmp_path, "w-arm") == (1, "DENY go_mismatch:revoked:w-arm")
    led = tmp_path / "state" / "go.jsonl"
    row = jsonl_get(led, 2)
    if bad == "removed":
        del row["id"]
    else:
        row["id"] = bad
    jsonl_set(led, 2, row)
    why = {"removed": "id:missing", None: "id:null", 7: "id:type", "": "id:format", "other-id": "id:no_such_grant"}[bad]
    assert shipped_arm_check(tmp_path, "w-arm") == (1, f"DENY state_corrupt:go.jsonl:2:{why}")


def test_f5_control_unrelated_valid_go_is_checked_normally(tmp_path):
    arm_go(tmp_path, "w-old")
    assert ledger("revoke", "--id", "w-old", "--by", "conductor", env=base_env(tmp_path)).returncode == 0
    arm_go(tmp_path, "w-new")
    rc, out = shipped_arm_check(tmp_path, "w-new")
    assert rc == 0 and out.startswith("ALLOW M6") and out.endswith("go=w-new"), out
    assert shipped_arm_check(tmp_path, "w-old") == (1, "DENY go_mismatch:revoked:w-old")


def test_go_ledger_writer_refuses_rows_its_readers_reject(tmp_path):
    env = base_env(tmp_path)
    led = tmp_path / "state" / "go.jsonl"
    base = ["--pr", "101", "--head", HEAD, "--policy-version", "v", "--granted-by", "c"]
    for args, why in ((["--id", "w", "--action", "Arm Automerge", *base], "action:format"),
                      (["--id", "w", "--action", "", *base], "action:format"),
                      (["--id", "w", "--action", "x", "--pr", "101", "--head", HEAD, "--policy-version", " ",
                        "--granted-by", "c"], "policy_version:empty"),
                      (["--id", "w", "--action", "x", "--pr", "0", "--head", HEAD, "--policy-version", "v",
                        "--granted-by", "c"], "pr:format")):
        r = ledger("add", *args, env=env)
        assert (r.returncode, r.stdout.strip()) == (2, f"ERROR row_refused:go.jsonl:1:{why}"), r.stdout
    assert not led.exists()
    r = ledger("revoke", "--id", "never-granted", "--by", "conductor", env=env)
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR row_refused:go.jsonl:1:id:no_such_grant")
    assert not led.exists()
    assert ledger("add", "--id", "w", "--action", "x", *base, env=env).returncode == 0
    r = ledger("revoke", "--id", "w", "--by", " ", env=env)
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR row_refused:go.jsonl:2:by:empty")
    assert len(led.read_text().splitlines()) == 1


def test_go_ledger_writer_refuses_to_append_to_a_corrupt_ledger(tmp_path):
    env = base_env(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    led = tmp_path / "state" / "go.jsonl"
    led.write_text(json.dumps(revoke_row("w", by=None)) + "\n")
    before = led.read_text()
    for cmd, args in (("add", ["--id", "w", "--action", "x", "--pr", "1", "--head", HEAD, "--policy-version", "v",
                               "--granted-by", "c"]), ("revoke", ["--id", "w", "--by", "c"])):
        r = ledger(cmd, *args, env=env)
        assert r.returncode == 2 and r.stdout.startswith("ERROR state_corrupt:go.jsonl:1:"), r.stdout
    assert led.read_text() == before


# --- a rerun_failed_outage GO binds its run_id ---------------------------------------

RUN = "12345678901"


def rerun_go(tmp_path, wid, *extra):
    return ledger("add", "--id", wid, "--action", "rerun_failed_outage", "--pr", "101",
                  "--head", HEAD, "--policy-version", pv(), "--granted-by", "conductor GO", *extra,
                  env=base_env(tmp_path))


def rerun_check(tmp_path, wid, *extra):
    r = run("check_mandate.py", "--holder", "shepherd", "--action", "rerun_failed_outage",
            "--pr-author", "your-login", "--base", "main", "--go-id", wid, "--pr", "101",
            "--expect-head", HEAD, "--remote-head", HEAD, "--expect-policy-version", pv(), *extra,
            env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def test_a_rerun_go_binds_exactly_its_run(tmp_path):
    r = rerun_go(tmp_path, "w-rerun", "--run-id", RUN)
    assert (r.returncode, r.stdout.strip()) == (0, "ADDED w-rerun")
    assert jsonl_get(tmp_path / "state" / "go.jsonl", 1)["run_id"] == RUN
    assert rerun_check(tmp_path, "w-rerun", "--run-id", RUN) == (0, "ALLOW M3-rerun-outage-ci go=w-rerun")
    assert rerun_check(tmp_path, "w-rerun", "--run-id", "12345678902") == (
        1, "DENY go_mismatch:run_id:w-rerun")
    assert rerun_check(tmp_path, "w-rerun") == (1, "DENY go_needs:--run-id (a rerun GO binds its run)")
    assert rerun_check(tmp_path, "w-rerun", "--run-id", "0") == (1, "DENY bad_run_id (want 1-20 digits)")


def test_go_ledger_needs_run_id_for_a_rerun_and_only_for_a_rerun(tmp_path):
    r = rerun_go(tmp_path, "w-rerun")
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR a rerun_* GO needs --run-id (1-20 digits)")
    r = rerun_go(tmp_path, "w-rerun", "--run-id", "044")
    assert r.returncode == 2
    r = ledger("add", "--id", "w-arm", "--action", "arm_automerge", "--pr", "101", "--head", HEAD,
               "--policy-version", pv(), "--granted-by", "conductor GO", "--run-id", RUN,
               env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR --run-id is for rerun_* actions only")
    assert not (tmp_path / "state" / "go.jsonl").exists()


def test_an_older_rerun_row_without_run_id_still_loads_but_binds_no_run(tmp_path):
    """Backwards compatible: run_id is optional in the schema, so the ledger stays readable; but
    such a GO no longer allows a rerun, because the check needs the run it names."""
    row = go_row(id="w-old", action="rerun_failed_outage", branch=None, new_head=None,
                 head=HEAD, policy_version=pv())
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "go.jsonl").write_text(json.dumps(row) + "\n")
    assert rerun_check(tmp_path, "w-old", "--run-id", RUN) == (1, "DENY go_mismatch:run_id:w-old")
    assert rerun_go(tmp_path, "w-new", "--run-id", RUN).returncode == 0  # the ledger is valid


@pytest.mark.parametrize("over,why", [
    ({"run_id": RUN, "action": "arm_automerge"}, "run_id:not_a_rerun"),
    ({"run_id": 12345678901}, "run_id:type"), ({"run_id": "0"}, "run_id:format"),
    ({"run_id": None}, "run_id:null"),
])
def test_a_bad_run_id_in_a_grant_row_is_corrupt(tmp_path, over, why):
    row = go_row(id="w", action="rerun_failed_outage", branch=None, new_head=None, policy_version=pv())
    row.update(over)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "go.jsonl").write_text(json.dumps(row) + "\n")
    rc, out = rerun_check(tmp_path, "w", "--run-id", RUN)
    assert rc == 1 and out.startswith(f"DENY state_corrupt:go.jsonl:1:{why}"), out
