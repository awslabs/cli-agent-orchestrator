"""heavy.slots: up to N concurrent heavy holders (brief: heavy-slots, 2026-11-17).

Slot 1 is heavy/active/owner.json (the single-slot file), slot n is owner-<n>.json. Every
lifecycle step runs under state/heavy.lock. slots=1 (the default) behaves exactly as before,
and a holder written in the old single-file format is simply slot 1: counted, refreshable and
releasable by its token, with no migration.
"""
import json
import re
import subprocess
import sys
import time

import pytest

from conftest import BIN, KIT, base_env, run, write_json
from test_resource_gate import GO_M, fakebin  # noqa: F401  (fakebin is a fixture)

TOKEN_RE = re.compile(r"token=([0-9a-f]{32})\b")
H = 3600


def config(tmp_path, slots=None, base="resources.json", **heavy):
    cfg = json.loads((KIT / base).read_text())
    if slots is not None:
        cfg["heavy"]["slots"] = slots
    cfg["heavy"].update(heavy)
    return write_json(tmp_path / f"cfg-{slots}-{base}", cfg)


def env_for(tmp_path, fakebin, meas=None):
    d, _ = fakebin
    fake = write_json(tmp_path / "fake.json", meas or GO_M)
    env = base_env(tmp_path, SUPER_AGENT_FAKE_MEASUREMENTS=fake)
    env["PATH"] = f"{d}:{env['PATH']}"
    return env


def gate(tmp_path, fakebin, conf, *args, meas=None, stderr=False):
    r = run("resource_gate.py", "--config", conf, *args, env=env_for(tmp_path, fakebin, meas))
    assert not fakebin[1].exists(), "a real system probe ran under fake measurements"
    assert "Traceback" not in r.stderr, r.stderr
    if stderr:
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    return r.returncode, r.stdout.strip()


def acquire_ok(tmp_path, fakebin, conf, label):
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", label)
    assert rc == 0 and re.fullmatch(rf"ACQUIRED {label} token=[0-9a-f]{{32}} verdict=GO", out), out
    return TOKEN_RE.search(out).group(1)


def active(tmp_path):
    return tmp_path / "state" / "heavy" / "active"


def files(tmp_path):
    d = active(tmp_path)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def row(tmp_path, name):
    return json.loads((active(tmp_path) / name).read_text())


def age(tmp_path, name, hours):
    data = row(tmp_path, name)
    data["acquired_at"] = data["refreshed_at"] = time.time() - hours * H
    (active(tmp_path) / name).write_text(json.dumps(data))


def write_old_format_owner(tmp_path, label="live-suite", token="5" * 32, hours=0.5):
    """Exactly what the single-slot code wrote: json.dump of these five keys, in this order,
    into heavy/active/owner.json, with nothing else in heavy/active."""
    d = active(tmp_path)
    d.mkdir(parents=True)
    now = time.time() - hours * H
    with open(d / "owner.json", "w") as fh:
        json.dump({"label": label, "token": token, "acquired_at": now, "refreshed_at": now,
                   "pid": 4242}, fh)
    return token


# --- slots=1 (default): unchanged behaviour ---

@pytest.mark.parametrize("slots", [None, 1])
def test_one_slot_behaves_exactly_as_before(tmp_path, fakebin, slots):
    conf = config(tmp_path, slots)
    tok = acquire_ok(tmp_path, fakebin, conf, "a")
    assert files(tmp_path) == ["owner.json"]
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "b") == (1, "REFUSED BUSY a age=0.0h")
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", "e" * 32) == (1, "REFUSED not_owner held_by=a")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", "e" * 32) == (1, "REFUSED not_owner held_by=a")
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", tok) == (0, "REFRESHED a")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tok) == (0, "RELEASED a")
    assert not active(tmp_path).exists()
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tok) == (1, "NOT_HELD")


def test_one_slot_stale_line_unchanged(tmp_path, fakebin):
    conf = config(tmp_path)
    acquire_ok(tmp_path, fakebin, conf, "old")
    age(tmp_path, "owner.json", 4)
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "new") == (
        1, "REFUSED STALE held_by=old age=4.0h (report it to the conductor; never take it over)")
    assert row(tmp_path, "owner.json")["label"] == "old" and files(tmp_path) == ["owner.json"]


# --- slots=3 ---

