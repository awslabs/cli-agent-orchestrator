# TSK-069 — Phase 2 implementation plan for PRJ-042

You are Codex, running in `codex exec --yolo --skip-git-repo-check` mode with no prior conversation context. Execute this task end-to-end: read inputs, write the plan, commit, push. Everything you need is in this prompt and in the repo at your CWD (`C:\dev\aws-cao`, branch `wezterm-multiplexer`).

## Background (why this exists)

PRJ-042 ports AWS's `cli-agent-orchestrator` (CAO) from tmux-only to a pluggable multiplexer backend with **WezTerm as the first non-tmux target**. Motivation: CAO's tmux dependency blocks Windows-native use and rules out rich-TUI agents (Claude Code, Codex, Gemini CLI) whose interactive panes don't survive tmux. WezTerm's CLI gives us the same primitives (spawn panes, send text, capture output) with native Windows support and no alt-screen interference.

- **Phase 0 (TSK-067, DONE):** catalogued CAO's multiplexer API surface — 14 methods, 11 active. See `docs/multiplexer-api-surface.md`.
- **Phase 1 (TSK-068, DONE):** four spikes validated WezTerm CLI as substrate. Results in `spikes/*-result.md` + `spikes/SUMMARY.md`. Verdict: GO.
- **Phase 1b (TSK-070, DONE):** follow-up Codex-on-Windows launch shim + send-text-doesn't-submit findings. See `spikes/02b-codex-launch.md`.
- **Phase 2 (THIS TASK):** design the actual implementation — `BaseMultiplexer` interface, `TmuxMultiplexer` refactor of existing code, new `WezTermMultiplexer`, per-provider regex patches.
- **Phase 3 (future):** implement Phase 2 plan.

Architecture is locked: **per-project Claude Code session acts as supervisor**, using CAO to drive ephemeral Codex/Gemini workers in WezTerm panes. Supervisor handles routing + dispatch; workers are stateless. No marc-hq-level meta-observer in this PRJ.

## Inputs you MUST read before writing the plan

All paths relative to `C:\dev\aws-cao` (your CWD):

**Design inputs:**
- `docs/multiplexer-api-surface.md` — the 14-method surface you must generalize
- `spikes/SUMMARY.md` — Phase 1 rollup
- `spikes/01-result.md` — spawn + basic send-text
- `spikes/02-result.md` — Claude/Codex/Gemini launch behavior
- `spikes/02b-codex-launch.md` — Codex-on-Windows shim (CRITICAL for the WezTerm backend)
- `spikes/03-result.md` — get-text output format + regex compat
- `spikes/04-result.md` — polling latency (500ms interval, detection latency)
- `spikes/TSK-068-prompt.md`, `spikes/TSK-070-prompt.md` — prior prompts for style reference

**Source inputs (CAO today):**
- `src/cli_agent_orchestrator/clients/tmux.py` — the current multiplexer client (this is what gets split into BaseMultiplexer + TmuxMultiplexer)
- `src/cli_agent_orchestrator/clients/providers/claude_code.py` — especially `_handle_startup_prompts()` (trust prompt, must port verbatim to WezTerm backend)
- `src/cli_agent_orchestrator/clients/providers/codex.py`
- `src/cli_agent_orchestrator/clients/providers/gemini_cli.py`

Read enough of each provider to identify regexes / state-detection patterns that assume tmux output format. The handoff hypothesis is that plain `wezterm cli get-text` output is compatible (validated in spike 03), but per-provider patches may still be needed.

## Key constraints from Phase 1 findings

These MUST be reflected in the plan:

1. **`send_message()` is a two-step primitive on WezTerm.** `wezterm cli send-text` populates the composer but does NOT submit. The backend must: (a) paste text body, (b) inject Enter separately (`wezterm cli send-text $'\r'` or `--no-paste` + key injection). This mirrors CAO's existing tmux `paste-buffer` + `send-keys C-m` split — generalize the two-step pattern into the base interface, not a WezTerm-only hack.

