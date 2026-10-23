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