def test_three_acquires_then_a_fourth_is_refused_listing_all_three(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    toks = [acquire_ok(tmp_path, fakebin, conf, x) for x in ("a", "b", "c")]
    assert files(tmp_path) == ["owner-2.json", "owner-3.json", "owner.json"]
    assert [row(tmp_path, n)["label"] for n in ("owner.json", "owner-2.json", "owner-3.json")] == ["a", "b", "c"]
    assert len(set(toks)) == 3
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "d") == (
        1, "REFUSED BUSY a age=0.0h b age=0.0h c age=0.0h")
    assert len(files(tmp_path)) == 3


def test_release_frees_exactly_one_slot(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    ta, tb, tc = (acquire_ok(tmp_path, fakebin, conf, x) for x in ("a", "b", "c"))
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tb) == (0, "RELEASED b")
    assert files(tmp_path) == ["owner-3.json", "owner.json"]
    acquire_ok(tmp_path, fakebin, conf, "d")  # takes the freed slot 2
    assert row(tmp_path, "owner-2.json")["label"] == "d"
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "e")[1].startswith("REFUSED BUSY a ")
    # b's delayed second release cannot free d's slot
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tb) == (1, "REFUSED not_owner held_by=a,d,c")
    for tok, label in ((ta, "a"), (tc, "c")):
        assert gate(tmp_path, fakebin, conf, "--release-heavy", tok) == (0, f"RELEASED {label}")
    assert files(tmp_path) == ["owner-2.json"] and active(tmp_path).is_dir()


def test_tokens_are_per_holder(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    ta, tb = acquire_ok(tmp_path, fakebin, conf, "a"), acquire_ok(tmp_path, fakebin, conf, "b")
    age(tmp_path, "owner.json", 1)
    age(tmp_path, "owner-2.json", 1)
    before_b = (active(tmp_path) / "owner-2.json").read_text()
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", ta) == (0, "REFRESHED a")
    assert time.time() - row(tmp_path, "owner.json")["refreshed_at"] < 60
    assert (active(tmp_path) / "owner-2.json").read_text() == before_b  # b untouched
    assert row(tmp_path, "owner.json")["token"] == ta and row(tmp_path, "owner-2.json")["token"] == tb
    assert gate(tmp_path, fakebin, conf, "--release-heavy", ta) == (0, "RELEASED a")
    assert files(tmp_path) == ["owner-2.json"]
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", ta) == (1, "REFUSED not_owner held_by=b")
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", tb) == (0, "REFRESHED b")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tb) == (0, "RELEASED b")
    assert not active(tmp_path).exists()


def test_label_reuse_gets_a_separate_slot_and_token(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    t1, t2 = acquire_ok(tmp_path, fakebin, conf, "suite"), acquire_ok(tmp_path, fakebin, conf, "suite")
    assert t1 != t2 and files(tmp_path) == ["owner-2.json", "owner.json"]
    assert gate(tmp_path, fakebin, conf, "--release-heavy", t2) == (0, "RELEASED suite")
    assert row(tmp_path, "owner.json")["token"] == t1


# --- stale holders: counted, reported, never taken over ---

def test_full_with_a_stale_holder_is_refused_stale(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    for x in ("a", "b", "c"):
        acquire_ok(tmp_path, fakebin, conf, x)
    age(tmp_path, "owner-2.json", 5)
    before = {n: (active(tmp_path) / n).read_text() for n in files(tmp_path)}
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "d") == (
        1, "REFUSED STALE held_by=b age=5.0h (report it to the conductor; never take it over)")
    assert {n: (active(tmp_path) / n).read_text() for n in files(tmp_path)} == before


def test_every_stale_holder_is_listed(tmp_path, fakebin):
    conf = config(tmp_path, 2)
    acquire_ok(tmp_path, fakebin, conf, "a")
    acquire_ok(tmp_path, fakebin, conf, "b")
    age(tmp_path, "owner.json", 4)
    age(tmp_path, "owner-2.json", 6)
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "c") == (
        1, "REFUSED STALE held_by=a age=4.0h held_by=b age=6.0h "
           "(report it to the conductor; never take it over)")


def test_stale_holder_still_counts_and_is_warned_when_a_slot_is_free(tmp_path, fakebin):
    conf = config(tmp_path, 2)
    acquire_ok(tmp_path, fakebin, conf, "old")
    age(tmp_path, "owner.json", 4)
    before = (active(tmp_path) / "owner.json").read_text()
    rc, out, err = gate(tmp_path, fakebin, conf, "--acquire-heavy", "new", stderr=True)
    assert rc == 0 and re.fullmatch(r"ACQUIRED new token=[0-9a-f]{32} verdict=GO", out), out
    assert err == "STALE held_by=old age=4.0h (report it to the conductor; never take it over)"
    assert (active(tmp_path) / "owner.json").read_text() == before
    # the stale holder still occupies its slot: now full
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "third")[1].startswith("REFUSED STALE held_by=old ")


