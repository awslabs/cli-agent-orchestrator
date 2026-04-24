from __future__ import annotations

import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPIKES = ROOT / "spikes"
WEZTERM = Path(r"C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WEZTERM), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def wait_for_text(pane_id: str, needle: str, timeout_s: float = 5.0) -> str:
    deadline = time.time() + timeout_s
    latest = ""
    while time.time() < deadline:
        latest = run("cli", "get-text", "--pane-id", pane_id).stdout
        if needle in latest:
            return latest
        time.sleep(0.2)
    return latest


def main() -> int:
    started_at = time.time()
    result_path = SPIKES / "01-result.md"
    pane_id = None
    verdict = "NO-GO"
    summary = "WezTerm CLI round-trip failed before validation completed."
    evidence = []
    try:
        version = run("--version").stdout.strip()
        spawn = run("cli", "spawn", "--new-window", "--", "bash", "-lc", "printf 'SHELL_READY\\n'; exec bash")
        pane_id = spawn.stdout.strip()
        evidence.append(f"- `spawn` pane id: `{pane_id}`")
        ready_text = wait_for_text(pane_id, "SHELL_READY", timeout_s=8)
        evidence.append(f"- shell ready marker observed: `{'SHELL_READY' in ready_text}`")

        send = run(
            "cli",
            "send-text",
            "--pane-id",
            pane_id,
            "--no-paste",
            "echo hello-from-spike\n",
        )
        evidence.append(f"- `send-text` exit code: `{send.returncode}`")
        text = wait_for_text(pane_id, "hello-from-spike", timeout_s=5)
        contains = "hello-from-spike" in text
        evidence.append(f"- `get-text` contains marker: `{contains}`")
        evidence.append("```text\n" + text.strip() + "\n```")

        if contains:
            verdict = "GO"
            summary = "spawn/send-text/get-text/kill-pane all worked with a standalone WezTerm window."
    except subprocess.CalledProcessError as exc:
        evidence.append(f"- command failed: `{exc.cmd}`")
        evidence.append(f"- return code: `{exc.returncode}`")
        if exc.stdout:
            evidence.append("```text\n" + exc.stdout.strip() + "\n```")
        if exc.stderr:
            evidence.append("```text\n" + exc.stderr.strip() + "\n```")
    finally:
        if pane_id:
            run("cli", "kill-pane", "--pane-id", pane_id, check=False)

    duration_ms = round((time.time() - started_at) * 1000)
    body = "\n".join(
        [
            "# Spike 1 Result",
            "",
            f"- Verdict: **{verdict}**",
            f"- Summary: {summary}",
            f"- WezTerm binary: `{WEZTERM}`",
            f"- WezTerm version: `{locals().get('version', 'unavailable')}`",
            f"- Duration: `{duration_ms} ms`",
            "",
            "## Evidence",
            *evidence,
            "",
        ]
    )
    result_path.write_text(body, encoding="utf-8")
    print(f"spike1 verdict={verdict}")
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
