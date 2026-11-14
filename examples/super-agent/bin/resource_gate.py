#!/usr/bin/env python3
"""Machine resource gate. Run it before every create_ephemeral_agent / assign / review-plan
approval, and before any full test suite. It is a GATE: check the exit code.

  resource_gate.py [--json] [--config resources.json]
      GO                 exit 0   go ahead
      THROTTLE <reasons> exit 3   at most one new worker at a time, small tiers, no parallel
                                  suites, delete finished workers first, defer non-urgent work
      STOP <reasons>     exit 1   create nothing, start no suite; let running work finish
  resource_gate.py --acquire-heavy LABEL   ACQUIRED <label> token=<32 hex> ... (0)
                                           | REFUSED STOP|BUSY|STALE|LOCKED ... (1)
  resource_gate.py --refresh-heavy TOKEN   REFRESHED (0) | NOT_HELD / REFUSED not_owner|STALE (1)
  resource_gate.py --release-heavy TOKEN   RELEASED (0) | NOT_HELD / REFUSED not_owner (1)

Usage in a shell:  python3 bin/resource_gate.py || { echo "not GO"; exit 1; }
Heavy jobs (full suites, review-workflow runs, main-merge full suites): one machine-wide,
marked by the directory state/heavy/active. Every lifecycle step (acquire, refresh, release)
runs under fcntl.flock on state/heavy.lock, so no step can interleave with another. Acquire
prints an unguessable token (secrets.token_hex(16)); refresh and release need that token, so
nobody can release someone else's slot by reusing a label. A marker whose last refresh is
older than heavy.stale_hours is reported as STALE, never taken over. owner.json is read and
written through bin/state_schema.py: a damaged one is REFUSED state_corrupt:heavy/active/owner.json:1:<why>.

Measurements are read-only, quick and need no sudo: kern.memorystatus_vm_pressure_level,
memory_pressure's free percentage, vm.swapusage, free GiB on disk_path, load1 / ncpu, and
live CAO terminals across all sessions. A value that can't be measured, or a broken CAO API,
counts as THROTTLE with a reason. So does a value that is not a sane measurement: pressure must
be exactly 1, 2 or 4, and every number must be finite and inside a plausible range.
Tests inject measurements with SUPER_AGENT_FAKE_MEASUREMENTS=<json file>; then no system
command runs. SUPER_AGENT_PROBE_PATH replaces the tool search path (tests only).
"""
import argparse
import importlib.util
import json
import os
import fcntl
import math
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

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
EXIT = {"GO": 0, "STOP": 1, "THROTTLE": 3}
KEYS = ("pressure_level", "free_memory_pct", "swap_used_gib", "disk_free_gib", "load1", "ncpu",
        "live_terminals")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
VALID_PRESSURE = (1, 2, 4)  # kern.memorystatus_vm_pressure_level: normal, warn, critical
# Plausible ranges (inclusive). Anything outside, non-finite or non-numeric is THROTTLE.
RANGES = {"free_memory_pct": (0, 100), "swap_used_gib": (0, 4096), "disk_free_gib": (0, 10 ** 6),
          "load1": (0, 10 ** 5), "ncpu": (1, 4096), "live_terminals": (0, 10 ** 5)}
INT_KEYS = ("ncpu", "live_terminals")
DEFAULT_TIMEOUTS = {"sysctl": 5, "memory_pressure": 20}


# ---------- measurement ----------

def _tool(name):
    search = os.environ.get("SUPER_AGENT_PROBE_PATH") or (
        os.environ.get("PATH", "") + ":/usr/sbin:/usr/bin")
    return shutil.which(name, path=search)


def _run(argv, timeout):
    exe = _tool(argv[0])
    if not exe:
        raise OSError(f"{argv[0]} not found")
    out = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise OSError(f"{argv[0]} exited {out.returncode}")
    return out.stdout


def _safe(fn):
    try:
        return fn()
    except Exception:  # an unmeasurable value becomes None -> THROTTLE "unmeasured"
        return None


# Parsers are pure: raw tool output in, number out, ValueError on anything unexpected.
def parse_pressure(text):
    t = (text or "").strip()
    if not re.fullmatch(r"-?[0-9]+", t):
        raise ValueError("pressure level is not an integer")
    return int(t)