def test_refresh_of_a_stale_holder_is_refused_on_any_slot(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    acquire_ok(tmp_path, fakebin, conf, "a")
    tb = acquire_ok(tmp_path, fakebin, conf, "b")
    age(tmp_path, "owner-2.json", 4)
    rc, out = gate(tmp_path, fakebin, conf, "--refresh-heavy", tb)
    assert rc == 1 and out.startswith("REFUSED STALE held_by=b age=4.0h"), out
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tb) == (0, "RELEASED b")


def test_lowering_slots_counts_existing_holders_not_slot_numbers(tmp_path, fakebin):
    three = config(tmp_path, 3)
    ta = acquire_ok(tmp_path, fakebin, three, "a")
    acquire_ok(tmp_path, fakebin, three, "b")
    acquire_ok(tmp_path, fakebin, three, "c")
    assert gate(tmp_path, fakebin, three, "--release-heavy", ta)[0] == 0
    one = config(tmp_path, 1)
    assert gate(tmp_path, fakebin, one, "--acquire-heavy", "d") == (1, "REFUSED BUSY b age=0.0h c age=0.0h")
    assert files(tmp_path) == ["owner-2.json", "owner-3.json"]


def test_empty_marker_is_a_placeholder_holder(tmp_path, fakebin):
    """A crash between mkdir and the first write: one holder nobody owns, aged by mtime."""
    active(tmp_path).mkdir(parents=True)
    assert gate(tmp_path, fakebin, config(tmp_path, 1), "--acquire-heavy", "x") == (1, "REFUSED BUSY ? age=0.0h")
    assert gate(tmp_path, fakebin, config(tmp_path, 1), "--release-heavy", "1" * 32) == (
        1, "REFUSED not_owner held_by=?")
    acquire_ok(tmp_path, fakebin, config(tmp_path, 2), "x")
    assert files(tmp_path) == ["owner.json"]


def test_last_release_drops_cut_short_writes(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    tok = acquire_ok(tmp_path, fakebin, conf, "a")
    (active(tmp_path) / "owner-2.json.tmp").write_text("{")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tok) == (0, "RELEASED a")
    assert not active(tmp_path).exists()


# --- live compatibility: an old-format owner.json written before slots existed ---

def test_old_format_holder_is_counted_and_releasable_by_its_token(tmp_path, fakebin):
    old = write_old_format_owner(tmp_path)
    conf = config(tmp_path, 3, base="resources.linux.json")
    linux = {"memory_psi_some_avg10": 0, "memory_psi_full_avg10": 0, "memory_available_gib": 40,
             "swap_used_gib": 0, "disk_free_gib": 100, "load1": 1, "ncpu": 16, "live_terminals": 1}

    def g(*args):
        return gate(tmp_path, fakebin, conf, *args, meas=linux)
    rc, out = g("--acquire-heavy", "new-1")
    assert rc == 0 and out.startswith("ACQUIRED new-1 token="), out
    rc, out = g("--acquire-heavy", "new-2")
    assert rc == 0, out
    assert g("--acquire-heavy", "new-3") == (1, "REFUSED BUSY live-suite age=0.5h new-1 age=0.0h new-2 age=0.0h")
    assert files(tmp_path) == ["owner-2.json", "owner-3.json", "owner.json"]
    assert g("--refresh-heavy", old) == (0, "REFRESHED live-suite")
    assert row(tmp_path, "owner.json")["token"] == old
    assert g("--release-heavy", old) == (0, "RELEASED live-suite")
    assert files(tmp_path) == ["owner-2.json", "owner-3.json"]
    assert g("--release-heavy", old) == (1, "REFUSED not_owner held_by=new-1,new-2")
    rc, out = g("--acquire-heavy", "new-3")
    assert rc == 0 and row(tmp_path, "owner.json")["label"] == "new-3", out


