# Spike 1 Result

- Verdict: **GO**
- Summary: spawn/send-text/get-text/kill-pane all worked with a standalone WezTerm window.
- WezTerm binary: `C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe`
- WezTerm version: `wezterm 20260331-040028-577474d8`
- Duration: `3312 ms`

## Evidence
- `spawn` pane id: `17`
- shell ready marker observed: `True`
- `send-text` exit code: `0`
- `get-text` contains marker: `True`
```text
SHELL_READY
marc@mafewin:/mnt/c/Users/marc$ echo hello-from-spike
hello-from-spike
marc@mafewin:/mnt/c/Users/marc$
```
