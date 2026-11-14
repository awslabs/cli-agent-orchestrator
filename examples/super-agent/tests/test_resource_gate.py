import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import KIT, base_env, run, write_json

GO_M = {"pressure_level": 1, "free_memory_pct": 60, "swap_used_gib": 1.0, "disk_free_gib": 100.0,
        "load1": 1.0, "ncpu": 10, "live_terminals": 1}
GO, STOP, THROTTLE = 0, 1, 3


@pytest.fixture
def fakebin(tmp_path):
    """sysctl / memory_pressure / df stand-ins that record any call. Tests assert none happen."""
    d = tmp_path / "fakebin"
    d.mkdir()
    marker = tmp_path / "real-probe-called"
    for name in ("sysctl", "memory_pressure", "df", "vm_stat", "ps"):
        p = d / name
        p.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n")
        p.chmod(0o755)
    return d, marker


def gate(tmp_path, fakebin, measurements, *args, cao_url=None):
    d, marker = fakebin
    fake = write_json(tmp_path / "fake.json", measurements)
    extra = {"SUPER_AGENT_FAKE_MEASUREMENTS": fake}
    if cao_url:
        extra["SUPER_AGENT_CAO_URL"] = cao_url
    env = base_env(tmp_path, **extra)
    env["PATH"] = f"{d}:{env['PATH']}"
    r = run("resource_gate.py", *args, env=env)
    assert not marker.exists(), "a real system probe ran under fake measurements"
    return r.returncode, r.stdout.strip()


def m(**over):
    d = dict(GO_M)
    d.update(over)
    return d


def test_go(tmp_path, fakebin):
    assert gate(tmp_path, fakebin, m()) == (GO, "GO")


def test_todays_machine_is_throttle(tmp_path, fakebin):
    rc, out = gate(tmp_path, fakebin, m(pressure_level=2, swap_used_gib=18.4, disk_free_gib=49,
                                        load1=4.7, free_memory_pct=51))
    assert rc == THROTTLE and out.startswith("THROTTLE")
    assert "pressure" in out and "swap" in out and "disk" not in out and "load" not in out


@pytest.mark.parametrize("over,code,word", [
    ({"pressure_level": 4}, STOP, "pressure"),
    ({"pressure_level": 3}, THROTTLE, "pressure"),
    ({"pressure_level": 2}, THROTTLE, "pressure"),
    ({"free_memory_pct": 14.9}, STOP, "free_mem"),
    ({"free_memory_pct": 15}, THROTTLE, "free_mem"),
    ({"free_memory_pct": 29.9}, THROTTLE, "free_mem"),
    ({"free_memory_pct": 30}, GO, None),
    ({"disk_free_gib": 19.9}, STOP, "disk"),
    ({"disk_free_gib": 20}, THROTTLE, "disk"),
    ({"disk_free_gib": 39.9}, THROTTLE, "disk"),
    ({"disk_free_gib": 40}, GO, None),
    ({"live_terminals": 7}, STOP, "terminals"),
    ({"live_terminals": 6}, THROTTLE, "terminals"),
    ({"live_terminals": 4}, THROTTLE, "terminals"),
    ({"live_terminals": 3}, GO, None),
    ({"swap_used_gib": 16.1}, THROTTLE, "swap"),
    ({"swap_used_gib": 16}, GO, None),
    ({"load1": 12.1}, THROTTLE, "load"),
    ({"load1": 12.0}, GO, None),
])
def test_thresholds(tmp_path, fakebin, over, code, word):
    rc, out = gate(tmp_path, fakebin, m(**over))
    assert rc == code, out
    assert out.split()[0] == {GO: "GO", STOP: "STOP", THROTTLE: "THROTTLE"}[code]
    if word:
        assert word in out


def test_load_is_divided_by_cpu_count(tmp_path, fakebin):
    assert gate(tmp_path, fakebin, m(load1=6.1, ncpu=5))[0] == THROTTLE
    assert gate(tmp_path, fakebin, m(load1=6.0, ncpu=5))[0] == GO


@pytest.mark.parametrize("key", sorted(GO_M))
def test_unmeasured_value_throttles(tmp_path, fakebin, key):
    d = m()
    d[key] = None
    rc, out = gate(tmp_path, fakebin, d)
    assert rc == THROTTLE and "unmeasured" in out


def test_stop_reasons_are_listed(tmp_path, fakebin):
    rc, out = gate(tmp_path, fakebin, m(pressure_level=4, disk_free_gib=5))
    assert rc == STOP and "pressure" in out and "disk" in out


