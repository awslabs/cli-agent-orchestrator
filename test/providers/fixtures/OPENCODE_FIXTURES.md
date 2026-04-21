# OpenCode CLI TUI Fixture Notes

## Source probe captures

All fixtures were derived from tmux capture-pane output against a live
`opencode 1.14.19` session on 2026-04-20, with one file re-captured on 2026-04-20
after a probe-script defect was discovered (see below).

| Fixture | Plain source | ANSI source |
|---|---|---|
| `opencode_cli_idle_splash` | `01-post-launch-plain.txt` | `01-post-launch-ansi.txt` |
| `opencode_cli_processing` | `02-processing-plain.txt` | Re-captured 2026-04-20 (see below) |
| `opencode_cli_completed` | `03-submitted-t12s-plain.txt` | `03-submitted-t12s-ansi.txt` |
| `opencode_cli_permission` | `04-permission-plain.txt` | `05-permission-ansi.txt` |
| `opencode_cli_idle_post_completion` | `07-post-reject-plain.txt` | `03-submitted-t12s-ansi.txt` (reuse — see below) |

## Known reuses and workarounds

### `opencode_cli_processing.ansi.txt` — re-captured

The original upstream probe captured `02-processing-ansi.txt` and
`03-submitted-t12s-ansi.txt` as byte-identical files (same md5).  The PROCESSING
ANSI fixture was re-captured via a fresh tmux probe on 2026-04-20 to produce a
frame that genuinely contains `esc interrupt` in-flight.  The re-captured file has
md5 `9cbe2723ffdab4306032226f7a21ea95`; confirmed distinct from the completed frame
(`b73b1e19…`).

### `opencode_cli_idle_post_completion.ansi.txt` — reuses completed ANSI frame

No ANSI capture exists for the `07-post-reject-plain.txt` (idle-post-completion)
state.  The ANSI fixture reuses `03-submitted-t12s-ansi.txt` (COMPLETED frame).
This is defensible because:
- Both states share the same idle footer (`ctrl+p commands`) and lack `esc interrupt`.
- Phase 3 `get_status()` tests that need to distinguish idle-post-completion from
  COMPLETED should use the **plain** `opencode_cli_idle_post_completion.txt` variant,
  which is a genuine distinct capture.
- The ANSI variant exists only to exercise the ANSI-stripping code path; the
  COMPLETED frame is equally valid for that purpose.

If a genuinely distinct ANSI idle-post-completion frame is needed, re-run the tmux
probe, wait for idle-post-completion state, and overwrite the `.ansi.txt` file.

### `opencode_cli_permission.ansi.txt` — uses "Always allow" sub-confirmation

`05-permission-ansi.txt` (probe) shows the secondary "Always allow" confirmation
dialog rather than the primary "Permission required" dialog in
`04-permission-plain.txt`.  Both are valid WAITING_USER_ANSWER states per §8.3 of
the design doc — the `PERMISSION_PROMPT_PATTERN` regex matches both
`△  Permission required` and `△  Always allow`.
