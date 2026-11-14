import hashlib
import json

import pytest

from conftest import KIT, base_env, run

BRANCH = "branch-review.py"
PR = "pr-review.py"


def baseline(name, sha, **over):
    row = {"kind": "baseline", "workflow": name[:-3], "file": name, "sha256": sha,
           "verified_at": "2026-11-08T11:45Z", "verified_by": "user", "evidence": "test"}
    row.update(over)
    return row


def setup(tmp_path, rows, files):
    led = tmp_path / "plan-approvals.jsonl"
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    paths = []
    for name, content in files.items():
        p = tmp_path / name
        p.write_bytes(content)
        paths.append(p)
    return led, paths


def check(tmp_path, led, paths):
    r = run("plan_hash_check.py", "--ledger", led, *paths, env=base_env(tmp_path))
    return r.returncode, r.stdout


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_matching_scripts_pass(tmp_path):
    led, paths = setup(tmp_path, [baseline(PR, sha(b"pr")), baseline(BRANCH, sha(b"br"))],
                       {PR: b"pr", BRANCH: b"br"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 0 and out.count("MATCH") == 2


def test_missing_baseline_refuses(tmp_path):
    led, paths = setup(tmp_path, [baseline(PR, sha(b"pr"))], {PR: b"pr", BRANCH: b"br"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 1 and f"REFUSE missing_baseline {BRANCH}" in out


def test_empty_ledger_refuses_first_use(tmp_path):
    led, paths = setup(tmp_path, [], {PR: b"pr"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 1 and "missing_baseline" in out


def test_absent_ledger_refuses(tmp_path):
    p = tmp_path / PR
    p.write_bytes(b"pr")
    rc, out = check(tmp_path, tmp_path / "nope.jsonl", [p])
    assert rc == 1 and "missing_baseline" in out


def test_changed_script_refuses(tmp_path):
    led, paths = setup(tmp_path, [baseline(PR, sha(b"pr"))], {PR: b"pr edited"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 1 and f"REFUSE changed {PR}" in out


def approval(name, sha_, **over):
    """The conductor's approval log row (conductor-playbook section 5, step 7)."""
    row = {"kind": "approval", "at": "2026-11-09T10:00:00+00:00", "plan_id": "plan-v2:p1",
           "workflow": name[:-3], "pr": 101, "head": "a" * 40, "base": "main", "sha256": {name: sha_}}
    row.update(over)
    return row


def test_approval_rows_never_become_a_baseline(tmp_path):
    rows = [approval(PR, sha(b"new"))]
    led, paths = setup(tmp_path, rows, {PR: b"new"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 1 and "missing_baseline" in out


def test_latest_baseline_wins(tmp_path):
    rows = [baseline(PR, sha(b"old")), baseline(PR, sha(b"new"))]
    led, paths = setup(tmp_path, rows, {PR: b"new"})
    assert check(tmp_path, led, paths)[0] == 0
    led, paths = setup(tmp_path, rows, {PR: b"old"})
    assert check(tmp_path, led, paths)[0] == 1


@pytest.mark.parametrize("over", [{"sha256": "xyz"}, {"evidence": ""}, {"verified_by": None}])
def test_malformed_baseline_refuses(tmp_path, over):
    led, paths = setup(tmp_path, [baseline(PR, sha(b"pr"), **over)], {PR: b"pr"})
    rc, out = check(tmp_path, led, paths)
    assert rc == 1 and "corrupt_ledger" in out


def test_corrupt_ledger_refuses(tmp_path):
    led = tmp_path / "l.jsonl"
    led.write_text("{broken\n")
    p = tmp_path / PR
    p.write_bytes(b"pr")
    rc, out = check(tmp_path, led, [p])
    assert rc == 1 and "corrupt_ledger" in out


def test_unreadable_script_refuses(tmp_path):
    led, _ = setup(tmp_path, [baseline(PR, sha(b"pr"))], {})
    rc, out = check(tmp_path, led, [tmp_path / PR])
    assert rc == 1 and "REFUSE unreadable" in out


def test_the_kit_ledger_is_the_default(tmp_path):
    """Without SUPER_AGENT_STATE_DIR the check reads KIT/state/plan-approvals.jsonl. The sample
    ships no baseline (you record your own), so a fresh copy refuses every script."""
    if (KIT / "state" / "plan-approvals.jsonl").exists():
        pytest.skip("this kit copy has its own recorded baselines")
    p = tmp_path / PR
    p.write_bytes(b"any script")
    env = base_env(tmp_path)
    del env["SUPER_AGENT_STATE_DIR"]
    r = run("plan_hash_check.py", p, env=env)
    assert r.returncode == 1 and f"REFUSE missing_baseline {PR}" in r.stdout, r.stdout


# --- strictness sweep: a row without a kind might be the latest baseline ---

@pytest.mark.parametrize("kind", ["missing", None, "", 1, ["baseline"]],
                         ids=["missing", "null", "empty", "int", "list"])
def test_sweep_row_without_a_kind_is_a_corrupt_ledger(tmp_path, kind):
    """Pre-fix it was skipped, so the older baseline still trusted the old script: MATCH."""
    newer = baseline("w.py", sha(b"new"))
    if kind == "missing":
        del newer["kind"]
    else:
        newer["kind"] = kind
    led, paths = setup(tmp_path, [baseline("w.py", sha(b"old")), newer], {"w.py": b"old"})
    why = "row_type:unknown (kind must be baseline or approval)"
    assert check(tmp_path, led, paths) == (1, f"REFUSE corrupt_ledger state_corrupt:plan-approvals.jsonl:2:{why}\n")


def test_sweep_other_kinds_are_still_skipped(tmp_path):
    rows = [baseline("w.py", sha(b"old")), approval("w.py", sha(b"x"))]
    led, paths = setup(tmp_path, rows, {"w.py": b"old"})
    assert check(tmp_path, led, paths) == (0, f"MATCH w.py {sha(b'old')[:8]}\n")
