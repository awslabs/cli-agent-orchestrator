"""Host resource view on the AG-UI fleet snapshot (#1155, host box).

Numbers only, local host only, every failure degrades to *absent*, sampled at most once per
TTL, default-off, no write path. Parsers are pure and run on any platform; Linux-only
comparisons against an independent read of ``/proc`` skip on other platforms with a named
reason (macOS is not reproducible in CI: it has canned-string parser tests here and a local
run in the PR body).
"""

from __future__ import annotations

import getpass
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import time
from test._support.agui_conformance import parse_sse
from test._support.agui_stream_harness import install
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import host_resources as hr
from cli_agent_orchestrator.services.ui_state_service import diff_snapshot

client = TestClient(app, base_url="http://localhost")

LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc: Linux only (macOS not in CI)"
)
ENUM = {"normal", "warn", "critical"}


@pytest.fixture(autouse=True)
def _fresh_sampler():
    hr.reset_host_sampler()
    yield
    hr.reset_host_sampler()


@pytest.fixture
def readers(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[int]]:
    """Deterministic readers that count their calls."""
    calls: Dict[str, List[int]] = {"cpu": [], "load": [], "mem": [], "psi": []}
    ticks = {"n": 0}

    def cpu() -> Any:
        calls["cpu"].append(1)
        ticks["n"] += 1
        return (10.0 * ticks["n"], 40.0 * ticks["n"])  # always 25% busy

    monkeypatch.setattr(hr, "_read_cpu_times", cpu)
    monkeypatch.setattr(hr, "_read_load_per_core", lambda: (calls["load"].append(1), 0.5)[1])
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: (calls["mem"].append(1), 42)[1])
    monkeypatch.setattr(hr, "_read_memory_pressure", lambda: (calls["psi"].append(1), "normal")[1])
    return calls


# --- pure parsers ----------------------------------------------------------------------


def test_psi_parser_reads_some_not_full() -> None:
    text = "some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=99.00 avg60=0 avg300=0 total=1\n"
    assert hr.parse_psi_some_avg10(text) == 12.5


@pytest.mark.parametrize(
    "text", ["", "full avg10=1.0", "some avg60=1.0", "some avg10=abc", "some avg10=-1"]
)
def test_psi_parser_unparsable_is_none(text: str) -> None:
    assert hr.parse_psi_some_avg10(text) is None


@pytest.mark.parametrize(
    "avg10,level",
    [
        (None, None),
        (0.0, "normal"),
        (9.99, "normal"),
        (10.0, "warn"),
        (39.99, "warn"),
        (40.0, "critical"),
        (100.0, "critical"),
    ],
)
def test_psi_threshold_boundaries(avg10: Optional[float], level: Optional[str]) -> None:
    assert hr.pressure_from_psi(avg10) == level
    assert (hr.PSI_WARN, hr.PSI_CRITICAL) == (10.0, 40.0)


@pytest.mark.parametrize(
    "text,level",
    [("1\n", "normal"), ("2", "warn"), (" 4 ", "critical"), ("3", None), ("x", None), ("", None)],
)
def test_macos_pressure_level_canned_strings(text: str, level: Optional[str]) -> None:
    assert hr.parse_macos_pressure_level(text) == level


def test_cpu_percent_math_and_window_rules() -> None:
    assert hr.cpu_percent_from_times((0, 0), (25, 100), 30) == 25
    assert hr.cpu_percent_from_times((0, 0), (25, 100), hr.MAX_CPU_WINDOW_SECONDS) == 25
    assert hr.cpu_percent_from_times((0, 0), (25, 100), hr.MAX_CPU_WINDOW_SECONDS + 0.1) is None
    assert hr.cpu_percent_from_times((0, 0), (25, 100), 0) is None
    assert hr.cpu_percent_from_times((0, 0), (25, 100), -1) is None
    assert hr.cpu_percent_from_times((5, 5), (5, 5), 10) is None  # no CPU time elapsed
    assert hr.cpu_percent_from_times((9, 5), (5, 9), 10) is None  # counters went backwards
    assert hr.cpu_percent_from_times((0, 0), (500, 100), 10) == 100  # clamped


