# TSK-070 — Spike 2b (Codex launch args) + Gemini re-check

You are executing TSK-070, Phase 1b of PRJ-042 (aws-cao WezTerm port). You have no prior conversation context; this prompt is fully self-contained.

## Context

TSK-068 ran 4 spikes testing WezTerm CLI as a replacement for tmux in CAO (`awslabs/cli-agent-orchestrator`). Results are at `C:\dev\aws-cao\spikes\01-result.md` through `04-result.md` + `SUMMARY.md`. Substrate verdict: GO (spikes 1, 3). Two unknowns remain:

1. **Codex launch exits immediately.** `wezterm cli spawn --new-window -- codex` produced `"Process codex in domain local didn't exit cleanly. Exited with code 1."` before the TUI rendered. Spikes 2 and 4 couldn't get Codex into a testable state.
2. **Gemini was not installed** when TSK-068 ran. It's now installed on marcwin (`gemini --version` should work).

Close both before Phase 2 planning.

## Working dir & branch

- `C:\dev\aws-cao` — already on branch `wezterm-multiplexer`
- Phase 0 deliverable: `docs/multiplexer-api-surface.md`
- Phase 1 outputs: `spikes/01-result.md` … `04-result.md`, `SUMMARY.md`
- CAO's existing Codex provider source (the ground truth for Codex-under-tmux args): `src/cli_agent_orchestrator/providers/codex.py`

## Part A — Spike 2b: Codex launch args under wezterm

**Goal:** find the exact command line that keeps `codex` alive in a wezterm pane long enough to accept a slash command via `wezterm cli send-text`.

**Investigation steps:**

1. **Read CAO's Codex provider.** Open `src/cli_agent_orchestrator/providers/codex.py`. Find the method that starts codex under tmux (likely `initialize()` or similar). Note the exact command it constructs — flags, agent profile injection, initial system prompt, `--yolo`, anything else. Also check how it handles startup prompts (trust dialog, etc.).

2. **Reproduce those args under `wezterm cli spawn`.** Start with the CAO-equivalent command, spawn in a new wezterm window, observe what happens. Examples to try (adapt based on step 1 findings):
   - `wezterm cli spawn --new-window -- codex --yolo`
   - `wezterm cli spawn --new-window -- codex --yolo "Hello from wezterm"` (initial prompt)
   - Wrap in a shell to keep the pane alive: `wezterm cli spawn --new-window -- bash -lc "codex --yolo; exec bash"` (so if codex exits, the pane persists)
   - Try without `--new-window` in case of window-related issues
   - Check codex's own help: `codex --help` to discover flags CAO might be using

3. **Once codex stays alive,** measure:
   - Time from spawn to TUI-ready state (ms) — detect by polling `wezterm cli get-text` for codex's prompt pattern
   - Does `wezterm cli send-text --pane-id <ID> --no-paste -- '/help\n'` produce visible output?
   - Does `wezterm cli send-text --pane-id <ID> -- '/help\n'` (bracketed-paste mode) produce visible output?
   - Note: codex may NOT have a `/help` — try whatever is the simplest testable slash command (maybe `/status` or just a regular text prompt like "say hello")

4. **Extract the pattern.** What did CAO's codex provider do differently from a naive `codex` invocation? How should the WezTerm backend construct its codex-spawn command?

**Deliverable:** `spikes/02b-codex-launch.md` with:
- The exact command line that works (verbatim, copy-pasteable)
- Why the naive `codex` spawn exited (your best hypothesis from the investigation)
- TUI-ready latency in ms
- Send-text verdict (A/B/both/neither) — same shape as spike 2
- A diff snippet showing how `WezTermMultiplexer` should construct codex's spawn command, vs what tmux currently does. Pattern it on CAO's existing provider code.

## Part B — Gemini re-check

**Goal:** now that gemini is installed, fill the gaps in spikes 2 and 4 for gemini only.

**Steps:**

1. Verify gemini is on PATH: `gemini --version`
2. **Spike 2 for gemini:** spawn gemini in a wezterm pane (use `--new-window`), wait for TUI, try both send-text modes (`--no-paste` and default) with a slash command gemini supports (check `gemini --help` first — might be `/help`, might be `/` something else).
3. **Spike 4 for gemini:** capture gemini's TUI output via `wezterm cli get-text`, extract gemini's idle and permission-prompt regex patterns from `src/cli_agent_orchestrator/providers/gemini_cli.py`, check whether those regexes match the captured text.
4. **Append** (don't rewrite) a new section `## Gemini (re-tested after install)` to each of `spikes/02-result.md` and `spikes/04-result.md`. Include the same evidence shape as the other providers.

## Part C — Update SUMMARY.md

After Parts A and B are complete, update `spikes/SUMMARY.md`:
- Change spike 2's verdict from NEEDS-WORKAROUND to the real post-investigation verdict (GO / NEEDS-WORKAROUND with specifics)
- Change spike 4's verdict similarly
- Add a new row for "2b — Codex launch args" with its own verdict
- Update the "Phase 2 implication" column per spike

## Constraints (HARD)

- Only modify files under `C:\dev\aws-cao\spikes\` — do NOT touch `src/`
- Commit each finding atomically with `rtk git` prefix:
  - `spike(2b): <verdict> — <summary>` (the 02b-codex-launch.md file)
  - `spike(2): add gemini post-install findings` (update 02-result.md)
  - `spike(4): add gemini post-install findings` (update 04-result.md)
  - `spike(summary): incorporate 2b and gemini findings`
- Push to origin/wezterm-multiplexer at the end
- DO NOT install packages or modify wezterm config
- If codex still doesn't cooperate after you've exhausted reasonable options (try ~5 approaches), stop and document findings — don't chase it for hours
- Prefer `--new-window` for all spawned panes to keep Marc's working panes undisturbed; kill panes when done

## Reporting

Print a tight summary to stdout at the end:
- Spike 2b verdict + the working codex command line
- Gemini: spike 2 verdict, spike 4 verdict
- Updated overall SUMMARY.md snapshot (the 5-row table)
- Anything surprising

Begin.
