# TSK-071 — Gemini delegation: tmux-callsite audit (read-only)

You are executing a focused audit of a fork of `awslabs/cli-agent-orchestrator` (CAO). You have no prior conversation context — this prompt is fully self-contained.

## Background

CAO is AWS Labs' CLI Agent Orchestrator: a supervisor AI CLI spawns worker AI CLIs (Claude Code, Codex, Gemini CLI) inside tmux windows, drives them via `tmux send-keys` + `tmux capture-pane`, and preserves each worker's TUI for interactive dialog. It's tmux-only and has no Windows support.

This fork is porting CAO to a multiplexer abstraction so Windows can use WezTerm instead of tmux. Phase 0/1/1b done; Phase 2 implementation is starting now. Phase 2 plan: `docs/PLAN-phase2.md` (binding spec). The plan moves `clients/tmux.py` into a new `multiplexers/tmux.py` behind a `BaseMultiplexer` ABC, and adds a sibling `WezTermMultiplexer`.

**Your job**: enumerate every place in the source tree that depends on tmux, classify each as legitimate or leakage, and produce a single result file. Read-only. No code changes.

## Repo state

- Working dir: `C:\dev\aws-cao`
- Branch: `wezterm-multiplexer` (already checked out)
- Read first: `docs/PLAN-phase2.md` (Phase 2 binding spec) and `docs/multiplexer-api-surface.md` (Phase 0 ground truth on the tmux API surface)
- Audit target: every file under `src/cli_agent_orchestrator/`
- Result file destination: `spikes/TSK-071-result.md`

## Constraints (HARD)

- READ-ONLY on `src/`. Do NOT modify any source file. Do not run formatters.
- The only file you create is `spikes/TSK-071-result.md`. Nothing else.
- Do not commit, push, or branch. The supervising session handles git.
- Do not install dependencies. Do not run pytest.
- If a tool you need is missing, write a short note in the result and continue.

## What to enumerate

Scan `src/cli_agent_orchestrator/` for all of the following and produce one combined list:

1. **Imports of `tmux_client`** — the singleton from `cli_agent_orchestrator.clients.tmux`. Both `from ... import tmux_client` and module-level `tmux_client.<x>(...)` usages.
2. **Imports of `libtmux`** anywhere outside `clients/tmux.py`.
3. **Direct `tmux` subprocess invocations** — search for `subprocess.run(["tmux"`, `subprocess.run(["tmux"`, `subprocess.Popen(["tmux"`, `os.system("tmux`, `shell=True` calls containing `tmux ` as a literal, and equivalents.
4. **`tmux send-keys -l` / `paste-buffer` / `capture-pane` / `pipe-pane`** literal strings anywhere in source.
5. **Any reliance on `TMUX` env var** (e.g. `os.environ["TMUX"]`).
6. **Hard-coded shell tooling assumed Unix-only** — `tail`, `cat`, `which`, `grep` invoked via `subprocess` from CAO source. (Plan §4 already calls out `inbox_service._get_log_tail`'s `tail -n` — confirm and find any others.)

## Classification

For each finding, classify as exactly one of:

- **LEGIT** — already inside the multiplexer boundary or its tests. Acceptable: anything inside `src/cli_agent_orchestrator/clients/tmux.py`, anything inside `src/cli_agent_orchestrator/multiplexers/` (does not exist yet but plan introduces it), and tests under `test/clients/test_tmux*.py` / `test/multiplexers/`. We are NOT auditing tests in this pass — only source.
- **PROVIDER-EXPECTED** — known tmux leakage Phase 2 plan §3/§5 already calls out: `providers/claude_code.py:204-224` raw `tmux send-keys -l "\x1b[B"` plus libtmux `pane.send_keys`, and `providers/codex.py:233-240` libtmux trust-path Enter. Cite the file:line and confirm the plan's count is accurate.
- **HIDDEN-LEAKAGE** — anything else. These are the bugs Phase 2 risks missing. For each, show 2-3 lines of context and explain why it isn't in the multiplexer/provider exception list.
- **UNIX-TOOLING** — non-tmux Unix command invocations from source (#6 above) that would break on Windows under the WezTerm backend.

## Output format

Single file `spikes/TSK-071-result.md` with this exact structure:

```markdown
# TSK-071 — tmux-callsite audit result

## Summary
- Total findings: <N>
- LEGIT: <n>
- PROVIDER-EXPECTED: <n>
- HIDDEN-LEAKAGE: <n>
- UNIX-TOOLING: <n>

## HIDDEN-LEAKAGE (review required)
<table or list, file:line + 2-3 lines context + why it's leakage>

## PROVIDER-EXPECTED (confirmed against plan)
<file:line + brief — one line each>

## UNIX-TOOLING (Windows risk)
<file:line + tool used + suggested replacement strategy>

## LEGIT (count only)
Count and the directories covered. Don't enumerate.

## Verdict for Phase 2 scope
One paragraph: does the plan cover everything, or are there hidden couplings that need to be added to the Phase 2 task list?
```

## Reporting back

Print the Verdict paragraph to stdout at the end (so it lands in the dispatch log). Do not print the full file contents to stdout — they go to the result file.

## Order

1. Read `docs/PLAN-phase2.md` and `docs/multiplexer-api-surface.md`.
2. Grep / scan the source tree for the six categories above.
3. Classify each finding.
4. Write `spikes/TSK-071-result.md`.
5. Echo the verdict paragraph.

Begin.