def test_old_format_holder_alone_releases_and_removes_the_marker(tmp_path, fakebin):
    old = write_old_format_owner(tmp_path, label="pr-fix-101")
    conf = config(tmp_path, 3)
    assert gate(tmp_path, fakebin, conf, "--release-heavy", "0" * 32) == (
        1, "REFUSED not_owner held_by=pr-fix-101")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", old) == (0, "RELEASED pr-fix-101")
    assert not active(tmp_path).exists()


def test_old_format_holder_under_one_slot_is_busy_then_free(tmp_path, fakebin):
    old = write_old_format_owner(tmp_path, hours=1)
    conf = config(tmp_path)
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "x") == (1, "REFUSED BUSY live-suite age=1.0h")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", old) == (0, "RELEASED live-suite")
    acquire_ok(tmp_path, fakebin, conf, "x")


def test_old_format_holder_goes_stale_like_any_other(tmp_path, fakebin):
    write_old_format_owner(tmp_path, hours=4)
    conf = config(tmp_path, 1)
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "x")[1].startswith(
        "REFUSED STALE held_by=live-suite age=4.0h")


# --- races: separate processes, all released at once against the same state ---

DRIVER = r"""
import os, runpy, sys, time
go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.0005)
sys.argv = [sys.argv[2]] + sys.argv[3:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code)
"""


def race(tmp_path, fakebin, conf, ops):
    """Start one process per op, each spinning on a start file; then create it so they all hit
    the lock together. Returns [(op, rc, stdout)]; any traceback is kept in race.tracebacks so
    callers check the slot bound first and the clean exit second."""
    driver = tmp_path / "race_driver.py"
    driver.write_text(DRIVER)
    go = tmp_path / f"go-{time.monotonic_ns()}"
    env = env_for(tmp_path, fakebin)
    procs = [(op, subprocess.Popen([sys.executable, "-I", str(driver), str(go), str(BIN / "resource_gate.py"),
                                    "--config", str(conf), *op], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)) for op in ops]
    time.sleep(0.5)
    go.write_text("")
    out, race.tracebacks = [], []
    for op, p in procs:
        stdout, stderr = p.communicate(timeout=60)
        if "Traceback" in stderr:
            race.tracebacks.append(stderr)
        out.append((op, p.returncode, stdout.strip()))
    go.unlink()
    return out


def holders_on_disk(tmp_path):
    return {n: row(tmp_path, n)["token"] for n in files(tmp_path) if not n.endswith(".tmp")}


@pytest.mark.parametrize("slots", [1, 3])
def test_racing_acquires_never_exceed_slots(tmp_path, fakebin, slots):
    conf = config(tmp_path, slots, lock_timeout_s=30)
    for _ in range(3):
        results = race(tmp_path, fakebin, conf, [("--acquire-heavy", f"r{i}") for i in range(12)])
        won = [TOKEN_RE.search(o).group(1) for _, rc, o in results if rc == 0]
        lost = [o for _, rc, o in results if rc != 0]
        on_disk = holders_on_disk(tmp_path)
        # the bound: never more winners than slots, and every winner really holds a slot
        assert len(won) <= slots and sorted(on_disk.values()) == sorted(won), results
        assert len(won) == slots and len(set(won)) == slots, results
        assert not race.tracebacks, race.tracebacks[0]
        assert all(o.startswith("REFUSED BUSY ") for o in lost), lost
        assert all(len(o.split(" age=")) - 1 == slots for o in lost), lost  # each lists N holders
        released = race(tmp_path, fakebin, conf, [("--release-heavy", t) for t in won])
        assert all(rc == 0 and o.startswith("RELEASED r") for _, rc, o in released), released
        assert not active(tmp_path).exists()


def test_racing_acquires_and_releases_keep_the_bound(tmp_path, fakebin):
    conf = config(tmp_path, 3, lock_timeout_s=30)
    held = [acquire_ok(tmp_path, fakebin, conf, x) for x in ("h1", "h2", "h3")]
    ops = [("--release-heavy", t) for t in held] + [("--acquire-heavy", f"n{i}") for i in range(8)]
    results = race(tmp_path, fakebin, conf, ops)
    won = [TOKEN_RE.search(o).group(1) for op, rc, o in results if op[0] == "--acquire-heavy" and rc == 0]
    on_disk = holders_on_disk(tmp_path)
    assert len(won) <= 3 and sorted(on_disk.values()) == sorted(won), results
    assert set(held).isdisjoint(on_disk.values()), results
    assert not race.tracebacks, race.tracebacks[0]
    rel = [(rc, o) for op, rc, o in results if op[0] == "--release-heavy"]
    assert all(rc == 0 and o.startswith("RELEASED h") for rc, o in rel), rel
    lost = [o for op, rc, o in results if op[0] == "--acquire-heavy" and rc != 0]
    assert all(o.startswith("REFUSED BUSY ") for o in lost), lost


