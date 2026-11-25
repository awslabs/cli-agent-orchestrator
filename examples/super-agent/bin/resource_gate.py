#!/usr/bin/env python3
"""Machine resource gate. Run it before every create_ephemeral_agent / assign / review-plan
approval, and before any full test suite. It is a GATE: check the exit code.

  resource_gate.py [--json] [--config resources.json]
      GO                 exit 0   go ahead
      THROTTLE <reasons> exit 3   at most one new worker at a time, small tiers, no parallel
                                  suites, delete finished workers first, defer non-urgent work
      STOP <reasons>     exit 1   create nothing, start no suite; let running work finish
  resource_gate.py --acquire-heavy LABEL   ACQUIRED <label> token=<32 hex> ... (0)
                                           | REFUSED STOP|BUSY|STALE|THROTTLE_CAP|LOCKED ... (1)
  resource_gate.py --refresh-heavy TOKEN   REFRESHED (0) | NOT_HELD / REFUSED not_owner|STALE (1)
  resource_gate.py --release-heavy TOKEN   RELEASED (0) | NOT_HELD / REFUSED not_owner (1)

Usage in a shell:  python3 bin/resource_gate.py || { echo "not GO"; exit 1; }
Heavy jobs (full suites, review-workflow runs, main-merge full suites): up to heavy.slots
(1..8, default 1) at once machine-wide, marked by the directory state/heavy/active with one
holder file per slot: owner.json is slot 1 (the single-slot file, so a holder written before
slots existed is simply slot 1), owner-<n>.json slot n. Every lifecycle step (acquire,
refresh, release) runs under fcntl.flock on state/heavy.lock, so no step can interleave with
another and acquires can never exceed heavy.slots. Each acquire prints its own unguessable
token (secrets.token_hex(16)); refresh and release need that token and act on that holder
only, so nobody can release someone else's slot by reusing a label.
Every holder counts towards heavy.slots, stale or not. A holder whose last refresh is older
than heavy.stale_hours is never taken over: when every slot is held and one is stale, acquire
prints REFUSED STALE held_by=<label> age=<h>h ...; when a slot is still free, acquire succeeds
and warns STALE held_by=... on stderr (stdout stays the one ACQUIRED line). When every slot is
held by fresh holders: REFUSED BUSY <label> age=<h>h [<label> age=<h>h ...], slot order.
The verdict caps new acquires: GO allows heavy.slots holders; THROTTLE allows one, so an
acquire with any holder in place is REFUSED THROTTLE_CAP held_by=<label>[,<label>...]
(THROTTLE <reasons>; one heavy run at a time while throttled); STOP refuses every acquire.
Holders already in place keep their slots: refresh and release never look at the verdict.
Holder files are read and written through bin/state_schema.py: a damaged one, a duplicate
token or an unknown entry in heavy/active is REFUSED state_corrupt:heavy/active/<file>:1:<why>.

Measurements are read-only, quick and need no sudo: kern.memorystatus_vm_pressure_level,
memory_pressure's free percentage, vm.swapusage, free GiB on disk_path(s), load1 / ncpu, and
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


def emit_hint(line):
    """Print a reason-specific recovery hint without changing machine stdout."""
    reason = line.split(maxsplit=1)[1] if " " in line else ""
    if "disk_free_gib=" in reason:
        value = reason.split("disk_free_gib=", 1)[1].split()[0]
        hint = f"free space is below its configured limit ({value}); free space on the named path"
    elif line.startswith("REFUSED LOCKED"):
        hint = "the heavy-slot lock is held; retry on a later wake"
    elif line.startswith("REFUSED STALE"):
        hint = "the named heavy holder is stale; report it to the conductor and do not take it over"
    elif line.startswith("REFUSED not_owner"):
        hint = "the token is not the recorded holder; use the owning terminal or wait for release"
    elif line.startswith("STOP bad_config"):
        hint = "repair the named resources configuration field before starting work"
    elif line.startswith("STOP"):
        hint = f"new work is stopped by {reason}; resolve that measured condition"
    elif line.startswith("THROTTLE"):
        hint = f"defer nonessential work until {reason} clears, then re-check"
    else:
        hint = f"resolve {reason}, then retry the resource operation"
    print(f"hint: {hint}", file=sys.stderr)
MAC_KEYS = ("pressure_level", "free_memory_pct", "swap_used_gib", "disk_free_gib", "load1", "ncpu",
        "live_terminals")
LINUX_KEYS = ("memory_psi_some_avg10", "memory_psi_full_avg10", "memory_available_gib",
              "swap_used_gib", "disk_free_gib", "load1", "ncpu", "live_terminals")
KEYS = MAC_KEYS  # existing callers on macOS
PROC_ROOT = "/proc"
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


def _disk_paths(cfg, default):
    paths = cfg.get("disk_paths")
    return list(paths) if isinstance(paths, dict) else paths if paths is not None else [cfg.get("disk_path", default)]


def _measure_disks(paths, measure):
    values = {}
    for path in paths:
        try:
            values[path] = measure(path)
        except Exception:
            return values, path
    return values, None


def _disk_result(values, missing):
    if missing is not None:
        return None, missing
    path, free = min(values.items(), key=lambda item: item[1])
    return free, path


def is_loopback_api(url):
    """True for http://localhost:<port> or http://127.0.0.1:<port>: workplace.API_RE, the rule cao_api gets."""
    try:
        m = SS.workplace().API_RE.match(url) if isinstance(url, str) else None
        return bool(m) and 0 < int(m.group(1)) < 65536
    except Exception:  # an unloadable rule is never "loopback"
        return False