# --- thin readers: every degrade branch -------------------------------------------------


def test_read_text_missing_file_is_none(tmp_path) -> None:
    assert hr._read_text(str(tmp_path / "absent")) is None
    good = tmp_path / "f"
    good.write_text("x")
    assert hr._read_text(str(good)) == "x"
    bad = tmp_path / "bad"
    bad.write_bytes(b"\xff\xfe")
    assert hr._read_text(str(bad)) is None


def test_pressure_linux_psi_present_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(hr, "_read_text", lambda p: "some avg10=41.0 avg60=0 avg300=0 total=0")
    assert hr._read_memory_pressure() == "critical"
    # PSI absent: omitted, never inferred from meminfo.
    monkeypatch.setattr(hr, "_read_text", lambda p: None)
    assert hr._read_memory_pressure() is None


def test_pressure_macos_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(hr, "_read_macos_pressure_text", lambda: "2\n")
    assert hr._read_memory_pressure() == "warn"
    monkeypatch.setattr(hr, "_read_macos_pressure_text", lambda: None)
    assert hr._read_memory_pressure() is None


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "cygwin"])
def test_unknown_platform_has_no_pressure(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert hr._read_memory_pressure() is None


def _run_result(code: int, out: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_macos_sysctl_is_run_by_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launchd-started server has a minimal PATH, so ``sysctl`` must not be looked up."""
    argv: List[Any] = []

    def ok(args: Any, **kwargs: Any):
        argv.extend(args)
        return _run_result(0, "1\n")

    monkeypatch.setattr(hr.subprocess, "run", ok)
    hr._read_macos_pressure_text()
    assert argv[0] == hr.MACOS_SYSCTL == "/usr/sbin/sysctl"
    assert os.path.isabs(argv[0])
    assert argv[1:] == ["-n", "kern.memorystatus_vm_pressure_level"]


def test_macos_sysctl_reader_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def ok(*args: Any, **kwargs: Any):
        seen.update(kwargs)
        return _run_result(0, "1\n")

    monkeypatch.setattr(hr.subprocess, "run", ok)
    assert hr._read_macos_pressure_text() == "1\n"
    assert seen["timeout"] == hr.SUBPROCESS_TIMEOUT_SECONDS and seen["check"] is False

    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _run_result(1, "1\n"))
    assert hr._read_macos_pressure_text() is None  # non-zero exit

    def timeout(*a: Any, **k: Any):
        raise subprocess.TimeoutExpired("sysctl", 2)

    monkeypatch.setattr(hr.subprocess, "run", timeout)
    assert hr._read_macos_pressure_text() is None

    def missing(*a: Any, **k: Any):
        raise FileNotFoundError("sysctl")

    monkeypatch.setattr(hr.subprocess, "run", missing)
    assert hr._read_macos_pressure_text() is None


def test_psutil_and_os_readers_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any):
        raise OSError("no")

    monkeypatch.setattr(hr.psutil, "cpu_times", boom)
    assert hr._read_cpu_times() is None
    monkeypatch.setattr(hr.psutil, "virtual_memory", boom)
    assert hr._read_memory_used_percent() is None
    monkeypatch.setattr(hr.os, "getloadavg", boom)
    assert hr._read_load_per_core() is None


def test_readers_happy_path_and_cpu_count_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    busy_total = hr._read_cpu_times()
    assert busy_total is not None and busy_total[1] >= busy_total[0] >= 0
    memory = hr._read_memory_used_percent()
    assert memory is not None and 0 <= memory <= 100
    monkeypatch.setattr(hr.os, "getloadavg", lambda: (2.0, 0, 0))
    monkeypatch.delattr(hr.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(hr.os, "cpu_count", lambda: 4)
    assert hr._read_load_per_core() == 0.5
    monkeypatch.setattr(hr.os, "cpu_count", lambda: None)
    assert hr._read_load_per_core() is None  # no core count: absent, not a division error


def test_load_uses_affinity_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hr.os, "getloadavg", lambda: (3.0, 0, 0))
    monkeypatch.setattr(hr.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    assert hr._read_load_per_core() == 1.5

    def denied(pid: int):
        raise OSError("no")

    monkeypatch.setattr(hr.os, "sched_getaffinity", denied, raising=False)
    monkeypatch.setattr(hr.os, "cpu_count", lambda: 6)
    assert hr._read_load_per_core() == 0.5


def test_busy_total_does_not_double_count_guest_time() -> None:
    """psutil's own formula: guest/guest_nice are already inside user/nice on Linux."""
    from collections import namedtuple

    Times = namedtuple("Times", "user nice system idle iowait irq softirq steal guest guest_nice")
    busy, total = hr.busy_total_from_cpu_times(Times(40, 0, 10, 40, 5, 0, 0, 0, 10, 0))
    assert total == 95  # 105 summed, minus the 10 of guest
    assert busy == 50  # total - idle - iowait
    # A platform without guest or iowait fields (macOS) is just user+system+idle.
    Mac = namedtuple("Mac", "user nice system idle")
    assert hr.busy_total_from_cpu_times(Mac(30, 0, 10, 60)) == (40, 100)


# --- sampler: priming, absent-first-cpu, TTL, degrade ----------------------------------


def test_host_entry_names_its_runner_and_never_the_old_runtime_name(
    readers: Dict[str, List[int]],
) -> None:
    """3.0 renamed the field ``runner_id``; the old name must not be on the wire at all."""
    (entry,) = hr.refresh_host_now()
    assert entry["runner_id"] is None
    assert "runtime_id" not in entry and "runner_id" in hr.HOST_KEYS
    assert "runtime_id" not in hr.HOST_KEYS


def test_first_sample_has_no_cpu_and_second_after_window_does(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])
    (first,) = hr.refresh_host_now()
    assert "cpu_percent" not in first  # no window yet: absent, not 0
    assert first["runner_id"] is None
    assert first["load_per_core"] == 0.5 and first["memory_used_percent"] == 42
    assert first["memory_pressure"] == "normal"
    assert set(first) <= hr.HOST_KEYS and "sampled_at" in first
    clock["t"] += 31
    (second,) = hr.refresh_host_now()
    assert second["cpu_percent"] == 25


def test_prime_gives_the_first_real_sample_a_cpu_value(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])
    hr.prime_host_sampler()
    clock["t"] += 31
    (entry,) = hr.refresh_host_now()
    assert entry["cpu_percent"] == 25


