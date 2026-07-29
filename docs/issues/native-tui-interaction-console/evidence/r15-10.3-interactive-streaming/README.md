# r15 — §10.3 interactive-streaming installed live-provider acceptance evidence

Run date: 2026-07-29 (r1 repair head). Branch: `feature/native-tui-console-lane-c`.
Harness: `test/e2e/test_interactive_streaming_live.py` (pytestmark `e2e`).
Result: **12 passed** (8 kimi + 4 claude) in 228.68 s, plus the r16
menu-evidence round: `r15-kimi-09` menu safe-cancel and `r15-kimi-10`
steer effect pass on kimi 0.29.2 (73.6 s with the claude falsification
probe), and `r16-claude-05` records the live-proven claude 2.1.220
queued-command limit (35.1 s conversion run at the same head).

Scope (design §6.7, cond-0194, r15 owner override): a declared
`payload_class: "interactive"` batch bypasses **only** the provider
IDLE/COMPLETED turn-state refusal and the kimi dispatch grace — never the
pane-input lease, identity/socket/native-session proof, copy-mode guard,
journal/exact-id/no-resend discipline, write deadline, or admission caps.
Only the armed manual streaming capture declares it; automation stays
readiness-gated.

## Provenance

Same harness discipline as the Lane C §10.6 bundle (private tmux socket,
per-run `CAO_STATE_ROOT`, the operator's real `$HOME` for provider auth,
`lanec-acceptance` profile, disposable managed native-TUI sessions):

| fact | value |
|---|---|
| kimi binary | `/opt/homebrew/bin/kimi`, banner `0.29.2` (the pinned build) |
| claude binary | `<HOME>/.local/bin/claude`, banner `2.1.220 (Claude Code)` (the pinned build) |
| turn driver | a slow counting prompt (undeclared prose, sent while idle), with `_turn_active` observed before every mid-turn case and Escape+settle after |

Command:

```
CAO_LANE_C_EVIDENCE_DIR=<run evidence dir> \
  uv run pytest -m e2e test/e2e/test_interactive_streaming_live.py -v --no-cov
```

All home paths, scratch paths, machine tmp-root prefixes, and
account-identifying strings are redacted (`<HOME>`, `<SCRATCH>`,
`<STATE_ROOT>`, `<TMUX_SOCKDIR>`, `<HOST_TMP>`, `<ACCOUNT>`, …). The live
generator itself redacts the machine temp root in both its raw and
resolved forms plus the harvested account display names (r1 hardening);
the regression lives in `test/e2e/test_evidence_sanitizer.py`. No
credential or secret value appears anywhere in the bundle.

## Cases

- **r15-kimi-01 capability and gate**: the per-terminal, build-exact
  identity block advertises `interactive_streaming: {supported: true}`;
  during an active turn the same-shaped **undeclared** batch is still
  `refused/pane-busy` — the inheritance fence holds (automation never
  inherits the bypass).
- **r15-kimi-02 interactive text**: declared interactive text types into
  the mid-turn composer and one Enter submits it — both batches
  `accepted` with `request_schema_version: 4`; the marker is in the
  transcript.
- **r15-kimi-03 navigation**: declared Down/Up/Escape batches deliver
  mid-turn (`accepted`).
- **r15-kimi-04 steer**: declared `C-s` chord delivers mid-turn
  (`accepted`).
- **r15-kimi-05 lease contention**: with the arbiter's flock for the
  exact pane held cross-process by the test, the declared interactive
  POST is `refused/pane-busy` with the §6.4 discriminator detail
  ("input lease is held by another process") — the bypass never skips a
  real lease owner (deterministic, no timing race).
- **r15-kimi-06 stale identity**: a tampered `terminal_generation` is a
  zero-byte `refused` (stale-generation/identity-mismatch); the marker
  never appears.
- **r15-kimi-07 copy mode**: with the pane in copy mode, the declared
  interactive batch is `refused/copy-mode-active` — fail-closed per r15,
  zero bytes, and `pane_in_mode` is still `1` after the refusal (the
  operator's copy mode was never exited by a machine write; the legacy
  undeclared auto-exit is preserved for undeclared batches).
- **r15-kimi-08 killed response**: the interactive POST is written over a
  raw socket that closes **without reading**; the journal reaches its
  terminal state (verified on local journal evidence, not the API), then
  exactly **one** exact-id `GET /control-input/{control_id}` reconciles
  `accepted` and the marker appears **exactly once** — no resend, no
  duplicate provider write.
- **r15-claude-01**: the same paired gate: undeclared `pane-busy` mid-turn;
  declared interactive text+Enter `accepted` with the marker in the
  transcript.
- **r15-claude-02**: declared Down/Up/Escape deliver mid-turn.
- **r15-claude-03**: tampered identity and copy mode are the same
  zero-byte refusals as kimi (copy mode fail-closed, mode untouched).
- **r15-claude-04**: the same killed-response drill: settle proven on
  local journal evidence, one exact-id GET → `accepted`, marker exactly
  once.
- **r15-kimi-09 menu safe-cancel** (Fable r15 P1, r16 design head
  `cc52f89`): during the active turn, declared `/model`+Enter opens the
  picker (`20-menu-open.txt`), Down+Up navigates without changing the
  setting, Escape closes the overlay; the shim `config.toml` model and
  the `K2.7 Coding thinking` status line are unchanged, the
  identity/provider-controls reread is intact, and the counting turn
  keeps progressing after the cancel (`60-turn-continued.txt`).
- **r15-kimi-10 steer effect** (Fable r15 P2; predicate strengthened per
  Sol r16 P1.2): a unique queued instruction mid-turn + declared `C-s` —
  the pending queue empties into the turn context and **freshly
  generated provider-output rows** carry the unique requested suffix
  (`2 ZQe4598a` … `9 ZQe4598a` in `40-provider-acted.txt`; `notes.md`
  counts 8 effect rows). The numeric-suffix shape excludes the
  instruction row, queue/context echoes, and pre-steer capture, so a
  bare accepted chord or an echoed instruction can never satisfy it.
- **r16-claude-05 queued-command limit** (r16, live-proven provider
  behavior — not a CAO defect): during the active turn, declared `/model`
  is accepted and **visibly queued** in the native composer
  (`10-command-visibly-queued.txt`), no model/effort menu opens mid-turn
  (`notes.md`), the counting turn continues and completes
  (`20-turn-continued.txt`, `30-after-turn-completed.txt`), the setting
  is unchanged (`~/.claude.json` model key, `sonnet-5 │ xhigh` status,
  identity reread), and the control/journal evidence claims bytes
  delivered / a queued command only — never menu execution or
  cancellation. A future build that opens a real menu here fails the
  test loudly for re-pinning.

## Browser status evidence (stubbed backend)

`web/e2e/streaming-interactive.spec.ts` (desktop 1280×800, mobile
390×844, **8 passed**) captures the dashboard states:
`web/e2e/screenshots/{desktop,mobile}-chromium-interactive-armed-accepted.png`
(armed banner + accepted trace), `…-interactive-pause-recovered.png`
(turn-gate pause notice, then recovery — the `detail`-field
normalization), `…-interactive-lease-contention-disarm.png` (truthful
disarm on real lease contention), `…-interactive-old-server.png` (no
declaration + the honest notice). Provider/model settings were never
changed.
