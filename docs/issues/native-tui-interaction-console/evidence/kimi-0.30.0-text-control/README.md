# cond-0198 — Kimi 0.30.0 exact-build text/control live acceptance evidence

Run date: 2026-07-29. Branch: `feature/native-tui-console-lane-c`.
Harness: `test/e2e/test_kimi_0300_text_control_live.py` (pytestmark `e2e`).
Result: **6 passed** in 150.04 s on the REAL installed Kimi 0.30.0 binary
(`/opt/homebrew/bin/kimi`, banner `0.30.0`, bundle
`dist/main.mjs` sha256
`49ad0553cff0b5f60f83ba85df56bb5ccdbcb908158c80d9363d0e5a529ea51c`).

Scope (cond-0198): the operator's Kimi auto-updated to 0.30.0. Exact-set
pins were added — `SUPPORTED_VERSIONS`/`PINNED_VERSIONS` entries,
`_PROVEN_COMPOSER_NEWLINE["0.30.0"]`, `_PROVEN_STEER_CHORDS["0.30.0"]` —
and this suite is the live proof behind them: the real build accepts and
executes the pinned text/control flow with truthful outcomes. **Image
delivery authority stays pinned to 0.29.2 only** — no image block is
advertised for 0.30.0 and no image proof was requested or run.

Command:

```
CAO_LANE_C_EVIDENCE_DIR=<run evidence dir> \
  uv run pytest -m e2e test/e2e/test_kimi_0300_text_control_live.py -v --no-cov
```

Same harness discipline and redaction vocabulary as the Lane C §10.6 and
r15 bundles (`<HOME>`, `<SCRATCH>`, `<STATE_ROOT>`, `<TMUX_SOCKDIR>`,
`<HOST_TMP>`, `<ACCOUNT>`). No credential or secret value appears anywhere.

## Cases

- **kimi-0300-01 capability**: the per-terminal, build-exact block
  advertises `operator_message` (8192 B, multiline, 4 attachments),
  `interactive_streaming: {supported: true}`, and `steer_chords: ["C-s"]`
  — and NOT the `image` block.
- **kimi-0300-02 v1 text**: the pinned control-input v1 text+Enter flow is
  accepted and the unique marker reaches the transcript.
- **kimi-0300-03 multiline operator message**: a two-line operator message
  is accepted through the proven C-j composer plan; both unique line
  markers reach the transcript.
- **kimi-0300-04 interactive bypass + fence**: during the active turn the
  same-shaped UNDECLARED batch is still `refused/pane-busy` (inheritance
  fence), while the declared interactive batch is accepted with schema v4
  and its marker reaches the mid-turn composer.
- **kimi-0300-05 steer effect**: a unique instruction queued mid-turn +
  declared `C-s` → the provider consumes the steer and acts on it: the
  exact `STEER-ACK-…` line appears as its own fresh provider-output row
  (`● STEER-ACK-…` in `30-provider-acted.txt`). The exact-row predicate
  cannot be satisfied by the instruction row, a wrapped queue/composer
  echo, or pre-steer content, and no second prompt carries the token —
  causality is the steer alone.
- **kimi-0300-06 image refused**: a PNG upload on 0.30.0 is 422
  `refused/provider-unsupported` — image delivery authority stays pinned
  to 0.29.2; nothing staged.