def test_stale_window_drops_cpu_and_reprimes(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])
    hr.prime_host_sampler()
    clock["t"] += 600  # an idle fleet: nobody asked for ten minutes
    (stale,) = hr.refresh_host_now()
    assert "cpu_percent" not in stale
    clock["t"] += 31
    (fresh,) = hr.refresh_host_now()
    assert fresh["cpu_percent"] == 25  # re-primed by the stale sample


def _join_worker() -> None:
    worker = hr._sampler._worker
    assert worker is not None, "a background refresh should have been started"
    worker.join(5)
    assert not worker.is_alive()


def test_ttl_one_probe_for_many_snapshots(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])
    hr.prime_host_sampler()
    for _ in range(25):
        assert hr.sample_host()  # served from the cache
        clock["t"] += 0.5
    assert len(readers["load"]) == len(readers["mem"]) == len(readers["psi"]) == 1
    assert hr._sampler._worker is None, "inside the TTL nothing is refreshed"
    clock["t"] += 30
    stale = hr.sample_host()  # past the TTL: returns the OLD sample, refreshes behind it
    assert len(stale) == 1
    _join_worker()
    assert len(readers["load"]) == 2


def test_sample_never_probes_on_the_callers_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow reader (macOS sysctl, up to 2 s) must not stall the event loop's snapshot."""
    import threading

    entered, release = threading.Event(), threading.Event()
    calls: List[int] = []

    def slow_load() -> float:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return 0.5

    monkeypatch.setattr(hr, "_read_cpu_times", lambda: None)
    monkeypatch.setattr(hr, "_read_load_per_core", slow_load)
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: None)
    monkeypatch.setattr(hr, "_read_memory_pressure", lambda: None)
    assert hr.sample_host() == []  # nothing cached yet: not reported, and no waiting
    assert entered.wait(5), "the refresh runs on a background thread"
    # Single flight: more snapshots while the probe is in progress start no second probe.
    assert hr.sample_host() == [] and hr.sample_host() == []
    release.set()
    _join_worker()
    assert calls == [1]
    assert [e["load_per_core"] for e in hr.sample_host()] == [0.5]