def test_racing_against_an_old_format_holder(tmp_path, fakebin):
    old = write_old_format_owner(tmp_path)
    conf = config(tmp_path, 3, lock_timeout_s=30)
    results = race(tmp_path, fakebin, conf, [("--acquire-heavy", f"r{i}") for i in range(10)])
    won = [TOKEN_RE.search(o).group(1) for _, rc, o in results if rc == 0]
    on_disk = holders_on_disk(tmp_path)
    assert on_disk.get("owner.json") == old, results  # the live holder is never overwritten
    assert len(won) <= 2 and sorted(on_disk.values()) == sorted(won + [old]), results
    assert len(won) == 2 and not race.tracebacks, results
    assert sorted(on_disk) == ["owner-2.json", "owner-3.json", "owner.json"]
    assert gate(tmp_path, fakebin, conf, "--release-heavy", old) == (0, "RELEASED live-suite")


# --- state_schema: new files accepted, damaged ones rejected ---

def three_holders(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    return conf, [acquire_ok(tmp_path, fakebin, conf, x) for x in ("a", "b", "c")]


def check(tmp_path):
    r = run("state_schema.py", "check", "--state-dir", tmp_path / "state", env=base_env(tmp_path))
    return r.returncode, r.stdout.strip().splitlines()


def test_schema_check_accepts_every_holder_file(tmp_path, fakebin):
    three_holders(tmp_path, fakebin)
    assert check(tmp_path) == (0, ["OK 3 files"])


@pytest.mark.parametrize("text,why", [("{", "document:not_json"), ("[]", "row:not_object"),
                                      ('{"label": "b"}', "token:missing")])
def test_damaged_slot_file_fails_closed_everywhere(tmp_path, fakebin, text, why):
    conf, (ta, tb, tc) = three_holders(tmp_path, fakebin)
    (active(tmp_path) / "owner-2.json").write_text(text)
    pre = f"REFUSED state_corrupt:heavy/active/owner-2.json:1:{why}"
    for args in (("--acquire-heavy", "d"), ("--refresh-heavy", ta), ("--release-heavy", tc)):
        rc, out = gate(tmp_path, fakebin, conf, *args)
        assert rc == 1 and out.startswith(pre), (args, out)
    assert files(tmp_path) == ["owner-2.json", "owner-3.json", "owner.json"]
    assert check(tmp_path) == (1, [f"state_corrupt:heavy/active/owner-2.json:1:{why}"])


def test_duplicate_token_across_slots_fails_closed(tmp_path, fakebin):
    conf, (ta, tb, _) = three_holders(tmp_path, fakebin)
    data = row(tmp_path, "owner-3.json")
    data["token"] = ta
    (active(tmp_path) / "owner-3.json").write_text(json.dumps(data))
    for args in (("--release-heavy", ta), ("--refresh-heavy", tb), ("--acquire-heavy", "d")):
        rc, out = gate(tmp_path, fakebin, conf, *args)
        assert rc == 1 and out.startswith("REFUSED state_corrupt:heavy/active/owner-3.json:1:token:duplicate"), out
    assert len(files(tmp_path)) == 3
    rc, lines = check(tmp_path)
    assert rc == 1 and lines == ["state_corrupt:heavy/active/owner-3.json:1:token:duplicate"], lines


@pytest.mark.parametrize("name", ["owner-9.json", "owner-1.json", "owner-02.json", "notes.txt", "x.tmp"])
def test_unknown_entry_in_the_marker_fails_closed(tmp_path, fakebin, name):
    conf, (ta, _, _) = three_holders(tmp_path, fakebin)
    (active(tmp_path) / name).write_text("{}")
    pre = f"REFUSED state_corrupt:heavy/active/{name}:1:file:unknown"
    for args in (("--acquire-heavy", "d"), ("--release-heavy", ta)):
        rc, out = gate(tmp_path, fakebin, conf, *args)
        assert rc == 1 and out.startswith(pre), (args, out)
    assert check(tmp_path) == (1, [f"state_corrupt:heavy/active/{name}:1:file:unknown"])


def test_holder_name_that_is_a_directory_is_unknown(tmp_path, fakebin):
    conf, _ = three_holders(tmp_path, fakebin)
    tb = row(tmp_path, "owner-2.json")["token"]
    assert gate(tmp_path, fakebin, conf, "--release-heavy", tb)[0] == 0
    (active(tmp_path) / "owner-2.json").mkdir()
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", "d")
    assert rc == 1 and out.startswith("REFUSED state_corrupt:heavy/active/owner-2.json:1:file:unknown"), out


def test_marker_that_is_a_file_fails_closed(tmp_path, fakebin):
    active(tmp_path).parent.mkdir(parents=True)
    active(tmp_path).write_text("")
    rc, out = gate(tmp_path, fakebin, config(tmp_path, 3), "--acquire-heavy", "d")
    assert rc == 1 and out.startswith("REFUSED state_corrupt:heavy/active:1:file:not_a_directory"), out
    assert check(tmp_path) == (1, ["state_corrupt:heavy/active:1:file:not_a_directory"])


def test_holder_writes_go_through_the_schema(tmp_path, monkeypatch):
    from state_helpers import load
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    RG = load("resource_gate")
    marker = active(tmp_path)
    marker.mkdir(parents=True)
    good = {"label": "a", "token": "0" * 32, "acquired_at": 1.0, "refreshed_at": 1.0, "pid": 1}
    with pytest.raises(RG.SS.RowRefused, match=r"^row_refused:heavy/active/owner-3.json:1:token:null$"):
        RG._write_owner(str(marker), dict(good, token=None), "owner-3.json")
    with pytest.raises(RG.SS.RowRefused, match=r"^row_refused:heavy/active/owner-9.json:1:file:not_a_holder_name$"):
        RG._write_owner(str(marker), good, "owner-9.json")
    assert not any(marker.iterdir())
    RG._write_owner(str(marker), good, "owner-3.json")
    assert json.loads((marker / "owner-3.json").read_text()) == good
    SS = RG.SS
    assert [SS.heavy_holder_name(n) for n in (1, 2, 8)] == ["owner.json", "owner-2.json", "owner-8.json"]
    for bad in (0, 9, True, "1"):
        with pytest.raises(ValueError):
            SS.heavy_holder_name(bad)


# --- config ---

def test_shipped_slot_counts():
    assert json.loads((KIT / "resources.linux.json").read_text())["heavy"]["slots"] == 3
    assert "slots" not in json.loads((KIT / "resources.json").read_text())["heavy"]  # default 1


@pytest.mark.parametrize("value,why", [(0, "out_of_range"), (9, "out_of_range"), (-1, "out_of_range"),
                                       (2.5, "not_integer"), (3.0, "not_integer"), (True, "not_integer"),
                                       ("3", "not_integer"), (None, "not_integer")])
@pytest.mark.parametrize("base", ["resources.json", "resources.linux.json"])
def test_bad_slots_config_fails_closed(tmp_path, value, why, base):
    cfg = json.loads((KIT / base).read_text())
    cfg["heavy"]["slots"] = value
    path = write_json(tmp_path / "bad.json", cfg)
    r = run("resource_gate.py", "--config", path, "--acquire-heavy", "a", env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (1, f"STOP bad_config:heavy.slots:{why}") and r.stderr.startswith("hint: ")
    assert not active(tmp_path).exists()


@pytest.mark.parametrize("value", [1, 3, 8])
def test_good_slots_config_is_accepted(tmp_path, fakebin, value):
    conf = config(tmp_path, value)
    toks = [acquire_ok(tmp_path, fakebin, conf, f"s{i}") for i in range(value)]
    assert len(set(toks)) == value
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "over")[1].startswith("REFUSED BUSY s0 ")


# --- the verdict caps new acquires: GO heavy.slots, THROTTLE one, STOP none ---

THROTTLED = dict(GO_M, swap_used_gib=20)  # THROTTLE swap_gib=20.0>16
STOPPED = dict(GO_M, disk_free_gib=5)  # STOP disk_free_gib=5.0<20
CAP = "(THROTTLE swap_gib=20.0>16; one heavy run at a time while throttled)"


def test_throttle_with_no_holder_acquires(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", "a", meas=THROTTLED)
    assert rc == 0 and re.fullmatch(r"ACQUIRED a token=[0-9a-f]{32} verdict=THROTTLE", out), out
    assert files(tmp_path) == ["owner.json"]


@pytest.mark.parametrize("held", [["a"], ["a", "b"]])
def test_throttle_with_a_holder_is_capped(tmp_path, fakebin, held):
    conf = config(tmp_path, 3)
    for x in held:
        acquire_ok(tmp_path, fakebin, conf, x)
    before = {n: (active(tmp_path) / n).read_text() for n in files(tmp_path)}
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "c", meas=THROTTLED) == (
        1, f"REFUSED THROTTLE_CAP held_by={','.join(held)} {CAP}")
    assert {n: (active(tmp_path) / n).read_text() for n in files(tmp_path)} == before


def test_throttle_cap_on_the_linux_config(tmp_path, fakebin):
    conf = config(tmp_path, 3, base="resources.linux.json")
    linux = {"memory_psi_some_avg10": 0, "memory_psi_full_avg10": 0, "memory_available_gib": 40,
             "swap_used_gib": 0, "disk_free_gib": 100, "load1": 1, "ncpu": 16, "live_terminals": 1}
    busy = dict(linux, load1=32)
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", "a", meas=busy)
    assert rc == 0 and out.endswith("verdict=THROTTLE"), out
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "b", meas=busy) == (
        1, "REFUSED THROTTLE_CAP held_by=a (THROTTLE load_per_cpu=2.0>1.2; one heavy run at a time while throttled)")
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", "b", meas=linux)
    assert rc == 0 and out.endswith("verdict=GO"), out


