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
