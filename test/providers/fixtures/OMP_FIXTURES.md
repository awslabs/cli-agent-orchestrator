# OMP TUI Fixture Notes

## Source probe captures

These sanitized fixtures were captured from a live `omp/17.2.10` interactive
session on 2026-08-07. `tmux capture-pane -e -p` supplied the rendered fixtures;
`tmux pipe-pane` supplied `omp_processing.raw.txt` to preserve cursor redraw
bytes that capture-pane composites away.

```sh
omp --version  # captured: omp/17.2.10
mkdir -p /tmp/cao-omp-fixtures
tmux new-session -d -s cao-omp-fixtures -x 160 -y 48 -c /tmp/cao-omp-fixtures omp
tmux capture-pane -e -p -t cao-omp-fixtures
tmux send-keys -t cao-omp-fixtures 'Reply with exactly ACK and do not use any tools.' Enter
tmux new-session -d -s cao-omp-approval -x 160 -y 48 -c /tmp/cao-omp-fixtures 'omp --approval-mode always-ask'
tmux pipe-pane -o -t cao-omp-fixtures 'tee -a /tmp/cao-omp-fixtures/omp-raw.log'
```

| Fixture | Captured state |
| --- | --- |
| `omp_idle.txt` | Fresh interactive session |
| `omp_processing.txt` | Model generation (`Working… ⟨esc⟩`) |
| `omp_completed.txt` | Completed assistant responses |
| `omp_waiting.txt` | `Allow tool: bash` approval dialog |
| `omp_error.txt` | Invalid-model runtime error |
| `omp_prose.txt` | Completed prose containing `Error:`, `working`, and `cancel` |
| `omp_processing.raw.txt` | Pipe-pane cursor redraw sample |

The saved copies replace account/model identity, local paths, token usage, and
terminal title data with neutral values. They retain only UI markers, prompts,
responses, and cursor-control sequences relevant to provider detection.

## OMP 18.x captures

`omp18_*.txt` were captured from a live `omp/18.20.0` interactive session on
2026-11-15 with `tmux capture-pane -e -p` (160x48), using the same prompts as
above plus a bash-tool prompt (`sleep 12`) for the tool-running state. OMP 18
changed the TUI in three ways the provider has to tolerate alongside 17.x:

- The working row leads with the Escape keycap and is followed by the verb or
  the current tool intent: `⎋ Working…`, `⎋ Run requested delay` (17.x:
  `Working… ⟨esc⟩`, `Running … ⟨esc⟩`). The spinner and elapsed time moved to
  the status row.
- The status row is `╭── π > … ╮` with `composer.shape: box`, and an unframed
  ` π > … ` row over a bare `╰─` with the default `composer.shape: band`
  (17.x: `╭── OMP session … ╮` over `╰─ … ─╯`).
- The user-turn background comes from the theme: `48;2;15;18;22` under the
  default `titanium` theme (17.2.10 capture: `48;2;5;5;5`).

| Fixture | Captured state |
| --- | --- |
| `omp18_box_processing.txt` | Model generation, box composer |
| `omp18_box_completed.txt` | Completed response, box composer |
| `omp18_band_idle.txt` | Fresh session, default band composer |
| `omp18_band_processing.txt` | Model generation, band composer |
| `omp18_band_tool_running.txt` | Bash tool running, band composer |
| `omp18_band_completed.txt` | Completed response, band composer |

Scrubbing: the working directory is replaced with `/tmp/cao-omp-fixtures`, the
model name with `Model`, and the subscription marker is removed.