2. **Codex-on-Windows launch requires a shim:**
   ```
   wezterm cli spawn --new-window --cwd <DIR> -- \
     C:\Users\marc\scoop\apps\nodejs-lts\current\bin\codex.cmd \
     -c hooks=[] --yolo --no-alt-screen --disable shell_snapshot
   ```
   The `codex.cmd` path, `hooks=[]`, `--no-alt-screen`, and `--disable shell_snapshot` are all load-bearing. Plan must account for a per-provider launch-command template and a Windows-vs-Unix path resolver.

3. **Claude trust prompt:** port `_handle_startup_prompts()` from `claude_code.py` to work against the WezTerm backend unchanged. Verify the regex still matches `get-text` output.

4. **Polling:** 500ms interval, 0 missed markers at 10-message bursts, 144-207ms detection latency. Adequate replacement for tmux `pipe-pane`. Plan should specify WezTermMultiplexer uses periodic `get-text` diffs instead of a streaming pipe.

5. **Regex compat:** use plain `get-text` mode, NOT `--escapes`. Existing CAO regexes work against plain output.

6. **Gemini not on PATH** on marcwin; wiring Gemini is stretch, not MVP. Plan may defer Gemini backend integration.

## Deliverable — `docs/PLAN-phase2.md`

Structure:

### 1. Executive summary (≤10 lines)
One paragraph: what Phase 2 delivers, rough LoC and day estimate, main risks.

### 2. BaseMultiplexer interface
Full method signatures with docstrings. Derived from `docs/multiplexer-api-surface.md` — same 11 active methods, but with any necessary generalizations (e.g., two-step submit, launch-command templating). Call out which methods are abstract vs. default-implemented.

### 3. TmuxMultiplexer identity refactor
How `clients/tmux.py` becomes `TmuxMultiplexer(BaseMultiplexer)` with behavior unchanged. What moves, what stays. Should be mechanical — explicitly flag any non-trivial behavior change as a risk.

### 4. WezTermMultiplexer — new
Concrete method-by-method design:
- Pane/window model mapping (tmux session/window/pane → wezterm workspace/tab/pane)
- `send_message()` two-step flow with exact commands
- `get_text()` buffer retrieval + polling loop
- Launch command templating with the Codex-on-Windows shim as a worked example
- Claude trust-prompt handler port (reuse vs. re-implement)
- Error handling for unavailable `wezterm` binary

### 5. Per-provider patches
For each of claude_code.py / codex.py / gemini_cli.py: list the regexes or state-detection calls that were inspected, state whether they need patches for the WezTerm backend, and if so what. Based on spike 03 most should pass through unchanged — explicitly say so where applicable.

### 6. Test strategy
How Phase 3 verifies this. Real-WezTerm smoke tests? Mocked multiplexer tests? Existing CAO test harness — does it parameterize cleanly?

### 7. LoC + day estimate
Table: component → lines added / lines moved / days. Be honest: solo maintainer, Windows primary, Claude + Codex MVP only.

### 8. Risks
Ranked list. Must include at minimum: (a) per-provider regex drift not caught in spike 03, (b) Codex `hooks=[]` shim becoming stale if upstream config moves, (c) Gemini-on-Windows-PATH blocker, (d) WezTerm CLI surface changes across versions.

### 9. Out of scope (explicit)
Layer 2 marc-hq meta-observer. Non-WezTerm non-tmux backends. Gemini MVP wiring if you judged it stretch.

## Style rules

- English. Markdown. Code blocks for commands and signatures.
- No ceremony, no roadmap-bureaucracy bullet lists. Terse and load-bearing.
- If a design question genuinely needs human input, park it in a "Decisions deferred" section at the end with options — don't make up an answer.
- Cite files by path + line number where it sharpens a claim.

## Workflow

1. Read every file listed under **Inputs** above.
2. Write `docs/PLAN-phase2.md`.
3. `git add docs/PLAN-phase2.md spikes/TSK-069-prompt.md`
4. `git commit -m "docs(multiplexer): Phase 2 implementation plan (TSK-069)"`
5. `git push origin wezterm-multiplexer`
6. Print a one-paragraph summary of the plan and the commit SHA. Done.

You are on branch `wezterm-multiplexer` already. Don't create a new branch. Don't open a PR — #206 is already open and tracks this branch.