def test_json_prints_measurements_and_keeps_exit_code(tmp_path, fakebin):
    rc, out = gate(tmp_path, fakebin, m(swap_used_gib=20), "--json")
    data = json.loads(out)
    assert rc == THROTTLE and data["verdict"] == "THROTTLE"
    assert data["measurements"]["swap_used_gib"] == 20 and data["reasons"]


def test_thresholds_come_from_resources_json():
    cfg = json.loads((KIT / "resources.json").read_text())
    assert cfg["stop"] == {"pressure_level_gte": 4, "free_memory_pct_lt": 15,
                           "disk_free_gib_lt": 20, "live_terminals_gte": 7}
    assert cfg["throttle"] == {"pressure_level_gte": 2, "free_memory_pct_lt": 30,
                               "swap_used_gib_gt": 16, "disk_free_gib_lt": 40,
                               "load_per_cpu_gt": 1.2, "live_terminals_gte": 4}
    assert cfg["heavy"]["stale_hours"] == 3


def test_custom_config(tmp_path, fakebin):
    cfg = json.loads((KIT / "resources.json").read_text())
    cfg["throttle"]["live_terminals_gte"] = 2
    p = write_json(tmp_path / "res.json", cfg)
    assert gate(tmp_path, fakebin, m(live_terminals=2), "--config", p)[0] == THROTTLE


# --- broken CAO API: THROTTLE with a reason, never a crash ---

def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Srv:
    def __init__(self, handler):
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def handler(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = routes.get(self.path, (404, "{}"))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format, *args):
            pass
    return H


def no_terms():
    d = m()
    del d["live_terminals"]
    return d


def test_cao_api_refused_throttles(tmp_path, fakebin):
    rc, out = gate(tmp_path, fakebin, no_terms(), cao_url=f"http://127.0.0.1:{closed_port()}")
    assert rc == THROTTLE and "cao_api" in out


def test_cao_api_500_throttles(tmp_path, fakebin):
    with _Srv(handler({"/sessions": (500, "boom")})) as url:
        rc, out = gate(tmp_path, fakebin, no_terms(), cao_url=url)
    assert rc == THROTTLE and "cao_api" in out


def test_cao_api_garbage_throttles(tmp_path, fakebin):
    with _Srv(handler({"/sessions": (200, "<html>")})) as url:
        rc, out = gate(tmp_path, fakebin, no_terms(), cao_url=url)
    assert rc == THROTTLE and "cao_api" in out


def test_cao_api_counts_terminals_across_sessions(tmp_path, fakebin):
    routes = {
        "/sessions": (200, json.dumps([{"name": "s1"}, {"name": "s2"}])),
        "/sessions/s1/terminals": (200, json.dumps([{"id": "a"}, {"id": "b"}])),
        "/sessions/s2/terminals": (200, json.dumps([{"id": "c"}, {"id": "d"}, {"id": "e"}])),
    }
    with _Srv(handler(routes)) as url:
        rc, out = gate(tmp_path, fakebin, no_terms(), "--json", cao_url=url)
    data = json.loads(out)
    assert data["measurements"]["live_terminals"] == 5 and rc == THROTTLE


def test_cao_api_counts_three_as_go(tmp_path, fakebin):
    routes = {"/sessions": (200, json.dumps([{"name": "s1"}])),
              "/sessions/s1/terminals": (200, json.dumps([{"id": "a"}, {"id": "b"}, {"id": "c"}]))}
    with _Srv(handler(routes)) as url:
        assert gate(tmp_path, fakebin, no_terms(), cao_url=url) == (GO, "GO")


# --- invalid measurements fail closed ---

@pytest.mark.parametrize("key,val", [
    ("pressure_level", 0), ("pressure_level", -1), ("pressure_level", 3), ("pressure_level", 5),
    ("pressure_level", "1"), ("pressure_level", True), ("pressure_level", 1.0),
    ("free_memory_pct", float("nan")), ("free_memory_pct", float("inf")), ("free_memory_pct", -1),
    ("free_memory_pct", 101), ("free_memory_pct", "60"),
    ("swap_used_gib", float("nan")), ("swap_used_gib", -0.5), ("swap_used_gib", float("inf")),
    ("disk_free_gib", float("nan")), ("disk_free_gib", -5), ("disk_free_gib", float("inf")),
    ("load1", float("nan")), ("load1", -1), ("ncpu", 0), ("ncpu", 2.5), ("ncpu", -4),
    ("live_terminals", -1), ("live_terminals", 1.5), ("live_terminals", True),
])
def test_invalid_measurement_throttles_never_go(tmp_path, fakebin, key, val):
    rc, out = gate(tmp_path, fakebin, m(**{key: val}))
    assert rc == THROTTLE and out.startswith("THROTTLE"), out
    assert ("unknown:" if key == "pressure_level" else "invalid:") in out, out


