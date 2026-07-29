# Lane C — §10.6 installed live-provider acceptance evidence

Run date: 2026-07-29 (r1 repair head). Branch: `feature/native-tui-console-lane-c`.
Harness: `test/e2e/test_operator_message_live.py` (pytestmark `e2e`).
Result: **7 passed** (6 kimi + 1 claude) in 102.09 s, including the r1
killed-response case (kimi-06).

## Provenance

| fact | value |
|---|---|
| kimi binary | `/opt/homebrew/bin/kimi`, banner `0.29.2` (the pinned build) |
| claude binary | `<HOME>/.local/bin/claude`, banner `2.1.220 (Claude Code)` (the pinned build) |
| tmux | private socket per run (PATH shim for the server); no pane on the operator's shared server is addressable |
| cao-server | subprocess with the operator's real `$HOME` (provider auth) and `CAO_STATE_ROOT` pointed at a per-run tmp dir — every CAO state artifact (SQLite, manifest, staged attachments, pane locks) is this run's |
| kimi provider home | shim: symlinks to real `~/.kimi-code` entries except `config.toml`, which is a copy |
| agent profile | `lanec-acceptance` (no MCP servers; deliberately **without** a "never use tools" instruction — the kimi image proof requires the provider to invoke its own `ReadMediaFile`) |
| launches | v2 reserve → launch → bind, `execution_mode: native_tui`, per-provider disposable tmux session + git worktree |
| fixture | real 120×80 PNG generated in-test (left half red, right half blue — the r3 fixture shape) |

Command:

```
CAO_LANE_C_EVIDENCE_DIR=<run evidence dir> \
  uv run pytest -m e2e test/e2e/test_operator_message_live.py -v --no-cov
```

All home paths, scratch paths, and account-identifying strings are redacted
(`<HOME>`, `<SCRATCH>`, `<STATE_ROOT>`, `<TMUX_SOCKDIR>`, `<HOST_TMP>`,
`<ACCOUNT>`, …). `<HOST_TMP>` covers the machine-specific
`/private/var/folders/.../T` prefix of the per-run pytest tmp dir (the TUI's
box-width truncation made the exact-path redaction miss those fragments in
the first committed bundle — repaired in the r1 sanitation pass, which also
replaced the personal first name in the Claude welcome banner with
`<ACCOUNT>`). No credential or secret value appears anywhere in the bundle.

## Cases

- **kimi-01 capability blocks** (no artifacts): the live per-terminal
  `GET /terminals/{id}/control-identity` advertises the §8.6
  `operator_message` (8192 B, multiline, 4 attachments) and `image`
  (PNG-only, `staged-path-text`, the pinned `ReadMediaFile` directive
  template) blocks for kimi 0.29.2.
- **kimi-02 non-PNG refusal** (`kimi-02-non-png/`): a JPEG upload to a
  PNG-only provider is HTTP 422 `{outcome: refused, reason_code:
  attachment-type-unsupported}` with a durable `failed` record — unproven
  formats are refused, never converted.
- **kimi-03 staged PNG** (`kimi-03-staged-png/`): upload 201 `ready`
  (120×80 sniffed from content) → operator-message submit `accepted` →
  the transcript shows the pinned directive template with the staged
  absolute path, then `Used ReadMediaFile (…/attachments/<terminal>/<id>.png)
  · image`, then the model's correct observation: "The left half is red
  and the right half is blue." (The upstream capability was proven at
  round 3; this case proves the Lane C *server path*: upload → token
  substitution → typed operator message.)
- **kimi-04 long text** (`kimi-04-long-text/`): a >512-byte text-only
  operator message is accepted through the build-proven composer plan and
  reaches the transcript (unique marker observed).
- **kimi-05 at-most-once** (`kimi-05-at-most-once/`): an identical
  same-id re-POST replays the journaled answer (`outcome: accepted,
  replayed: true`) with zero new bytes; a divergent same-id POST is
  `refused/request-rebound`; the exact-id reconcile returns the same
  journaled record; the transcript contains the marker **exactly once**
  (see `notes.md`) — no duplicate provider submission.
- **kimi-06 killed response** (`kimi-06-killed-response/`, r1): the submit
  POST is written over a raw socket that closes **without reading** — the
  response is provably lost mid-submit while the server completes the
  write — then one exact-id `GET /operator-message/{operation_id}`
  reconciles to the journaled `accepted` answer and the transcript
  contains the marker **exactly once** (see `notes.md`). This is the
  §10.6 dropped/killed-response acceptance drill, not an ordinary replay.
- **claude-01 staged PNG** (`claude-01-staged-png/`): upload 201 →
  operator-message submit `accepted` → the bare staged path (claude's
  documented reference form) reaches the composer and the provider reads
  the file: "Left half is red, right half is blue."