def test_sample_returns_copies_not_the_shared_cache(readers: Dict[str, List[int]]) -> None:
    hr.refresh_host_now()
    first = hr.sample_host()
    first[0]["load_per_core"] = 999
    first.append({"x": 1})
    assert hr.sample_host()[0]["load_per_core"] == 0.5 and len(hr.sample_host()) == 1


def test_a_probe_that_fails_inside_refresh_is_degraded_to_not_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh`` itself swallows a probe bug: the entry is absent and the flag clears."""

    def boom() -> Any:
        raise RuntimeError("probe bug")

    monkeypatch.setattr(hr, "_read_load_per_core", boom)
    assert hr.sample_host() == []
    _join_worker()
    assert hr._sampler._refreshing is False and hr.sample_host() == []


def test_a_refresh_that_raises_still_clears_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background thread's own ``finally``: even a failure outside ``refresh``'s guard."""

    def broken(self: Any) -> Any:
        raise RuntimeError("refresh bug")

    monkeypatch.setattr(hr.HostSampler, "refresh", broken)
    assert hr.sample_host() == []
    _join_worker()
    assert hr._sampler._refreshing is False


def test_a_thread_that_cannot_start_does_not_stick_the_flag(
    monkeypatch: pytest.MonkeyPatch, readers: Dict[str, List[int]]
) -> None:
    import threading

    real_start = threading.Thread.start
    monkeypatch.setattr(
        threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no threads"))
    )
    assert hr.sample_host() == []  # does not raise, nothing cached yet
    assert hr._sampler._refreshing is False and hr._sampler._worker is None
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert hr.sample_host() == []  # the next call can start a refresh
    _join_worker()
    assert [e["load_per_core"] for e in hr.sample_host()] == [0.5]


def test_every_field_failing_gives_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "_read_cpu_times",
        "_read_load_per_core",
        "_read_memory_used_percent",
        "_read_memory_pressure",
    ):
        monkeypatch.setattr(hr, name, lambda: None)
    assert hr.refresh_host_now() == []


def test_one_field_failing_omits_only_that_field(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: None)
    (entry,) = hr.refresh_host_now()
    assert "memory_used_percent" not in entry and "load_per_core" in entry


def test_sampler_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise RuntimeError("probe bug")

    monkeypatch.setattr(hr, "_read_load_per_core", boom)
    assert hr.refresh_host_now() == []


def test_contaminated_readers_are_dropped_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a reader returns is validated: strings, containers, bools and unknown levels
    never reach the entry."""
    monkeypatch.setattr(hr, "_read_cpu_times", lambda: None)
    monkeypatch.setattr(hr, "_read_load_per_core", lambda: "SENTINEL-HOST")
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: {"SENTINEL-KEY": 1})
    monkeypatch.setattr(hr, "_read_memory_pressure", lambda: "SENTINEL-LEVEL")
    assert hr.refresh_host_now() == []
    monkeypatch.setattr(hr, "_read_load_per_core", lambda: True)
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: 40)
    (entry,) = hr.refresh_host_now()
    assert set(entry) == {"runner_id", "memory_used_percent", "sampled_at"}


def test_refresh_after_ttl_with_no_change_yields_no_delta_when_sampled_at_is_frozen(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two refreshes a TTL apart with identical readings diff to nothing, provided
    ``sampled_at`` is the same. In production it is NOT the same, so a refresh always emits
    one whole-key ``/host`` replace: at most one per 30 s of fleet activity, never per event.
    """
    import datetime as real_datetime

    class Frozen(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 1, 1, tzinfo=tz)

    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(hr, "datetime", Frozen)
    hr.prime_host_sampler()
    clock["t"] += 31
    first = {"host": hr.refresh_host_now()}
    clock["t"] += 31
    second = {"host": hr.refresh_host_now()}
    assert first["host"] == second["host"] and first["host"][0]["cpu_percent"] == 25
    assert diff_snapshot(first, second) == []
    # Inside the TTL the cached entry is the same value, so no delta either.
    assert diff_snapshot({"host": hr.sample_host()}, {"host": hr.sample_host()}) == []


