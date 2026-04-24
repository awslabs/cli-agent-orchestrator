from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPIKES = ROOT / "spikes"
WEZTERM = Path(r"C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe")
WORKDIR = str(ROOT)

TARGETS = {
    "claude": {
        "file": ROOT / "src" / "cli_agent_orchestrator" / "providers" / "claude_code.py",
        "command": ["claude"],
        "patterns": [
            "IDLE_PROMPT_PATTERN",
            "TRUST_PROMPT_PATTERN",
            "BYPASS_PROMPT_PATTERN",
        ],
    },
    "codex": {
        "file": ROOT / "src" / "cli_agent_orchestrator" / "providers" / "codex.py",
        "command": ["codex"],
        "patterns": [
            "IDLE_PROMPT_PATTERN",
            "TRUST_PROMPT_PATTERN",
            "WAITING_PROMPT_PATTERN",
            "CODEX_WELCOME_PATTERN",
        ],
    },
    "gemini": {
        "file": ROOT / "src" / "cli_agent_orchestrator" / "providers" / "gemini_cli.py",
        "command": ["gemini"],
        "patterns": [
            "IDLE_PROMPT_PATTERN",
            "WELCOME_BANNER_PATTERN",
            "RESPONDING_WITH_PATTERN",
        ],
    },
}


def wez(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(WEZTERM), *args], text=True, capture_output=True, check=check)


def exists(cmd: str) -> bool:
    return subprocess.run(["bash", "-lc", f"command -v {cmd}"], capture_output=True).returncode == 0


def extract_constants(path: Path, names: list[str]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for name in names:
        match = re.search(rf"^{name}\s*=\s*r([\"'])(.*?)\1", text, flags=re.MULTILINE)
        if match:
            found[name] = match.group(2)
    return found


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def capture_runtime(cli: str, command: list[str]) -> tuple[str, str]:
    pane_id = None
    try:
        pane_id = wez(
            "cli",
            "spawn",
            "--new-window",
            "--cwd",
            WORKDIR,
            "--",
            *command,
            check=True,
        ).stdout.strip()
        time.sleep(5)
        plain = wez("cli", "get-text", "--pane-id", pane_id, check=True).stdout
        escaped = wez("cli", "get-text", "--pane-id", pane_id, "--escapes", check=True).stdout
        return plain, escaped
    finally:
        if pane_id:
            wez("cli", "kill-pane", "--pane-id", pane_id, check=False)


def main() -> int:
    result_path = SPIKES / "04-result.md"
    body = ["# Spike 4 Result", ""]
    summary_bits: list[str] = []
    diff_snippets: list[str] = []
    needs_workaround = False

    for cli, meta in TARGETS.items():
        constants = extract_constants(meta["file"], meta["patterns"])
        body.extend([f"## {cli}", f"- Source: `{meta['file'].relative_to(ROOT)}`"])
        for name in meta["patterns"]:
            body.append(f"- `{name}` = `{constants.get(name, 'NOT FOUND')}`")

        if not exists(cli):
            body.append(f"- Runtime probe: blocked; `{cli}` executable unavailable.")
            summary_bits.append(f"{cli}: blocked")
            needs_workaround = True
            continue

        plain, escaped = capture_runtime(cli, meta["command"])
        clean_plain = strip_ansi(plain)
        clean_escaped = strip_ansi(escaped)
        body.append(f"- Plain capture length: `{len(clean_plain)}`")
        body.append(f"- Escaped capture length: `{len(escaped)}`")

        matched = []
        missing = []
        body.append("")
        body.append("| Pattern | Plain | `--escapes` |")
        body.append("|---|---|---|")
        for name in meta["patterns"]:
            pattern = constants.get(name)
            if not pattern:
                body.append(f"| `{name}` | missing | missing |")
                missing.append(name)
                continue
            plain_match = bool(re.search(pattern, clean_plain, re.MULTILINE))
            escaped_match = bool(re.search(pattern, clean_escaped, re.MULTILINE))
            body.append(f"| `{name}` | `{plain_match}` | `{escaped_match}` |")
            if plain_match or escaped_match:
                matched.append(name)
            else:
                missing.append(name)

        excerpt = "\n".join(clean_plain.strip().splitlines()[-18:])
        body.extend(["", "```text", excerpt, "```", ""])
        if missing:
            needs_workaround = True
            summary_bits.append(f"{cli}: missing {', '.join(missing)}")
            diff_snippets.extend(
                [
                    "```diff",
                    f"--- a/{meta['file'].relative_to(ROOT).as_posix()}",
                    f"+++ b/{meta['file'].relative_to(ROOT).as_posix()}",
                    "@@",
                    f"-# Existing WezTerm probe did not match: {', '.join(missing)}",
                    f"+# Phase 2: either normalize WezTerm startup text or broaden these regexes: {', '.join(missing)}",
                    "```",
                    "",
                ]
            )
        else:
            summary_bits.append(f"{cli}: all probed patterns matched")

    verdict = "NEEDS-WORKAROUND" if needs_workaround else "GO"
    body[1:1] = [f"- Verdict: **{verdict}**", f"- Summary: `{'; '.join(summary_bits)}`", ""]
    if diff_snippets:
        body.extend(["## Candidate Regex Patch Notes", *diff_snippets])
    else:
        body.extend(["## Candidate Regex Patch Notes", "- No regex changes suggested from this probe."])
    result_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"spike4 verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