@pytest.mark.parametrize("val", [1, 2, 4])
def test_supported_pressure_levels_are_measurements(tmp_path, fakebin, val):
    rc, out = gate(tmp_path, fakebin, m(pressure_level=val))
    assert "unknown" not in out and rc == {1: GO, 2: THROTTLE, 4: STOP}[val], out


def test_invalid_pressure_does_not_hide_a_real_stop(tmp_path, fakebin):
    rc, out = gate(tmp_path, fakebin, m(pressure_level=0, disk_free_gib=5))
    assert rc == STOP and "disk" in out


# --- live parsers on recorded raw outputs: no real probe, no fake measurement file ---

MP_NORMAL = ("The system has 34359738368 (8388608 pages with a page size of 4096).\n\n"
             "Stats:\nPageins: 162628211 \nPageouts: 3259874 \n\n"
             "System-wide memory free percentage: 47%\n")
SWAP_RAW = "total = 19456.00M  used = 18386.19M  free = 1069.81M  (encrypted)\n"


@pytest.fixture
def probebin(tmp_path):
    """Recorded-output stand-ins for sysctl and memory_pressure. Returns a builder."""
    def build(pressure="1\n", swap=SWAP_RAW, mp=MP_NORMAL, omit=(), sleep=None):
        d = tmp_path / "probebin"
        d.mkdir(exist_ok=True)
        (d / "pressure.out").write_text(pressure)
        (d / "swap.out").write_text(swap)
        (d / "mp.out").write_text(mp)
        nap = f"sleep {sleep}\n" if sleep else ""
        scripts = {
            "sysctl": (f"#!/bin/sh\n{nap}case \"$2\" in\n"
                       f"  kern.memorystatus_vm_pressure_level) cat '{d}/pressure.out' ;;\n"
                       f"  vm.swapusage) cat '{d}/swap.out' ;;\n  *) exit 1 ;;\nesac\n"),
            "memory_pressure": f"#!/bin/sh\n{nap}cat '{d}/mp.out'\n",
        }
        for name, body in scripts.items():
            if name in omit:
                continue
            (d / name).write_text(body)
            (d / name).chmod(0o755)
        return d
    return build


def live(tmp_path, d, timeout=5):
    cfg = json.loads((KIT / "resources.json").read_text())
    cfg["disk_path"] = str(tmp_path)
    cfg["probe_timeouts"] = {"sysctl": timeout, "memory_pressure": timeout}
    conf = write_json(tmp_path / "live-res.json", cfg)
    env = base_env(tmp_path, SUPER_AGENT_PROBE_PATH=d,
                   SUPER_AGENT_CAO_URL=f"http://127.0.0.1:{closed_port()}")
    env["PATH"] = f"{d}:{env['PATH']}"
    r = run("resource_gate.py", "--json", "--config", conf, env=env)
    data = json.loads(r.stdout)
    return r.returncode, data, " ".join(data["reasons"])


@pytest.mark.parametrize("raw,level", [("1\n", 1), ("2\n", 2), ("4\n", 4)])
def test_pressure_parser_reads_the_tool(tmp_path, probebin, raw, level):
    rc, data, why = live(tmp_path, probebin(pressure=raw))
    assert data["measurements"]["pressure_level"] == level
    assert ("pressure=" in why) == (level > 1) and "unknown:pressure" not in why
    if level == 4:
        assert rc == STOP


@pytest.mark.parametrize("raw", ["", "\n", "garbage\n", "1 2\n", "2.0\n", "+2\n", "4_0\n"])
def test_pressure_parser_rejects_bad_output(tmp_path, probebin, raw):
    rc, data, why = live(tmp_path, probebin(pressure=raw))
    assert data["measurements"]["pressure_level"] is None and "unmeasured:pressure" in why
    assert rc == THROTTLE


def test_pressure_tool_reporting_zero_is_unknown(tmp_path, probebin):
    rc, data, why = live(tmp_path, probebin(pressure="0\n"))
    assert data["measurements"]["pressure_level"] == 0 and "unknown:pressure=0" in why
    assert rc == THROTTLE


