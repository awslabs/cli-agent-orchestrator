# Composer fixtures (issue #78 recovery)

Real `tmux capture-pane -p` snapshots (150x50, escapes stripped, the same view
`get_history(strip_escapes=True, visible_only=True)` gives
`extract_current_composer`) of a TUI holding an UNSUBMITTED message: the text
was pasted with `tmux paste-buffer -p` (bracketed paste) and Enter was never
sent. This is what the pane looks like after the swallowed Enter in #78.

Captured 2026-10-21 on macOS, tmux 3.8:

| File | TUI | Composer content |
|------|-----|------------------|
| `claude_code_composer_single_line.txt` | Claude Code 2.1.302 (Bedrock) | one line, fits the row |
| `claude_code_composer_wrapped.txt` | Claude Code 2.1.302 | one long line, wrapped over two rows |
| `claude_code_composer_multi_line.txt` | Claude Code 2.1.302 | three pasted lines (not collapsed to a paste token) |
| `kiro_cli_tui_composer_single_line.txt` | kiro-cli 2.33.1, agent `kiro_default` | one line |
| `kiro_cli_tui_composer_wrapped.txt` | kiro-cli 2.33.1 | one long line, wrapped over two rows |
| `kiro_cli_tui_composer_multi_line.txt` | kiro-cli 2.33.1 | three pasted lines |

The pasted texts:

- `MSG1: refactor module A so the retry policy lives in one place`
- `MSG2: write tests for module B covering the retry policy, the backoff
  schedule, the jitter bounds, the max-attempts ceiling, the circuit breaker
  reset, and the metrics emitted on every attempt so the dashboard can show
  failures per minute`
- `Run the migration once.` / `Then report the row counts.` / `Do not retry on failure.`

Kiro's header row (`<agent> · <model> · ◔ N% · <auth>`) differs between these
captures and the 2.25 fixtures (`developer · auto · ◔ 11% · auth ok`), so the
composer reader anchors on the last separator and the `›` row, not the header.

## Status-area fixtures (PR #975, P2-1)

Real `tmux capture-pane -p` snapshots (80x45) of Claude Code 2.1.308 (Bedrock)
captured 2026-10-23, confirming how the TUI renders the rows between the
conversation and the input box while a turn runs. This build has no TodoWrite,
so no native task list could be captured live; the task-row glyphs in
`STATUS_AREA_ROW_PATTERN` rest on the two public frames
(anthropics/claude-code#52688 at 2.1.119, anthropics/claude-code#10174).

| File | Shows |
|------|-------|
| `claude_code_974_queued_rows_mid_turn.txt` | mid-turn: a `⎿ $ sleep 60` tool row, one queued message (column-0 `❯ …` + `ctrl+x ctrl+s to send now`), the live `✶ Skedaddling…` spinner below them, a `⎿ Tip:` row, and the input box with the `❯ Press up to edit queued messages` placeholder |
| `claude_code_974_tool_row_queued_spinner.txt` | same turn later: a `● Waiting 60 seconds · 49s` tool row, two queued messages, the spinner on the `*` glyph frame |
| `claude_code_974_turn_ended_queued_in_transcript.txt` | the finished turn at 80 columns: delivered queued messages sit in the transcript as column-0 `❯ …` rows, the end-of-turn summary is the newest row above the now-empty box |

Observed: at 2.1.308 queued messages render at column 0 ABOVE the spinner (the
2.1.119 frame had an indented `❯ /effort` BELOW it), the queued-box placeholder
replaces the empty `❯ ` prompt, and the spinner row stayed short enough at 80
columns that wrapping was not observed.

All three are LOADED BY TESTS — `TestLiveCaptureFixtures` in
`test/providers/test_claude_code_unit.py` drives each frame through both
detector paths, with and without a recorded dispatch. They were added as
documentation only, and CI therefore never saw that the wrapped second row of
their `⎿ Tip:` line hid the live spinner (PR #975 review, P2-2). A capture in
the tree that no test loads cannot catch a regression, so keep them wired in.

Known gap, not caused by #974/#975: the RAW `get_status` path reads `completed`
on the two mid-turn frames, because `NEW_TUI_BOX_PATTERN` needs an empty `❯`
row and the `❯ Press up to edit queued messages` placeholder fills it, so the
raw walk never finds the input box. `TestLiveCaptureFixtures` asserts that
verdict as-is rather than skipping it, so the gap is recorded; accepting the
placeholder is a follow-up.
