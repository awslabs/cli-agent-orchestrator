# Spike batch — Phase 1 of PRJ-042 (aws-cao WezTerm port)

You are executing Phase 1 of a fork of `awslabs/cli-agent-orchestrator` (CAO). You have no prior conversation context — this prompt is fully self-contained.

## Background

CAO is AWS Labs' CLI Agent Orchestrator: a supervisor AI CLI spawns worker AI CLIs (Claude Code, Codex, Gemini CLI) inside tmux windows, drives them via `tmux send-keys` + `tmux capture-pane`, and preserves each worker's TUI for interactive dialog. It's tmux-only and has no Windows support.

We're porting it to run on Windows by replacing tmux with WezTerm (which has a CLI usable from Windows/macOS/Linux). Phase 0 (already complete) produced `docs/multiplexer-api-surface.md` enumerating CAO's `tmux_client` API surface — read that file first, it's the ground truth for what the WezTerm backend must implement.

Your job NOW is Phase 1: validate four unknowns about WezTerm's CLI before any abstraction work starts. Each spike is a throwaway script + a result markdown file with a binary verdict.

## Repo state

- Working dir: `C:\dev\aws-cao`
- Branch: `wezterm-multiplexer` (already checked out)
- Phase 0 deliverable: `C:\dev\aws-cao\docs\multiplexer-api-surface.md` (READ THIS FIRST)
- Spike workdir: `C:\dev\aws-cao\spikes\` — create scripts and results here
- Platform: Windows (marcwin), Git Bash for shell, WezTerm running as the GUI terminal
- WezTerm CLI is on PATH as `wezterm` (verify with `wezterm --version`)
- AI CLIs available on PATH for spike 2: `claude`, `codex`, `gemini` (verify each with `--version`)

## Constraints (HARD)

- Only modify files under `C:\dev\aws-cao\spikes\` and `C:\dev\aws-cao\docs\` — do NOT touch source files under `src/`
- Each spike commits independently with a clear message: `spike(N): <verdict> — <one line summary>`
- Use `rtk` prefix on git commands (e.g., `rtk git add`, `rtk git commit`, `rtk git push`) per the user's CLAUDE.md
- Push to origin/wezterm-multiplexer when all 4 spikes are done
- Spike scripts are throwaway — bash or PowerShell or Python, whichever is fastest. Don't engineer them.
- DO NOT install any dependencies beyond what's already on the system
- DO NOT modify wezterm config (`wezterm.lua`)
- If a spike needs to create wezterm panes, prefer `--new-window` so they're isolated from the user's working panes (less disruptive); kill panes on exit

## The 4 spikes

### Spike 1 — WezTerm send-text + get-text round-trip
**Question:** Does the WezTerm CLI work at all on marcwin for our use case?
**Test:**
1. `wezterm cli spawn --new-window -- bash` — capture the new pane-id from stdout
2. `wezterm cli send-text --pane-id <ID> --no-paste -- 'echo hello-from-spike\n'`
3. Wait 500ms
4. `wezterm cli get-text --pane-id <ID>` — verify "hello-from-spike" appears in output
5. `wezterm cli kill-pane --pane-id <ID>`
**Result file:** `spikes/01-result.md`
**Verdict:** GO / NO-GO / NEEDS-WORKAROUND
**If NO-GO, abort the rest** — substrate is broken, no point continuing.

### Spike 2 — Paste-mode behavior with each AI CLI's TUI
**Question:** Will `wezterm cli send-text` correctly deliver input to Ink-based TUIs (Claude Code, Codex, Gemini)? CAO's tmux uses paste-buffer + `paste-buffer -p` to wrap text in bracketed-paste sequences (`\x1b[200~ ... \x1b[201~`) which bypasses TUI hotkey interception. WezTerm needs an equivalent.
**Test (per CLI in {claude, codex, gemini}):**
1. Spawn the CLI in a new wezterm pane (e.g., `wezterm cli spawn --new-window -- claude`)
2. Wait for the TUI to fully render (~3 seconds — adjust if needed)
3. Try sending `/help\n` two ways:
   - **A:** `wezterm cli send-text --pane-id <ID> --no-paste -- '/help\n'`
   - **B:** `wezterm cli send-text --pane-id <ID> -- '/help\n'` (default, which IS bracketed-paste in WezTerm)
4. After each, wait 2s, capture pane via get-text, check whether the slash command was accepted (look for help output)
5. Kill the pane between attempts to start clean
**Result file:** `spikes/02-result.md`
**Verdict:** Per-CLI table: `{claude: A|B|both|neither, codex: ..., gemini: ...}` plus a recommended default for the WezTerm backend
**If `neither` for any CLI:** that's NEEDS-WORKAROUND — document the failure mode (e.g., "claude eats `/` because of input mode X")

### Spike 3 — Polling latency for `pipe_pane` substitute
**Question:** WezTerm has no continuous-stream-to-file equivalent. CAO uses `tmux pipe-pane` to log all pane output to a file, then a watchdog watches the file for state-detection patterns. We need to replace this with polling on `wezterm cli get-text`. Is polling fast enough?
**Test:**
1. Spawn a wezterm pane running `bash`
2. Start a Python (or bash) loop that calls `wezterm cli get-text --pane-id <ID>` every 100ms / 200ms / 500ms (run three trials)
3. While polling, send a known marker: `wezterm cli send-text --pane-id <ID> --no-paste -- 'echo SPIKE-MARKER-$(date +%N)\n'`
4. Measure: (a) time-to-first-detection of the marker (ms after send-text returned), (b) CPU% of the polling loop (rough — `Get-Process` snapshot or `time` if Python), (c) any output that get-text *missed* between polls (send 10 markers in quick succession with `sleep 0.05` between, then verify all 10 appear in the polled buffer)
5. Repeat at all three intervals
**Result file:** `spikes/03-result.md`
**Verdict:** GO with recommended interval / NEEDS-WORKAROUND (specify the WezTerm Lua hook fallback design if polling is too slow)
**Required:** concrete numbers (latency in ms, CPU%, miss-count)

### Spike 4 — `get-text` ANSI / regex compatibility
**Question:** CAO providers detect state with regexes like `_permission_prompt_pattern = r'Allow this action\? \[y/n/t\]:'` and idle patterns. These were tuned against `tmux capture-pane -p` output. Does `wezterm cli get-text` produce text that matches the same patterns? ANSI escape handling may differ.
**Test:**
1. Read the actual regex patterns from `src/cli_agent_orchestrator/providers/claude_code.py`, `codex.py`, `gemini_cli.py` — extract the idle pattern and any prompt patterns
2. For each of {claude, codex, gemini}: spawn the CLI in a wezterm pane, do something that triggers each pattern (idle = wait after spawn; permission prompt = ask the CLI to do something it'll prompt for; for codex use a path it doesn't have access to so it asks)
3. Capture pane output via `wezterm cli get-text --pane-id <ID>` and run the regexes against it
4. Compare against `wezterm cli get-text --pane-id <ID> --escapes` (raw ANSI) if needed to understand any normalization
**Result file:** `spikes/04-result.md`
**Verdict:** Per-CLI per-pattern table: `{claude.idle: matches, claude.permission: matches with patch X, codex.idle: ...}` — list any regex patches needed, formatted as a unified diff snippet ready for Phase 2

## Reporting

After all 4 spikes complete:

1. Each `spikes/0N-result.md` exists with verdict + evidence
2. Final commit + push to `origin/wezterm-multiplexer`
3. Write `spikes/SUMMARY.md` with a 4-row table:

| # | Spike | Verdict | Key finding | Phase 2 implication |
|---|---|---|---|---|

4. Print to stdout (so it lands in the codex exec output file):
   - One-line per-spike verdict
   - The single biggest risk for Phase 2 implementation
   - Anything that surprised you and the doc didn't anticipate

## Order

1. Spike 1 (gating — abort all if NO-GO)
2. Spike 2, 3, 4 — these are independent, run in any order, parallelize internally if comfortable
3. SUMMARY.md
4. Commit + push everything

Begin.