def test_missing_tools_are_unmeasured(tmp_path, probebin):
    rc, data, why = live(tmp_path, probebin(omit=("sysctl", "memory_pressure")))
    for k in ("pressure_level", "swap_used_gib", "free_memory_pct"):
        assert data["measurements"][k] is None
    assert "unmeasured:pressure" in why and "unmeasured:swap_gib" in why and rc == THROTTLE


def test_probe_timeout_is_unmeasured(tmp_path, probebin):
    rc, data, why = live(tmp_path, probebin(sleep=3), timeout=0.3)
    assert data["measurements"]["pressure_level"] is None and "unmeasured:pressure" in why
    assert data["measurements"]["free_memory_pct"] is None and rc == THROTTLE


def test_swap_and_free_parsers_read_the_tools(tmp_path, probebin):
    rc, data, why = live(tmp_path, probebin(pressure="2\n"))
    meas = data["measurements"]
    assert meas["free_memory_pct"] == 47 and abs(meas["swap_used_gib"] - 18386.19 / 1024) < 1e-6
    assert "swap_gib=18.0>16" in why


@pytest.mark.parametrize("swap,gib", [("total = 2048.00M  used = 512.00M  free = 1.0M\n", 0.5),
                                      ("total = 2.00G  used = 1.50G  free = 0.5G\n", 1.5),
                                      ("total = 0.00K  used = 0.00K  free = 0.00K\n", 0.0)])
def test_swap_units(tmp_path, probebin, swap, gib):
    rc, data, why = live(tmp_path, probebin(swap=swap))
    assert abs(data["measurements"]["swap_used_gib"] - gib) < 1e-9


@pytest.mark.parametrize("swap,mp", [("garbage\n", "garbage\n"), ("", "")])
def test_swap_and_free_garbage_is_unmeasured(tmp_path, probebin, swap, mp):
    rc, data, why = live(tmp_path, probebin(swap=swap, mp=mp))
    assert data["measurements"]["swap_used_gib"] is None and data["measurements"]["free_memory_pct"] is None
    assert "unmeasured:swap_gib" in why and "unmeasured:free_mem_pct" in why and rc == THROTTLE


# --- heavy-job marker: token-owned, serialised by flock ---

TOKEN_RE = re.compile(r"token=([0-9a-f]{32})\b")


def heavy(tmp_path, fakebin, meas, *args):
    return gate(tmp_path, fakebin, meas, *args)


def acquire_ok(tmp_path, fakebin, label, meas=None):
    rc, out = heavy(tmp_path, fakebin, meas or m(), "--acquire-heavy", label)
    assert rc == 0 and out.startswith(f"ACQUIRED {label} "), out
    return TOKEN_RE.search(out).group(1)


def owner_file(tmp_path):
    return tmp_path / "state" / "heavy" / "active" / "owner.json"


def test_acquire_and_release(tmp_path, fakebin):
    tok = acquire_ok(tmp_path, fakebin, "suite-101")
    marker = tmp_path / "state" / "heavy" / "active"
    data = json.loads((marker / "owner.json").read_text())
    assert marker.is_dir() and data["label"] == "suite-101" and data["token"] == tok
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", tok)
    assert rc == 0 and out.startswith("RELEASED suite-101") and not marker.exists()


def test_tokens_are_unique_per_acquisition(tmp_path, fakebin):
    t1 = acquire_ok(tmp_path, fakebin, "a")
    assert heavy(tmp_path, fakebin, m(), "--release-heavy", t1)[0] == 0
    t2 = acquire_ok(tmp_path, fakebin, "a")
    assert t1 != t2


def test_acquire_on_throttle_when_free(tmp_path, fakebin):
    assert heavy(tmp_path, fakebin, m(swap_used_gib=20), "--acquire-heavy", "a")[0] == 0


def test_acquire_refused_on_stop(tmp_path, fakebin):
    rc, out = heavy(tmp_path, fakebin, m(disk_free_gib=5), "--acquire-heavy", "a")
    assert rc == 1 and out.startswith("REFUSED STOP")
    assert not (tmp_path / "state" / "heavy" / "active").exists()


def test_second_heavy_refused(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "a")
    rc, out = heavy(tmp_path, fakebin, m(), "--acquire-heavy", "b")
    assert rc == 1 and out.startswith("REFUSED BUSY") and " a " in f" {out} "