def parse_free_pct(text):
    m = re.search(r"free percentage:\s*([0-9]+(?:\.[0-9]+)?)%", text or "")
    if not m:
        raise ValueError("no free percentage")
    return float(m.group(1))


def parse_swap_gib(text):
    m = re.search(r"used\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMG])\b", text or "")
    if not m:
        raise ValueError("no swap used value")
    return float(m.group(1)) / {"K": 1024 ** 2, "M": 1024, "G": 1}[m.group(2)]


def _timeout(cfg, tool):
    return float((cfg.get("probe_timeouts") or {}).get(tool, DEFAULT_TIMEOUTS[tool]))


def _pressure(cfg):
    return parse_pressure(_run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                               _timeout(cfg, "sysctl")))


def _free_pct(cfg):
    return parse_free_pct(_run(["memory_pressure"], _timeout(cfg, "memory_pressure")))


def _swap_gib(cfg):
    return parse_swap_gib(_run(["sysctl", "-n", "vm.swapusage"], _timeout(cfg, "sysctl")))


def _disk_gib(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 ** 3


def count_live_terminals(base_url, timeout=3):
    """Return (count, None) or (None, 'cao_api:<reason>'). Never raises."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with opener.open(base_url.rstrip("/") + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    try:
        sessions = get("/sessions")
        if not isinstance(sessions, list):
            return None, "cao_api:bad_shape"
        total = 0
        for s in sessions:
            name = s.get("name") or s.get("id") if isinstance(s, dict) else None
            if not isinstance(name, str) or not name.strip():  # never ask for /sessions/None
                return None, "cao_api:bad_shape"
            terms = get(f"/sessions/{urllib.parse.quote(str(name), safe='')}/terminals")
            if not isinstance(terms, list):
                return None, "cao_api:bad_shape"
            total += len(terms)
        return total, None
    except Exception as exc:
        return None, f"cao_api:{type(exc).__name__}"


def measure(cfg):
    cao_url = os.environ.get("SUPER_AGENT_CAO_URL") or cfg.get("cao_url", "http://localhost:9889")
    fake = os.environ.get("SUPER_AGENT_FAKE_MEASUREMENTS")
    errors = []
    if fake:
        with open(fake) as fh:
            given = json.load(fh)
        m = {k: given.get(k) for k in KEYS}
        if "live_terminals" not in given:
            m["live_terminals"], err = count_live_terminals(cao_url)
            errors += [err] if err else []
        return m, errors
    m = {
        "pressure_level": _safe(lambda: _pressure(cfg)),
        "free_memory_pct": _safe(lambda: _free_pct(cfg)),
        "swap_used_gib": _safe(lambda: _swap_gib(cfg)),
        "disk_free_gib": _safe(lambda: _disk_gib(cfg.get("disk_path", "/System/Volumes/Data"))),
        "load1": _safe(lambda: os.getloadavg()[0]),
        "ncpu": _safe(os.cpu_count),
    }
    m["live_terminals"], err = count_live_terminals(cao_url)
    errors += [err] if err else []
    return m, errors


# ---------- decision (pure) ----------

def _show(v):
    return re.sub(r"\s", "_", repr(v))[:24]


def validate(m):
    """Return (clean, problems). Pure. Invalid values become None in `clean` and are named in
    `problems`, so they can never reach a comparison (NaN compares False with everything)."""
    clean, problems = dict(m), []
    p = m.get("pressure_level")
    if p is not None and (isinstance(p, bool) or not isinstance(p, int) or p not in VALID_PRESSURE):
        problems.append(f"unknown:pressure={_show(p)}")
        clean["pressure_level"] = None
    for key, (lo, hi) in RANGES.items():
        v = m.get(key)
        if v is None:
            continue
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        ok = ok and math.isfinite(v) and lo <= v <= hi
        if ok and key in INT_KEYS:
            ok = isinstance(v, int)
        if not ok:
            problems.append(f"invalid:{key}={_show(v)}")
            clean[key] = None
    return clean, problems


def decide(m, cfg, errors=()):
    """Return (verdict, reasons). Pure."""
    st, th = cfg["stop"], cfg["throttle"]
    m, problems = validate(m)
    stop, throttle = [], list(errors) + problems
    invalid = {x.split(":", 1)[1].split("=", 1)[0] for x in problems}
    p, f, s = m.get("pressure_level"), m.get("free_memory_pct"), m.get("swap_used_gib")
    d, t = m.get("disk_free_gib"), m.get("live_terminals")
    load = None
    if m.get("load1") is not None and m.get("ncpu"):
        load = m["load1"] / m["ncpu"]

    for name, val, keys in (("pressure", p, ("pressure",)), ("free_mem_pct", f, ("free_memory_pct",)),
                            ("swap_gib", s, ("swap_used_gib",)), ("disk_free_gib", d, ("disk_free_gib",)),
                            ("load", load, ("load1", "ncpu")), ("live_terminals", t, ("live_terminals",))):
        if val is None and not (name == "live_terminals" and errors) and not invalid & set(keys):
            throttle.append(f"unmeasured:{name}")

    if p is not None:
        if p >= st["pressure_level_gte"]:
            stop.append(f"pressure={p}>={st['pressure_level_gte']}")
        elif p >= th["pressure_level_gte"]:
            throttle.append(f"pressure={p}>={th['pressure_level_gte']}")
    if f is not None:
        if f < st["free_memory_pct_lt"]:
            stop.append(f"free_mem_pct={f:g}<{st['free_memory_pct_lt']}")
        elif f < th["free_memory_pct_lt"]:
            throttle.append(f"free_mem_pct={f:g}<{th['free_memory_pct_lt']}")
    if s is not None and s > th["swap_used_gib_gt"]:
        throttle.append(f"swap_gib={s:.1f}>{th['swap_used_gib_gt']}")
    if d is not None:
        if d < st["disk_free_gib_lt"]:
            stop.append(f"disk_free_gib={d:.1f}<{st['disk_free_gib_lt']}")
        elif d < th["disk_free_gib_lt"]:
            throttle.append(f"disk_free_gib={d:.1f}<{th['disk_free_gib_lt']}")
    if load is not None and load > th["load_per_cpu_gt"]:
        throttle.append(f"load_per_cpu={load:.2f}>{th['load_per_cpu_gt']}")
    if t is not None:
        if t >= st["live_terminals_gte"]:
            stop.append(f"live_terminals={t}>={st['live_terminals_gte']}")
        elif t >= th["live_terminals_gte"]:
            throttle.append(f"live_terminals={t}>={th['live_terminals_gte']}")

    if stop:
        return "STOP", stop
    if throttle:
        return "THROTTLE", throttle
    return "GO", []


# ---------- heavy-job marker ----------

class LockTimeout(Exception):
    pass


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def heavy_marker():
    return os.path.join(state_dir(), "heavy", "active")


class heavy_lock:
    """fcntl.flock on state/heavy.lock around one whole lifecycle step."""

    def __init__(self, cfg):
        self.timeout = float((cfg.get("heavy") or {}).get("lock_timeout_s", 10))

    def __enter__(self):
        os.makedirs(state_dir(), exist_ok=True)
        self.fd = os.open(os.path.join(state_dir(), "heavy.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise LockTimeout()
                time.sleep(0.05)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def _owner(marker):
    """The validated owner.json (SS.load_owner: label, token, acquired_at, refreshed_at, pid).
    A marker whose owner file is missing or unreadable (a crash between mkdir and the write) is
    a placeholder that nobody owns, aged by the marker's mtime. A damaged owner file raises
    SS.StateCorrupt: REFUSED, never taken over or released."""
    try:
        return SS.load_owner(os.path.join(marker, "owner.json"))
    except OSError:
        mtime = os.stat(marker).st_mtime
        return {"label": "?", "token": None, "acquired_at": mtime, "refreshed_at": mtime}


def _is_owner(owner, token):
    held = owner.get("token")
    return isinstance(held, str) and secrets.compare_digest(held, token)


def _write_owner(marker, data):
    SS.check_owner_write(os.path.join(marker, "owner.json"), data)  # never write what _owner rejects
    tmp = os.path.join(marker, "owner.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(marker, "owner.json"))


def acquire(label, verdict, reasons, cfg):
    if verdict == "STOP":
        return 1, f"REFUSED STOP {' '.join(reasons)}"
    marker = heavy_marker()
    stale_s = float(cfg["heavy"]["stale_hours"]) * 3600
    with heavy_lock(cfg):
        if os.path.isdir(marker):
            owner = _owner(marker)
            age = time.time() - owner["refreshed_at"]
            if age > stale_s:
                return 1, (f"REFUSED STALE held_by={owner.get('label')} age={age / 3600:.1f}h "
                           f"(report it to the conductor; never take it over)")
            return 1, f"REFUSED BUSY {owner.get('label')} age={age / 3600:.1f}h"
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        os.mkdir(marker)
        token = secrets.token_hex(16)
        now = time.time()
        _write_owner(marker, {"label": label, "token": token, "acquired_at": now,
                              "refreshed_at": now, "pid": os.getppid()})
    return 0, f"ACQUIRED {label} token={token} verdict={verdict}"


def refresh(token, cfg):
    marker = heavy_marker()
    stale_s = float(cfg["heavy"]["stale_hours"]) * 3600
    with heavy_lock(cfg):
        if not os.path.isdir(marker):
            return 1, "NOT_HELD"
        owner = _owner(marker)
        if not _is_owner(owner, token):
            return 1, f"REFUSED not_owner held_by={owner.get('label')}"
        age = time.time() - owner["refreshed_at"]
        if age > stale_s:
            return 1, (f"REFUSED STALE held_by={owner.get('label')} age={age / 3600:.1f}h "
                       f"(release it and report to the conductor)")
        owner["refreshed_at"] = time.time()
        _write_owner(marker, owner)
    return 0, f"REFRESHED {owner.get('label')}"


def release(token, cfg):
    marker = heavy_marker()
    with heavy_lock(cfg):
        if not os.path.isdir(marker):
            return 1, "NOT_HELD"
        owner = _owner(marker)
        if not _is_owner(owner, token):
            return 1, f"REFUSED not_owner held_by={owner.get('label')}"
        os.remove(os.path.join(marker, "owner.json"))
        os.rmdir(marker)
    return 0, f"RELEASED {owner.get('label')}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Machine resource gate: GO / THROTTLE / STOP.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--config", default=os.path.join(KIT, "resources.json"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--acquire-heavy", metavar="LABEL")
    g.add_argument("--refresh-heavy", metavar="TOKEN")
    g.add_argument("--release-heavy", metavar="TOKEN")
    a = ap.parse_args(argv)

    if a.acquire_heavy is not None and not LABEL_RE.fullmatch(a.acquire_heavy):
        print("ERROR bad label (want [A-Za-z0-9][A-Za-z0-9_.-]{0,63})")
        return 2
    token = a.release_heavy if a.release_heavy is not None else a.refresh_heavy
    if token is not None and not TOKEN_RE.fullmatch(token):
        print("ERROR bad token (want the 32 lowercase hex from ACQUIRED ... token=)")
        return 2
    try:
        with open(a.config) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"STOP unreadable_config:{type(exc).__name__}")
        return EXIT["STOP"]

    try:
        if a.release_heavy is not None:
            rc, line = release(token, cfg)
            print(line)
            return rc
        if a.refresh_heavy is not None:
            rc, line = refresh(token, cfg)
            print(line)
            return rc
    except LockTimeout:
        print("REFUSED LOCKED state/heavy.lock is held; try again on a later wake")
        return 1
    except SS.StateCorrupt as exc:
        print(f"REFUSED {exc} (report it to the conductor; never take it over)")
        return 1

    try:
        m, errors = measure(cfg)
    except (OSError, ValueError) as exc:  # e.g. an unreadable fake file
        m, errors = {k: None for k in KEYS}, [f"measure:{type(exc).__name__}"]
    verdict, reasons = decide(m, cfg, errors)

    if a.acquire_heavy is not None:
        try:
            rc, line = acquire(a.acquire_heavy, verdict, reasons, cfg)
        except LockTimeout:
            rc, line = 1, "REFUSED LOCKED state/heavy.lock is held; try again on a later wake"
        except SS.StateCorrupt as exc:
            rc, line = 1, f"REFUSED {exc} (report it to the conductor; never take it over)"
        print(line)
        return rc
    if a.json:
        print(json.dumps({"verdict": verdict, "reasons": reasons, "measurements": m}, indent=1))
    else:
        print(f"{verdict} {' '.join(reasons)}".rstrip())
    return EXIT[verdict]


if __name__ == "__main__":
    sys.exit(main())