def test_go_with_two_holders_acquires_up_to_slots(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    acquire_ok(tmp_path, fakebin, conf, "a")
    acquire_ok(tmp_path, fakebin, conf, "b")
    acquire_ok(tmp_path, fakebin, conf, "c")
    assert files(tmp_path) == ["owner-2.json", "owner-3.json", "owner.json"]
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "d") == (
        1, "REFUSED BUSY a age=0.0h b age=0.0h c age=0.0h")


@pytest.mark.parametrize("held", [[], ["a"]])
def test_stop_refuses_every_acquire(tmp_path, fakebin, held):
    conf = config(tmp_path, 3)
    for x in held:
        acquire_ok(tmp_path, fakebin, conf, x)
    rc, out = gate(tmp_path, fakebin, conf, "--acquire-heavy", "z", meas=STOPPED)
    assert rc == 1 and out.startswith("REFUSED STOP disk_free_gib=5.0<20"), out
    assert files(tmp_path) == (["owner.json"] if held else [])


@pytest.mark.parametrize("meas", [THROTTLED, STOPPED], ids=["throttle", "stop"])
def test_holders_in_place_keep_running_when_throttled_or_stopped(tmp_path, fakebin, meas):
    conf = config(tmp_path, 3)
    ta, tb = acquire_ok(tmp_path, fakebin, conf, "a"), acquire_ok(tmp_path, fakebin, conf, "b")
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", ta, meas=meas) == (0, "REFRESHED a")
    assert gate(tmp_path, fakebin, conf, "--release-heavy", ta, meas=meas) == (0, "RELEASED a")
    assert gate(tmp_path, fakebin, conf, "--refresh-heavy", tb, meas=meas) == (0, "REFRESHED b")
    assert files(tmp_path) == ["owner-2.json"]


def test_one_slot_under_throttle_is_still_plain_busy(tmp_path, fakebin):
    conf = config(tmp_path)
    acquire_ok(tmp_path, fakebin, conf, "a")
    assert gate(tmp_path, fakebin, conf, "--acquire-heavy", "b", meas=THROTTLED) == (1, "REFUSED BUSY a age=0.0h")


def test_throttle_cap_still_warns_about_a_stale_holder(tmp_path, fakebin):
    conf = config(tmp_path, 3)
    acquire_ok(tmp_path, fakebin, conf, "old")
    age(tmp_path, "owner.json", 4)
    rc, out, err = gate(tmp_path, fakebin, conf, "--acquire-heavy", "b", meas=THROTTLED, stderr=True)
    assert (rc, out) == (1, f"REFUSED THROTTLE_CAP held_by=old {CAP}")
    assert err.startswith("STALE held_by=old age=4.0h (report it to the conductor; never take it over)\n"
                          "hint: "), err