def list_live_terminals(base_url, timeout=3):
    """Use CAO's real per-session routes; return (rows, None) or (None, cao_api error)."""
    if not base_url:
        return None, "cao_api:workplace_config_unreadable"
    if not is_loopback_api(base_url):  # SUPER_AGENT_CAO_URL and resources cao_url get cao_api's rule
        return None, "cao_api:not_loopback"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with opener.open(base_url.rstrip("/") + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    try:
        sessions = get("/sessions")
        if not isinstance(sessions, list):
            return None, "cao_api:bad_shape"
        all_terms = []
        for s in sessions:
            name = s.get("name") or s.get("id") if isinstance(s, dict) else None
            if not isinstance(name, str) or not name.strip():  # never ask for /sessions/None
                return None, "cao_api:bad_shape"
            terms = get(f"/sessions/{urllib.parse.quote(str(name), safe='')}/terminals")
            if not isinstance(terms, list):
                return None, "cao_api:bad_shape"
            all_terms.extend(terms)
        return all_terms, None
    except Exception as exc:
        return None, f"cao_api:{type(exc).__name__}"


def count_live_terminals(base_url, timeout=3):
    """Return (count, None) or (None, 'cao_api:<reason>'). Never raises."""
    rows, error = list_live_terminals(base_url, timeout)
    return (len(rows), None) if rows is not None else (None, error)


def _linux_memory():
    with open(os.path.join(PROC_ROOT, "meminfo")) as fh:
        text = fh.read()
    values = {}
    for key in ("MemAvailable", "SwapTotal", "SwapFree"):
        hits = re.findall(r"^" + key + r":\s*([0-9]+)\s+kB\s*$", text, re.M)
        if len(hits) != 1:
            raise ValueError("missing or invalid meminfo field")
        values[key] = int(hits[0])
    if values["SwapFree"] > values["SwapTotal"]:
        raise ValueError("invalid swap accounting")
    return values["MemAvailable"] / 1024 ** 2, (values["SwapTotal"] - values["SwapFree"]) / 1024 ** 2


def _linux_psi():
    with open(os.path.join(PROC_ROOT, "pressure", "memory")) as fh:
        text = fh.read()
    values = []
    for kind in ("some", "full"):
        hits = re.findall(r"^" + kind + r"\s+avg10=([0-9]+(?:\.[0-9]+)?)(?:\s|$)", text, re.M)
        if len(hits) != 1:
            raise ValueError("missing or invalid PSI field")
        value = float(hits[0])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("PSI outside percentage range")
        values.append(value)
    return tuple(values)


def _measure_linux(cfg, cao_url):
    memory = _safe(_linux_memory) or (None, None)
    psi = _safe(_linux_psi) or (None, None)
    disks, missing = _measure_disks(
        _disk_paths(cfg, "/Users"),
        lambda path: shutil.disk_usage(path).free / 1024 ** 3)
    disk_free, disk_path = _disk_result(disks, missing)
    m = {"memory_psi_some_avg10": psi[0], "memory_psi_full_avg10": psi[1],
         "memory_available_gib": memory[0], "swap_used_gib": memory[1],
         "disk_free_gib": disk_free, "disk_path": disk_path,
         "disk_free_gib_by_path": disks,
         "load1": _safe(lambda: os.getloadavg()[0]), "ncpu": _safe(os.cpu_count)}
    m["live_terminals"], err = count_live_terminals(cao_url)
    return m, [err] if err else []


def measure(cfg):
    cao_url = os.environ.get("SUPER_AGENT_CAO_URL") or cfg.get("cao_url") or SS.cao_api()
    fake = os.environ.get("SUPER_AGENT_FAKE_MEASUREMENTS")
    errors = []
    if fake:
        with open(fake) as fh:
            given = json.load(fh)
        m = {k: given.get(k) for k in (LINUX_KEYS if "memory_available_gib_lt" in cfg["stop"] else MAC_KEYS)}
        if "live_terminals" not in given:
            m["live_terminals"], err = count_live_terminals(cao_url)
            errors += [err] if err else []
        return m, errors
    if "memory_available_gib_lt" in cfg["stop"]:
        return _measure_linux(cfg, cao_url)
    disks, missing = _measure_disks(
        _disk_paths(cfg, "/System/Volumes/Data"), _disk_gib)
    disk_free, disk_path = _disk_result(disks, missing)
    m = {
        "pressure_level": _safe(lambda: _pressure(cfg)),
        "free_memory_pct": _safe(lambda: _free_pct(cfg)),
        "swap_used_gib": _safe(lambda: _swap_gib(cfg)),
        "disk_free_gib": disk_free, "disk_path": disk_path,
        "disk_free_gib_by_path": disks,
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


def _disk_limits(cfg, path):
    paths = cfg.get("disk_paths")
    if isinstance(paths, dict):
        rule = paths[path]
        return rule["stop_lt"], rule["throttle_lt"]
    return cfg["stop"]["disk_free_gib_lt"], cfg["throttle"]["disk_free_gib_lt"]


def _decide_disk_paths(m, cfg, stop, throttle):
    paths = cfg.get("disk_paths")
    if not isinstance(paths, dict):
        return False
    values = m.get("disk_free_gib_by_path")
    if values is None:
        # Preserve the injected-measurement contract used by callers that
        # provide only the legacy scalar; live measurement supplies the map.
        values = {path: m.get("disk_free_gib") for path in paths}
    for path in paths:
        value = values.get(path)
        if value is None:
            throttle.append(f"unmeasured:disk_free_gib ({path})")
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 10 ** 6):
            throttle.append(f"invalid:disk_free_gib={_show(value)} ({path})")
            continue
        stop_lt, throttle_lt = _disk_limits(cfg, path)
        if value < stop_lt:
            stop.append(f"disk_free_gib={value:.1f}<{stop_lt} ({path})")
        elif value < throttle_lt:
            throttle.append(f"disk_free_gib={value:.1f}<{throttle_lt} ({path})")
    return True


def _decide_macos(m, cfg, errors=()):
    """Return (verdict, reasons). Pure."""
    st, th = cfg["stop"], cfg["throttle"]
    m, problems = validate(m)
    stop, throttle = [], list(errors) + problems
    invalid = {x.split(":", 1)[1].split("=", 1)[0] for x in problems}
    p, f, s = m.get("pressure_level"), m.get("free_memory_pct"), m.get("swap_used_gib")
    d, t = m.get("disk_free_gib"), m.get("live_terminals")
    disk_suffix = f" ({m['disk_path']})" if m.get("disk_path") else ""
    per_path_disk = _decide_disk_paths(m, cfg, stop, throttle)
    load = None
    if m.get("load1") is not None and m.get("ncpu"):
        load = m["load1"] / m["ncpu"]

    for name, val, keys in (("pressure", p, ("pressure",)), ("free_mem_pct", f, ("free_memory_pct",)),
                            ("swap_gib", s, ("swap_used_gib",)),
                            *((("disk_free_gib", d, ("disk_free_gib",)),) if not per_path_disk else ()),
                            ("load", load, ("load1", "ncpu")), ("live_terminals", t, ("live_terminals",))):
        if val is None and not (name == "live_terminals" and errors) and not invalid & set(keys):
            throttle.append(f"unmeasured:{name}{disk_suffix if name == 'disk_free_gib' else ''}")

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
    if d is not None and not per_path_disk:
        if d < st["disk_free_gib_lt"]:
            stop.append(f"disk_free_gib={d:.1f}<{st['disk_free_gib_lt']}{disk_suffix}")
        elif d < th["disk_free_gib_lt"]:
            throttle.append(f"disk_free_gib={d:.1f}<{th['disk_free_gib_lt']}{disk_suffix}")
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


def decide(m, cfg, errors=()):
    if "memory_available_gib_lt" not in cfg["stop"]:
        return _decide_macos(m, cfg, errors)
    st, th = cfg["stop"], cfg["throttle"]
    stop, throttle = [], list(errors)
    ranges = {**RANGES, "memory_psi_some_avg10": (0, 100),
              "memory_psi_full_avg10": (0, 100), "memory_available_gib": (0, 10 ** 6)}
    clean = dict(m)
    per_path_disk = isinstance(cfg.get("disk_paths"), dict)
    for key in LINUX_KEYS:
        if key == "disk_free_gib" and per_path_disk:
            continue
        value = m.get(key)
        lo, hi = ranges[key]
        if value is None:
            if not (key == "live_terminals" and errors):
                suffix = f" ({m['disk_path']})" if key == "disk_free_gib" and m.get("disk_path") else ""
                throttle.append(f"unmeasured:{key}{suffix}")
        elif (isinstance(value, bool) or not isinstance(value, (int, float))
              or not math.isfinite(value) or not lo <= value <= hi
              or (key in INT_KEYS and not isinstance(value, int))):
            throttle.append(f"invalid:{key}={_show(value)}")
            clean[key] = None
    if per_path_disk:
        _decide_disk_paths(clean, cfg, stop, throttle)
    for key in ("memory_psi_some_avg10", "memory_psi_full_avg10", "memory_available_gib",
                "swap_used_gib", *(() if per_path_disk else ("disk_free_gib",)), "live_terminals"):
        value = clean.get(key)
        if value is None:
            continue
        suffix = "_lt" if key in ("memory_available_gib", "disk_free_gib") else "_gte" if key == "live_terminals" else "_gt"
        symbol = {"_lt": "<", "_gte": ">=", "_gt": ">"}[suffix]
        def crossed(limit):
            return value < limit if suffix == "_lt" else value >= limit if suffix == "_gte" else value > limit
        for rules, reasons in ((st, stop), (th, throttle)):
            name = key + suffix
            if name in rules and crossed(rules[name]):
                path_suffix = f" ({clean['disk_path']})" if key == "disk_free_gib" and clean.get("disk_path") else ""
                reasons.append(f"{key}={value}{symbol}{rules[name]}{path_suffix}")
                break
    if clean.get("load1") is not None and clean.get("ncpu") is not None:
        load = clean["load1"] / clean["ncpu"]
        if load > th["load_per_cpu_gt"]:
            throttle.append(f"load_per_cpu={load}>{th['load_per_cpu_gt']}")
    return ("STOP", stop) if stop else ("THROTTLE", throttle) if throttle else ("GO", [])


# ---------- heavy-job marker ----------

class LockTimeout(Exception):
    pass


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def heavy_marker():
    return os.path.join(state_dir(), "heavy", "active")


def heavy_slots(cfg):
    """heavy.slots from the validated config: how many heavy holders may exist at once."""
    return (cfg.get("heavy") or {}).get("slots", 1)


class heavy_lock:
    """fcntl.flock on state/heavy.lock around one whole lifecycle step."""

    def __init__(self, cfg):
        self.timeout = float((cfg.get("heavy") or {}).get("lock_timeout_s", 10))

    def __enter__(self):
        # Host-local machine coordination, independent of the kit's writer lease.
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


def _holders(marker):
    """[(file name, row)] of every holder, slot order, each validated by SS.load_heavy_holders
    (label, token, acquired_at, refreshed_at, pid). [] when no marker exists. A marker with no
    holder file (a crash between mkdir and the write) is one placeholder that nobody owns
    (file name None), aged by the marker's mtime. A damaged holder file, a duplicate token or an
    unknown entry raises SS.StateCorrupt: REFUSED, never taken over or released."""
    if os.path.lexists(marker) and not os.path.isdir(marker):
        raise SS.StateCorrupt(SS.label(marker), 1, "file:not_a_directory")
    try:
        holders = SS.load_heavy_holders(marker)
    except FileNotFoundError:
        return []
    if not holders:
        mtime = os.stat(marker).st_mtime
        return [(None, {"label": "?", "token": None, "acquired_at": mtime, "refreshed_at": mtime})]
    return holders


def _age_h(row, now):
    return f"{(now - row['refreshed_at']) / 3600:.1f}h"


def _held_by(holders):
    return ",".join(str(row.get("label")) for _, row in holders)


def _is_owner(owner, token):
    held = owner.get("token")
    return isinstance(held, str) and secrets.compare_digest(held, token)


def _write_owner(marker, data, name="owner.json"):
    path = os.path.join(marker, name)
    SS.check_owner_write(path, data)  # never write what _holders rejects
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def acquire(label, verdict, reasons, cfg):
    if verdict == "STOP":
        return 1, f"REFUSED STOP {' '.join(reasons)}"
    marker = heavy_marker()
    stale_s = float(cfg["heavy"]["stale_hours"]) * 3600
    slots = heavy_slots(cfg)
    with heavy_lock(cfg):
        holders = _holders(marker)
        now = time.time()
        stale = [(n, r) for n, r in holders if now - r["refreshed_at"] > stale_s]
        stale_list = " ".join(f"held_by={r.get('label')} age={_age_h(r, now)}" for _, r in stale)
        if len(holders) >= slots:
            if stale:
                return 1, (f"REFUSED STALE {stale_list} "
                           f"(report it to the conductor; never take it over)")
            return 1, "REFUSED BUSY " + " ".join(f"{r.get('label')} age={_age_h(r, now)}"
                                                 for _, r in holders)
        if verdict == "THROTTLE" and holders:  # capacity 1 while throttled
            if stale:
                print(f"STALE {stale_list} (report it to the conductor; never take it over)",
                      file=sys.stderr)
            return 1, (f"REFUSED THROTTLE_CAP held_by={_held_by(holders)} "
                       f"(THROTTLE {' '.join(reasons)}; one heavy run at a time while throttled)")
        if not holders:
            try:
                os.mkdir(os.path.dirname(marker))
            except FileExistsError:
                if not os.path.isdir(os.path.dirname(marker)):
                    raise
            os.mkdir(marker)
        used = {n for n, _ in holders}
        name = next(SS.heavy_holder_name(s) for s in range(1, slots + 1)
                    if SS.heavy_holder_name(s) not in used)
        token = secrets.token_hex(16)
        _write_owner(marker, {"label": label, "token": token, "acquired_at": now,
                              "refreshed_at": now, "pid": os.getppid()}, name)
    if stale:
        print(f"STALE {stale_list} (report it to the conductor; never take it over)",
              file=sys.stderr)
    return 0, f"ACQUIRED {label} token={token} verdict={verdict}"


def refresh(token, cfg):
    marker = heavy_marker()
    stale_s = float(cfg["heavy"]["stale_hours"]) * 3600
    with heavy_lock(cfg):
        holders = _holders(marker)
        if not holders:
            return 1, "NOT_HELD"
        mine = [(n, r) for n, r in holders if _is_owner(r, token)]
        if not mine:
            return 1, f"REFUSED not_owner held_by={_held_by(holders)}"
        name, owner = mine[0]
        age = time.time() - owner["refreshed_at"]
        if age > stale_s:
            return 1, (f"REFUSED STALE held_by={owner.get('label')} age={age / 3600:.1f}h "
                       f"(release it and report to the conductor)")
        owner["refreshed_at"] = time.time()
        _write_owner(marker, owner, name)
    return 0, f"REFRESHED {owner.get('label')}"


def release(token, cfg):
    marker = heavy_marker()
    with heavy_lock(cfg):
        holders = _holders(marker)
        if not holders:
            return 1, "NOT_HELD"
        mine = [(n, r) for n, r in holders if _is_owner(r, token)]
        if not mine:
            return 1, f"REFUSED not_owner held_by={_held_by(holders)}"
        name, owner = mine[0]
        os.remove(os.path.join(marker, name))
        if len(holders) == 1:  # the last holder: drop cut-short writes, then the marker
            for leftover in os.listdir(marker):
                if SS.HEAVY_TMP_RE.fullmatch(leftover):
                    os.remove(os.path.join(marker, leftover))
            os.rmdir(marker)
    return 0, f"RELEASED {owner.get('label')}"


class BadConfig(ValueError):
    pass


def validate_config(cfg):
    """Reject typos, missing rules and non-finite/non-numeric limits before probing."""
    if not isinstance(cfg, dict):
        raise BadConfig('root:not_object')
    known = {'about', 'stop', 'throttle', 'heavy', 'cao_url', 'disk_path', 'disk_paths', 'probe_timeouts'}
    unknown = set(cfg) - known
    if unknown:
        raise BadConfig('root:unknown_key:' + sorted(unknown)[0])
    st = cfg.get('stop')
    if not isinstance(st, dict):
        raise BadConfig('stop:not_object')
    th = cfg.get('throttle')
    linux = any(k.startswith('memory_') for k in st) or (
        isinstance(th, dict) and any(k.startswith('memory_') for k in th))
    per_path_disk = isinstance(cfg.get('disk_paths'), dict)
    common_stop = {'live_terminals_gte'} | (set() if per_path_disk else {'disk_free_gib_lt'})
    common_throttle = {'swap_used_gib_gt', 'load_per_cpu_gt', 'live_terminals_gte'} | (
        set() if per_path_disk else {'disk_free_gib_lt'})
    stop_keys = common_stop | ({'memory_psi_full_avg10_gt', 'memory_available_gib_lt'} if linux else {'pressure_level_gte', 'free_memory_pct_lt'})
    throttle_keys = common_throttle | ({'memory_psi_some_avg10_gt', 'memory_available_gib_lt'} if linux else {'pressure_level_gte', 'free_memory_pct_lt'})
    for section, keys in (('stop', stop_keys), ('throttle', throttle_keys),
                          ('heavy', {'stale_hours', 'lock_timeout_s'}),
                          ('probe_timeouts', {'sysctl', 'memory_pressure'})):
        data = cfg.get(section, {} if section == 'probe_timeouts' else None)
        if not isinstance(data, dict):
            raise BadConfig(section + ':not_object')
        optional = {'slots'} if section == 'heavy' else set()
        extra = set(data) - keys - optional
        missing = keys - set(data) if section != 'probe_timeouts' else set()
        if extra or missing:
            raise BadConfig(section + ':' + ('unknown_key:' + sorted(extra)[0] if extra else 'missing_key:' + sorted(missing)[0]))
        for key, value in data.items():
            if key in optional:  # heavy.slots: an integer 1..SS.HEAVY_MAX_SLOTS, default 1
                if isinstance(value, bool) or not isinstance(value, int):
                    raise BadConfig(section + '.' + key + ':not_integer')
                if not 1 <= value <= SS.HEAVY_MAX_SLOTS:
                    raise BadConfig(section + '.' + key + ':out_of_range')
                continue
            numeric = not isinstance(value, bool) and isinstance(value, (int, float))
            try:
                finite = numeric and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise BadConfig(section + '.' + key + ':not_finite_number')
            if value < 0 or (section in ('heavy', 'probe_timeouts') and value <= 0):
                raise BadConfig(section + '.' + key + ':out_of_range')
    for key in ('about', 'cao_url', 'disk_path'):
        if key in cfg and (not isinstance(cfg[key], str) or not cfg[key]):
            raise BadConfig(key + ':not_text')
    if 'cao_url' in cfg and not is_loopback_api(cfg['cao_url']):
        raise BadConfig('cao_url:not_loopback')
    if 'disk_paths' in cfg:
        paths = cfg['disk_paths']
        if isinstance(paths, list):
            if not paths:
                raise BadConfig('disk_paths:not_nonempty_list')
            if any(not isinstance(path, str) or not path for path in paths):
                raise BadConfig('disk_paths:not_text')
        elif isinstance(paths, dict):
            if not paths:
                raise BadConfig('disk_paths:not_nonempty_map')
            for path, rules in paths.items():
                if not isinstance(path, str) or not path:
                    raise BadConfig('disk_paths:not_text')
                if (not isinstance(rules, dict)
                        or set(rules) != {'stop_lt', 'throttle_lt'}):
                    raise BadConfig(f'disk_paths.{path}:rules')
                for key, value in rules.items():
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) or value < 0):
                        raise BadConfig(f'disk_paths.{path}.{key}:out_of_range')
                if rules['stop_lt'] > rules['throttle_lt']:
                    raise BadConfig(f'disk_paths.{path}:threshold_order')
        else:
            raise BadConfig('disk_paths:not_nonempty_list')
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Machine resource gate: GO / THROTTLE / STOP.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--config", "--resources", default=os.path.join(
        KIT, "resources.linux.json" if sys.platform == "linux" else "resources.json"))
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
            cfg = validate_config(json.load(fh))
    except BadConfig as exc:
        line = f"STOP bad_config:{exc}"; print(line); emit_hint(line)
        return EXIT["STOP"]
    except (OSError, ValueError) as exc:
        line = f"STOP unreadable_config:{type(exc).__name__}"; print(line); emit_hint(line)
        return EXIT["STOP"]

    try:
        if a.release_heavy is not None:
            rc, line = release(token, cfg)
            print(line)
            if rc:
                emit_hint(line)
            return rc
        if a.refresh_heavy is not None:
            rc, line = refresh(token, cfg)
            print(line)
            if rc:
                emit_hint(line)
            return rc
    except LockTimeout:
        line = "REFUSED LOCKED state/heavy.lock is held; try again on a later wake"; print(line); emit_hint(line)
        return 1
    except (SS.StateCorrupt, SS.NotWriter) as exc:
        line = f"REFUSED {exc} (report it to the conductor; never take it over)"; print(line); emit_hint(line)
        return 1

    try:
        m, errors = measure(cfg)
    except (OSError, ValueError) as exc:  # e.g. an unreadable fake file
        m, errors = {k: None for k in (LINUX_KEYS if "memory_available_gib_lt" in cfg["stop"] else MAC_KEYS)}, [f"measure:{type(exc).__name__}"]
    verdict, reasons = decide(m, cfg, errors)

    if a.acquire_heavy is not None:
        try:
            rc, line = acquire(a.acquire_heavy, verdict, reasons, cfg)
        except LockTimeout:
            rc, line = 1, "REFUSED LOCKED state/heavy.lock is held; try again on a later wake"
        except (SS.StateCorrupt, SS.NotWriter) as exc:
            rc, line = 1, f"REFUSED {exc} (report it to the conductor; never take it over)"
        print(line)
        if rc:
            emit_hint(line)
        return rc
    if a.json:
        print(json.dumps({"verdict": verdict, "reasons": reasons, "measurements": m}, indent=1))
    else:
        line = f"{verdict} {' '.join(reasons)}".rstrip(); print(line)
        if verdict != "GO":
            emit_hint(line)
    return EXIT[verdict]


if __name__ == "__main__":
    sys.exit(main())