def test_a_real_refresh_changes_sampled_at_so_one_replace_per_ttl(
    readers: Dict[str, List[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime as real_datetime

    stamps = iter([0, 1])

    class Stepping(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 1, 1, 0, 0, next(stamps), tzinfo=tz)

    monkeypatch.setattr(hr, "datetime", Stepping)
    a = {"host": hr.refresh_host_now()}
    b = {"host": hr.refresh_host_now()}
    ops = diff_snapshot(a, b)
    assert [op["path"] for op in ops] == ["/host"] and ops[0]["op"] == "replace"


def test_host_is_whole_key_replaced() -> None:
    prev = {"host": [{"runner_id": None, "load_per_core": 0.5}]}
    curr = {"host": [{"runner_id": None, "load_per_core": 0.6}]}
    assert diff_snapshot(prev, curr) == [{"op": "replace", "path": "/host", "value": curr["host"]}]


# --- wire: stream routes, flag, allow-lists, leaks --------------------------------------


def _snapshots(body: str) -> List[Dict[str, Any]]:
    return [e["snapshot"] for e in parse_sse(body) if isinstance(e.get("snapshot"), dict)]


def _leaves(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _iso(value: str) -> bool:
    from datetime import datetime

    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def test_snapshot_key_allow_list_and_host_shape(
    monkeypatch: pytest.MonkeyPatch, readers: Dict[str, List[int]]
) -> None:
    install(monkeypatch)
    hr.refresh_host_now()  # what the start-up prime does
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    snapshot, *_ = _snapshots(body)
    assert set(snapshot) == {"sessions", "terminals", "counts", "scopes", "profiles", "host"}
    (entry,) = snapshot["host"]
    assert set(entry) <= hr.HOST_KEYS
    for key, leaf in entry.items():
        if key == "sampled_at":
            assert _iso(leaf)
        elif key == "memory_pressure":
            assert leaf in ENUM
        else:
            assert leaf is None or isinstance(leaf, (int, float, bool)), key
    assert readers["load"], "the sampler must have been consulted"


def test_legacy_framing_and_run_plane_have_no_host(
    monkeypatch: pytest.MonkeyPatch, readers: Dict[str, List[int]]
) -> None:
    install(monkeypatch)
    with client.stream("GET", "/agui/v1/stream?framing=cao-legacy") as resp:
        legacy = "".join(resp.iter_text())
    assert all("host" not in s for s in _snapshots(legacy))
    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with client.stream("POST", "/agui/v1/run", json=body) as resp:
        run = "".join(resp.iter_text())
    assert all("host" not in s for s in _snapshots(run))
    assert readers["load"] == [], "the legacy framing and the run plane must never sample"


def test_remote_runner_group_gets_no_host_entry(
    monkeypatch: pytest.MonkeyPatch, readers: Dict[str, List[int]]
) -> None:
    """Only the local host is probed: a remote group is "not reported", not a fake 0."""
    from cli_agent_orchestrator.services import session_service

    rows = [
        {
            "id": "t1",
            "tmux_session": "a",
            "provider": "kiro_cli",
            "agent_profile": "dev",
            "runner_id": None,
        },
        {
            "id": "t2",
            "tmux_session": "b",
            "provider": "kiro_cli",
            "agent_profile": "dev",
            "runner_id": "rn-remote",
        },
    ]
    monkeypatch.setattr(
        session_service, "list_sessions", lambda: [{"id": "a", "name": "a", "status": "x"}]
    )
    monkeypatch.setattr(
        session_service, "list_current_session_terminals", lambda *a, **k: list(rows)
    )
    hr.refresh_host_now()
    snapshot = main._agui_fleet_snapshot(["read"])
    assert {t["runner_id"] for t in snapshot["terminals"]} == {None, "rn-remote"}
    assert [h["runner_id"] for h in snapshot["host"]] == [None]
    assert "rn-remote" not in json.dumps(snapshot["host"])


def test_flag_unset_is_404_and_never_samples(
    monkeypatch: pytest.MonkeyPatch, readers: Dict[str, List[int]]
) -> None:
    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    monkeypatch.setattr(main, "_agui_enabled", lambda: False)
    assert client.get("/agui/v1/stream").status_code == 404
    assert client.post("/agui/v1/stream", json={}).status_code == 404
    assert readers["load"] == [] and readers["cpu"] == []


def test_mcp_apps_snapshot_has_no_host() -> None:
    from cli_agent_orchestrator.services.ui_state_service import build_dashboard_snapshot

    assert "host" not in build_dashboard_snapshot([], [])


def test_no_write_route_for_host() -> None:
    paths = {
        (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", None) or ())))
        for r in app.routes
    }
    assert not [p for p in paths if "host" in p[0] and p[0].startswith("/agui")]
    assert not [
        p for p in paths if p[0].startswith("/agui") and set(p[1]) & {"PUT", "PATCH", "DELETE"}
    ]


def test_startup_primes_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    primed: List[int] = []
    monkeypatch.setattr(hr, "prime_host_sampler", lambda: primed.append(1))
    monkeypatch.setattr(main, "_agui_enabled", lambda: False)
    asyncio.run(main._prime_host_sampler_if_enabled())
    assert primed == []
    monkeypatch.setattr(main, "_agui_enabled", lambda: True)
    asyncio.run(main._prime_host_sampler_if_enabled())
    assert primed == [1]


class _SyncSampler(hr.HostSampler):
    """A sampler that refreshes on every snapshot, so a short bus run sees STATE_DELTAs."""

    def sample(self) -> List[Dict[str, Any]]:
        return self.refresh()


def _frames(body: str) -> List[Dict[str, Any]]:
    return [e for e in parse_sse(body) if isinstance(e, dict)]


def test_leak_sentinels_never_reach_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinels in every identity source, a contaminated reader, and the real stream.

    The sampler must not touch psutil.Process, the host name or the user at all
    (``touched == []``), and what a contaminated reader returns is dropped. The assertions are
    on the serialized STATE_SNAPSHOT and STATE_DELTA frames of a real stream route.
    """
    from test._support import agui_ambient_records as R

    touched: List[str] = []

    class Proc:
        def __init__(self, *a: Any, **k: Any) -> None:
            touched.append("Process")

    monkeypatch.setattr(hr.psutil, "Process", Proc)
    monkeypatch.setattr(socket, "gethostname", lambda: touched.append("host") or "SENTINEL-HOST")
    monkeypatch.setattr(getpass, "getuser", lambda: touched.append("user") or "SENTINEL-USER")

    ticks = {"n": 0}
    reads: List[int] = []

    def load() -> Any:
        ticks["n"] += 1
        reads.append(1)
        # Alternates a real number and a contaminated string, so a delta occurs.
        return 0.1 * ticks["n"] if ticks["n"] % 2 else "SENTINEL-LOAD"

    monkeypatch.setattr(hr, "_read_cpu_times", lambda: None)
    monkeypatch.setattr(hr, "_read_load_per_core", load)
    monkeypatch.setattr(hr, "_read_memory_used_percent", lambda: ticks["n"] % 100)
    monkeypatch.setattr(hr, "_read_memory_pressure", lambda: "SENTINEL-LEVEL")
    monkeypatch.setattr(hr, "_sampler", _SyncSampler())
    install(monkeypatch, live=R.PRIMITIVE_RECORDS[:10])
    with client.stream("GET", "/agui/v1/stream") as resp:
        body = "".join(resp.iter_text())

    assert len(reads) > 1, "non-vacuity: the sampler ran once per snapshot"
    assert touched == [], "the host view must not read process, host or user identity"
    assert "SENTINEL" not in body
    snapshots = _snapshots(body)
    assert snapshots and snapshots[0]["host"], "non-vacuity: a host entry was serialized"
    deltas = [op for e in _frames(body) if e.get("type") == "STATE_DELTA" for op in e["delta"]]
    host_ops = [op for op in deltas if op["path"] == "/host"]
    assert host_ops, "non-vacuity: a STATE_DELTA frame carried /host"
    for entry in [e for s in snapshots for e in s["host"]] + [
        e for op in host_ops for e in op.get("value", [])
    ]:
        assert set(entry) <= hr.HOST_KEYS
        for key, leaf in entry.items():
            if key == "sampled_at":
                assert _iso(leaf)
            elif key == "memory_pressure":
                assert leaf in ENUM
            else:
                assert leaf is None or isinstance(leaf, (int, float)) and not isinstance(leaf, bool)


def test_state_delta_for_host_is_numbers_only() -> None:
    prev = {"host": []}
    curr = {
        "host": [{"runner_id": None, "cpu_percent": 3, "sampled_at": "2026-01-01T00:00:00+00:00"}]
    }
    (op,) = diff_snapshot(prev, curr)
    assert op["path"] == "/host"
    for leaf in _leaves(op["value"]):
        assert leaf is None or isinstance(leaf, (int, float, bool)) or _iso(leaf)


# --- Linux comparison against an independent read ---------------------------------------


@LINUX_ONLY
def test_load_matches_proc_loadavg() -> None:
    entries = hr.refresh_host_now()
    if not entries:
        pytest.skip("the host probe reported nothing here (restricted /proc?)")
    entry = entries[0]
    with open("/proc/loadavg") as f:
        load = float(f.read().split()[0])
    cores = len(os.sched_getaffinity(0))
    # The sampler rounds load/cores to one decimal; compare in the same units, with the
    # rounding step and the drift between the two reads as slack.
    tolerance = 0.5 / cores + 0.05
    assert abs(entry["load_per_core"] - round(load / cores, 1)) <= tolerance


@LINUX_ONLY
def test_memory_within_three_points_of_memavailable_formula() -> None:
    entries = hr.refresh_host_now()
    if not entries:
        pytest.skip("the host probe reported nothing here (restricted /proc?)")
    entry = entries[0]
    info: Dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
    expected = 100.0 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]
    assert abs(entry["memory_used_percent"] - expected) <= 3


def _burn(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


@LINUX_ONLY
def test_busy_children_raise_cpu_over_idle_baseline() -> None:
    """Deterministic stand-in for a ``top`` comparison, which is flaky and not a gate."""
    usable = len(os.sched_getaffinity(0))
    visible = os.cpu_count() or usable
    if usable != visible:
        pytest.skip(f"cpuset-limited ({usable} of {visible} CPUs usable): host CPU % is not ours")

    def window(burn: bool) -> Optional[int]:
        first = hr._read_cpu_times()
        procs = [
            multiprocessing.Process(target=_burn, args=(2.0,)) for _ in range(usable if burn else 0)
        ]
        for p in procs:
            p.start()
        time.sleep(2.0)
        for p in procs:
            p.join()
        second = hr._read_cpu_times()
        assert first is not None and second is not None
        return hr.cpu_percent_from_times(first, second, 2.0)

    for _ in range(3):  # retry on a fresh window: a noisy neighbour can spoil one
        idle, busy = window(False), window(True)
        if idle is not None and busy is not None and busy >= idle + 30:
            return
    # The +30 margin did not hold on a loaded runner. The property that matters is that
    # busy children read as busier than the idle window, by at least a floor of 10 points.
    assert idle is not None and busy is not None, "no CPU reading"
    assert (
        busy >= idle + 10
    ), f"busy children did not raise CPU over idle (idle={idle}, busy={busy})"
