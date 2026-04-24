from __future__ import annotations

import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPIKES = ROOT / "spikes"
WEZTERM = Path(r"C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe")
WORKDIR = str(ROOT)

CLIS = {
    "claude": ["claude"],
    "codex": ["codex"],
    "gemini": ["gemini"],
}


def run_cmd(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def wez(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd([str(WEZTERM), *args], check=check)


def command_exists(name: str) -> bool:
    return run_cmd(["bash", "-lc", f"command -v {name}"]).returncode == 0


def capture_help(cli: str, paste: bool) -> tuple[bool, str]:
    pane_id = None
    try:
        pane_id = wez(
            "cli",
            "spawn",
            "--new-window",
            "--cwd",
            WORKDIR,
            "--",
            *CLIS[cli],
            check=True,
        ).stdout.strip()
        time.sleep(5 if cli == "gemini" else 4)
        args = ["cli", "send-text", "--pane-id", pane_id]
        if not paste:
            args.append("--no-paste")
        args.append("/help\n")
        wez(*args, check=True)
        time.sleep(3)
        text = wez("cli", "get-text", "--pane-id", pane_id, check=True).stdout
        lowered = text.lower()
        matched = any(
            token in lowered
            for token in [
                "/help",
                "keyboard shortcuts",
                "slash commands",
                "commands",
                "help",
            ]
        )
        return matched, text
    finally:
        if pane_id:
            wez("cli", "kill-pane", "--pane-id", pane_id, check=False)


def main() -> int:
    result_path = SPIKES / "02-result.md"
    rows: list[tuple[str, str, str, str]] = []
    verdict_parts: list[str] = []
    recommended = []
    notes = []

    for cli in ["claude", "codex", "gemini"]:
        if not command_exists(cli):
            rows.append((cli, "blocked", "blocked", "command not installed or not on PATH"))
            verdict_parts.append(f"{cli}: blocked")
            notes.append(f"- `{cli}` could not be tested because the executable is unavailable in this environment.")
            continue

        a_ok, a_text = capture_help(cli, paste=False)
        b_ok, b_text = capture_help(cli, paste=True)

        if a_ok and b_ok:
            verdict = "both"
        elif a_ok:
            verdict = "A"
        elif b_ok:
            verdict = "B"
        else:
            verdict = "neither"

        verdict_parts.append(f"{cli}: {verdict}")
        recommended.append(f"- `{cli}`: prefer `{'default paste' if b_ok and not a_ok else '--no-paste' if a_ok and not b_ok else 'either mode works' if a_ok and b_ok else 'custom workaround needed'}`")
        a_excerpt = "\n".join(a_text.strip().splitlines()[-12:])
        b_excerpt = "\n".join(b_text.strip().splitlines()[-12:])
        rows.append(
            (
                cli,
                "pass" if verdict != "neither" else "fail",
                verdict,
                f"[A --no-paste]\n{a_excerpt}\n\n[B default paste]\n{b_excerpt}",
            )
        )

    verdict = "NEEDS-WORKAROUND" if any("neither" in part or "blocked" in part for part in verdict_parts) else "GO"
    body_lines = [
        "# Spike 2 Result",
        "",
        f"- Verdict: **{verdict}**",
        f"- Per-CLI verdicts: `{', '.join(verdict_parts)}`",
        "- Mode A: `wezterm cli send-text --no-paste -- '/help\\n'`",
        "- Mode B: `wezterm cli send-text -- '/help\\n'`",
        "",
        "## Recommendation",
        *(recommended or ["- No CLI-specific recommendation available."]),
        "",
        "## Evidence",
    ]
    for cli, status, mode, excerpt in rows:
        body_lines.extend(
            [
                f"### {cli}",
                f"- Status: `{status}`",
                f"- Accepted mode: `{mode}`",
                "```text",
                excerpt,
                "```",
            ]
        )
    if notes:
        body_lines.extend(["", "## Environment Notes", *notes])
    result_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    print(f"spike2 verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
