from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPIKES = ROOT / "spikes"
WEZTERM = Path(r"C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe")
INTERVALS = [0.1, 0.2, 0.5]


def wez(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(WEZTERM), *args], text=True, capture_output=True, check=check)


def trial(interval: float) -> dict[str, float | int]:
    pane_id = wez(
        "cli",
        "spawn",
        "--new-window",
        "--",
        "bash",
        "-lc",
        "printf 'SHELL_READY\\n'; exec bash",
        check=True,
    ).stdout.strip()
    marker = f"SPIKE-MARKER-{time.time_ns()}"
    stop = threading.Event()
    seen = {"detected_at": None, "polls": 0}

    def poller() -> None:
        while not stop.is_set():
            text = wez("cli", "get-text", "--pane-id", pane_id, check=True).stdout
            seen["polls"] += 1
            if marker in text and seen["detected_at"] is None:
                seen["detected_at"] = time.perf_counter()
            time.sleep(interval)

    thread = threading.Thread(target=poller, daemon=True)
    cpu_before = time.process_time()
    wall_start = time.perf_counter()
    thread.start()
    ready_deadline = time.perf_counter() + 8
    while time.perf_counter() < ready_deadline:
        text = wez("cli", "get-text", "--pane-id", pane_id, check=True).stdout
        if "SHELL_READY" in text:
            break
        time.sleep(0.1)
    time.sleep(interval * 2)
    send_returned_at = time.perf_counter()
    wez("cli", "send-text", "--pane-id", pane_id, "--no-paste", f"echo {marker}\n", check=True)

    deadline = time.perf_counter() + 10
    while seen["detected_at"] is None and time.perf_counter() < deadline:
        time.sleep(0.01)
    first_detection_ms = round(((seen["detected_at"] or time.perf_counter()) - send_returned_at) * 1000, 1)

    burst_markers = [f"BURST-{idx}-{time.time_ns()}" for idx in range(10)]
    burst_script = "; ".join(f"echo {m}; sleep 0.05" for m in burst_markers)
    wez("cli", "send-text", "--pane-id", pane_id, "--no-paste", burst_script + "\n", check=True)
    time.sleep(max(2, interval * 15))
    final_text = wez("cli", "get-text", "--pane-id", pane_id, check=True).stdout
    miss_count = sum(1 for m in burst_markers if m not in final_text)
    stop.set()
    thread.join(timeout=2)
    cpu_after = time.process_time()
    wall_elapsed = max(time.perf_counter() - wall_start, 0.001)
    wez("cli", "kill-pane", "--pane-id", pane_id, check=False)
    return {
        "interval_ms": int(interval * 1000),
        "first_detection_ms": first_detection_ms,
        "cpu_percent": round(max(cpu_after - cpu_before, 0.0) / wall_elapsed * 100, 2),
        "polls": int(seen["polls"]),
        "miss_count": miss_count,
    }


def main() -> int:
    result_path = SPIKES / "03-result.md"
    trials = [trial(interval) for interval in INTERVALS]
    recommended = min((t for t in trials if t["miss_count"] == 0), key=lambda x: x["first_detection_ms"], default=None)
    verdict = "GO" if recommended else "NEEDS-WORKAROUND"
    body = [
        "# Spike 3 Result",
        "",
        f"- Verdict: **{verdict}**",
        f"- Recommended interval: `{recommended['interval_ms']} ms`" if recommended else "- Recommended interval: none",
        "",
        "## Measurements",
        "",
        "| Interval | First detection (ms) | CPU % | Poll count | Miss count |",
        "|---|---:|---:|---:|---:|",
    ]
    for t in trials:
        body.append(
            f"| {t['interval_ms']} ms | {t['first_detection_ms']} | {t['cpu_percent']} | {t['polls']} | {t['miss_count']} |"
        )
    body.extend(
        [
            "",
            "## Raw JSON",
            "```json",
            json.dumps(trials, indent=2),
            "```",
            "",
        ]
    )
    result_path.write_text("\n".join(body), encoding="utf-8")
    print(f"spike3 verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