def test_same_label_twice_refused(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "a")
    assert heavy(tmp_path, fakebin, m(), "--acquire-heavy", "a")[0] == 1


def age_marker(tmp_path, acquired_ago, refreshed_ago):
    data = json.loads(owner_file(tmp_path).read_text())
    data["acquired_at"] = time.time() - acquired_ago
    data["refreshed_at"] = time.time() - refreshed_ago
    owner_file(tmp_path).write_text(json.dumps(data))


def test_stale_marker_reported_not_taken(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "old")
    age_marker(tmp_path, 3 * 3600 + 60, 3 * 3600 + 60)
    rc, out = heavy(tmp_path, fakebin, m(), "--acquire-heavy", "new")
    assert rc == 1 and out.startswith("REFUSED STALE") and "old" in out
    assert json.loads(owner_file(tmp_path).read_text())["label"] == "old"


def test_fresh_marker_under_3h_is_busy_not_stale(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "old")
    age_marker(tmp_path, 3 * 3600 - 120, 3 * 3600 - 120)
    assert heavy(tmp_path, fakebin, m(), "--acquire-heavy", "new")[1].startswith("REFUSED BUSY")


def test_staleness_counts_from_the_last_refresh(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "long")
    age_marker(tmp_path, 5 * 3600, 600)
    assert heavy(tmp_path, fakebin, m(), "--acquire-heavy", "new")[1].startswith("REFUSED BUSY")


def test_stale_marker_without_owner_file_uses_mtime(tmp_path, fakebin):
    d = tmp_path / "state" / "heavy" / "active"
    d.mkdir(parents=True)
    old = time.time() - 4 * 3600
    os.utime(d, (old, old))
    assert heavy(tmp_path, fakebin, m(), "--acquire-heavy", "new")[1].startswith("REFUSED STALE")


def test_refresh_extends_the_lease(tmp_path, fakebin):
    tok = acquire_ok(tmp_path, fakebin, "run")
    age_marker(tmp_path, 2 * 3600 + 3000, 2 * 3600 + 3000)
    rc, out = heavy(tmp_path, fakebin, m(), "--refresh-heavy", tok)
    assert rc == 0 and out.startswith("REFRESHED run"), out
    data = json.loads(owner_file(tmp_path).read_text())
    assert time.time() - data["refreshed_at"] < 60 and data["token"] == tok
    age_marker_keep = data["acquired_at"]
    assert time.time() - age_marker_keep > 2 * 3600


def test_refresh_needs_the_token(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "run")
    rc, out = heavy(tmp_path, fakebin, m(), "--refresh-heavy", "0" * 32)
    assert rc == 1 and out.startswith("REFUSED not_owner")


def test_refresh_of_a_stale_lease_is_refused(tmp_path, fakebin):
    tok = acquire_ok(tmp_path, fakebin, "run")
    age_marker(tmp_path, 4 * 3600, 4 * 3600)
    rc, out = heavy(tmp_path, fakebin, m(), "--refresh-heavy", tok)
    assert rc == 1 and out.startswith("REFUSED STALE")


def test_refresh_when_not_held(tmp_path, fakebin):
    assert heavy(tmp_path, fakebin, m(), "--refresh-heavy", "1" * 32) == (1, "NOT_HELD")


def test_release_by_a_foreign_owner_is_refused(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "a")
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", "f" * 32)
    assert rc == 1 and out.startswith("REFUSED not_owner held_by=a")
    assert (tmp_path / "state" / "heavy" / "active").exists()


def test_release_by_label_is_no_longer_accepted(tmp_path, fakebin):
    acquire_ok(tmp_path, fakebin, "a")
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", "a")
    assert rc == 2 and out.startswith("ERROR bad token")
    assert (tmp_path / "state" / "heavy" / "active").exists()


def test_delayed_release_cannot_free_the_next_holder(tmp_path, fakebin):
    """The review's interleaving: A releases, B acquires, A's delayed release runs again, C tries."""
    tok_a = acquire_ok(tmp_path, fakebin, "A")
    assert heavy(tmp_path, fakebin, m(), "--release-heavy", tok_a)[0] == 0
    acquire_ok(tmp_path, fakebin, "B")
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", tok_a)
    assert rc == 1 and out.startswith("REFUSED not_owner held_by=B")
    rc, out = heavy(tmp_path, fakebin, m(), "--acquire-heavy", "C")
    assert rc == 1 and out.startswith("REFUSED BUSY B")


