# TSK-075 — Task 4 result

## Files touched
- `src/cli_agent_orchestrator/multiplexers/__init__.py`
- `test/multiplexers/test_selection.py`
- `spikes/TSK-075-result.md`

## Selection branches verified
| Signals | Platform | Selected backend |
| --- | --- | --- |
| `CAO_MULTIPLEXER=tmux` (even with tmux/wezterm env present) | any | `tmux` |
| `CAO_MULTIPLEXER=wezterm` | any | `wezterm` |
| `CAO_MULTIPLEXER=foo` | any | `ValueError` |
| `TMUX=/tmp/tmux-1000/default,1234,0` | any | `tmux` |
| `WEZTERM_PANE=66` | any | `wezterm` |
| `TERM_PROGRAM=WezTerm` | any | `wezterm` |
| no env signals | `win32` | `wezterm` |
| no env signals | `linux` | `tmux` |
| repeated call in same process | any | same cached instance |
| fresh test after cache clear | any | cache empty before selection |

## Tests
- `test_selection.py`: 10 pass / 0 fail
- full (excl. e2e): 1049 pass / 43 fail — must be ≤43

## Lazy-import behavior
Confirm: `import cli_agent_orchestrator.multiplexers` works without Task 5's `wezterm.py` present. WezTerm is only resolved when `get_multiplexer()` selects the `wezterm` branch.

## Deviations
- No code deviations from the task brief.
- Full-suite failures remained at the allowed ceiling of 43; they were not changed by this task.