def hold_lock(tmp_path):
    import fcntl
    (tmp_path / "state").mkdir(exist_ok=True)
    fd = os.open(tmp_path / "state" / "heavy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def fast_lock_config(tmp_path):
    cfg = json.loads((KIT / "resources.json").read_text())
    cfg["heavy"]["lock_timeout_s"] = 0.3
    return write_json(tmp_path / "fastlock.json", cfg)


def test_acquire_waits_for_the_lifecycle_lock(tmp_path, fakebin):
    conf = fast_lock_config(tmp_path)
    fd = hold_lock(tmp_path)
    try:
        rc, out = heavy(tmp_path, fakebin, m(), "--config", conf, "--acquire-heavy", "a")
    finally:
        os.close(fd)
    assert rc == 1 and out.startswith("REFUSED LOCKED"), out
    assert not (tmp_path / "state" / "heavy" / "active").exists()


@pytest.mark.parametrize("op", ["--release-heavy", "--refresh-heavy"])
def test_release_and_refresh_wait_for_the_lifecycle_lock(tmp_path, fakebin, op):
    tok = acquire_ok(tmp_path, fakebin, "a")
    before = owner_file(tmp_path).read_text()
    conf = fast_lock_config(tmp_path)
    fd = hold_lock(tmp_path)
    try:
        rc, out = heavy(tmp_path, fakebin, m(), "--config", conf, op, tok)
    finally:
        os.close(fd)
    assert rc == 1 and out.startswith("REFUSED LOCKED"), out
    assert owner_file(tmp_path).read_text() == before


def test_release_when_not_held(tmp_path, fakebin):
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", "a" * 32)
    assert rc == 1 and out.startswith("NOT_HELD")


def test_release_works_even_on_stop(tmp_path, fakebin):
    tok = acquire_ok(tmp_path, fakebin, "a")
    assert heavy(tmp_path, fakebin, m(pressure_level=4), "--release-heavy", tok)[0] == 0


@pytest.mark.parametrize("label", ["../x", "a b", "", "x/y"])
def test_bad_label_rejected(tmp_path, fakebin, label):
    rc, out = heavy(tmp_path, fakebin, m(), "--acquire-heavy", label)
    assert rc == 2 and out.startswith("ERROR bad label")
    assert not (tmp_path / "state" / "heavy").exists()


@pytest.mark.parametrize("tok", ["", "abc", "A" * 32, "g" * 32, "a" * 33])
def test_bad_token_rejected(tmp_path, fakebin, tok):
    rc, out = heavy(tmp_path, fakebin, m(), "--release-heavy", tok)
    assert rc == 2 and out.startswith("ERROR bad token")


# --- strictness sweep: a session the gate can't name is not "no terminals" ---

@pytest.mark.parametrize("bad", [{}, {"name": ""}, {"name": None, "id": None}, {"name": 5}, {"name": " "}, "s9"],
                         ids=["empty", "name_empty", "both_null", "name_int", "name_blank", "not_object"])
def test_sweep_session_without_a_name_throttles(tmp_path, fakebin, bad):
    """Pre-fix such a session was asked for /sessions/None/terminals (or /sessions/5/...); an API
    answering [] there made its terminals count as zero -> GO."""
    routes = {"/sessions": (200, json.dumps([{"name": "s1"}, bad])),
              "/sessions/s1/terminals": (200, json.dumps([{"id": "a"}])),
              "/sessions/None/terminals": (200, "[]"), "/sessions/5/terminals": (200, "[]"),
              "/sessions/s9/terminals": (200, "[]"), "/sessions/%20/terminals": (200, "[]")}
    with _Srv(handler(routes)) as url:
        rc, out = gate(tmp_path, fakebin, no_terms(), cao_url=url)
    assert rc == THROTTLE and "cao_api:bad_shape" in out, out


def test_sweep_session_named_by_id_still_counts(tmp_path, fakebin):
    routes = {"/sessions": (200, json.dumps([{"name": "s1"}, {"id": "s2"}])),
              "/sessions/s1/terminals": (200, json.dumps([{"id": "a"}])),
              "/sessions/s2/terminals": (200, json.dumps([{"id": "b"}]))}
    with _Srv(handler(routes)) as url:
        rc, out = gate(tmp_path, fakebin, no_terms(), "--json", cao_url=url)
    assert json.loads(out)["measurements"]["live_terminals"] == 2 and rc == GO, out
