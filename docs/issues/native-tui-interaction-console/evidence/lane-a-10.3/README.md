# Lane A — §10.3 installed-provider live acceptance evidence

Run date: 2026-07-28. Branch: `feature/native-tui-console-lane-a`.
Harness: `test/e2e/test_native_tui_provider_acceptance.py` (pytestmark `e2e`).

## Provenance

| fact | value |
|---|---|
| kimi binary | `/opt/homebrew/bin/kimi` → `…/@moonshot-ai/kimi-code/dist/main.mjs`, sha256 `2ee6e2f1…f532db`, banner `0.29.2` |
| claude binary | `<HOME>/.local/bin/claude` → `<HOME>/.local/share/claude/versions/2.1.220`, sha256 `8addc857…`, banner `2.1.220 (Claude Code)` |
| tmux | `/opt/homebrew/bin/tmux` (private socket per run, PATH shim for the server) |
| cao-server | subprocess with the operator's real `$HOME` (provider auth) and `CAO_STATE_ROOT` pointed at a per-run tmp dir (all CAO state isolated) |
| kimi provider home | shim: symlinks to real `~/.kimi-code` entries **except `config.toml`, which is a copy** — `/model` persists never touch the operator's real config (verified: md5 of the real file identical before/after all runs) |
| agent profile | `acceptance` (no MCP servers, no tool grants), written to the isolated state root's agent store |
| launches | v2 reserve → launch → bind, `execution_mode: native_tui`, per-provider disposable tmux session + git worktree |

## Command and outcome

```
CAO_LANE_A_EVIDENCE_DIR="$PWD/docs/issues/native-tui-interaction-console/evidence/lane-a-10.3" \
  uv run pytest -m e2e test/e2e/test_native_tui_provider_acceptance.py -v --tb=short -p no:cacheprovider
```

Three rounds, same day:

- **Round 1: 14 passed, 1 failed in 133 s** (`run/round-1/`). The one
  failure was intentional: case-08 was **IMPOSSIBLE** on the installed kimi
  build (F1 below) and the test was written to stay red until the pin matched
  the build it names.
- **Round 2 (after the F1 pin correction): 15 passed in 140 s.** The kimi
  emptiness pin was corrected from this evidence to the installed rounded-box
  composer; case-08 then passed with the declared `/compact` **accepted** on a
  proven-empty composer, and case-07's refusal reads the proven-nonempty
  variant. The red-by-design regression guard stays in the test.
- **Round 3 (r11 declared-command two-close): 15 passed in 149 s.** The r11
  repair: declared commands require both the emptiness pin and the
  command-execution observation pin pre-write, and close `accepted` only with
  the execution signal observed and its evidence journaled. The kept evidence
  from this round is exactly the two-close proof: case-08 and case-12's
  accepted records carry `submission_observed: submitted` and a
  `capture-pane:` evidence reference, replayed by exact id (earlier rounds
  recorded `submission_observed: null` — the PR #48 defect the r11 review
  flagged). Other cases' regenerated churn was restored per the steer-040
  scope guard.

## Case index

| case | test | outcome | evidence |
|---|---|---|---|
| launch (kimi) | fixture | PASS — reserve→launch→bind `bound` (~5 s) | `launch-kimi/` |
| launch (claude) | fixture | PASS after answering the workspace-trust dialog (below) | `launch-claude/`, `91-workspace-trust-dialog-2.txt` |
| identity (kimi) | `TestKimiIdentitySurface` | PASS — build-exact `provider_controls` (steer `['C-s']`, grace 5000 ms), guard advertised, schema `[1,2,3,4]` | `case-00-kimi-identity/` |
| identity (claude) | `TestClaudeIdentitySurface` | PASS — steer `[]`, guard advertised | `case-00-claude-identity/` |
| 10 codex | `TestCodexAdvertisement` | PASS — no `codex` key in `provider_controls`; keys exactly `[claude_code, kimi_cli]` | `case-10-codex-advertisement/` |
| 1 model menu (fused) | `test_01_fused_model_menu_sequence` | PASS with **deviation D1** | `case-01-kimi-model-menu/` |
| 2 reasoning menu | `test_02_reasoning_level_navigation` | PASS with **deviation D2** | `case-02-kimi-reasoning-menu/` |
| 7 Home/End vs menu | `test_03_home_end_against_model_menu` | PASS — **limitation L1** | `case-03-kimi-home-end-menu/` |
| 5 cadence + grace | `test_04_human_cadence_batches_and_dispatch_grace` | PASS | `case-04-kimi-cadence-grace/` |
| 3 steer mid-turn | `test_05_steer_mid_turn` | PASS with **deviation D3** | `case-05-kimi-steer/` |
| 4 escape interrupt | `test_06_escape_interrupts_streaming` | PASS — transcript shows `Interrupted by user` | `case-06-kimi-escape/` |
| 6a guard (kimi) | `test_07_declared_compact_refused_with_prefill` | PASS — **proven-nonempty** refusal after the F1 correction, prefill byte-identical, zero bytes | `case-07-kimi-guard-prefill/` |
| 6b compact executes (kimi) | `test_08_declared_compact_executes_on_empty_composer` | PASS — accepted **with execution evidence** (`submitted` + `capture-pane:` ref, r11) | `case-08-kimi-compact-executes/` |
| 6c slash carrier (kimi) | `test_09_undeclared_slash_text_is_prose` | PASS — undeclared `/tmp/x` delivered as prose | `case-09-kimi-slash-carrier/` |
| 8a guard (claude) | `test_01_declared_compact_refused_with_prefill` (claude) | PASS — **proven-nonempty** refusal, styled composer region byte-identical | `case-11-claude-guard-prefill/` |
| 8b compact executes (claude) | `test_02_declared_compact_executes_on_empty_composer` (claude) | PASS — accepted **with execution evidence** (`submitted` + `capture-pane:` ref, r11) | `case-12-claude-compact-executes/` |
| 9 escape (claude) | `test_03_escape_interrupts_active_turn` | PASS — spinner `· Ideating…` gone after Escape | `case-13-claude-escape/` |

## F1 — RESOLVED: the kimi composer-emptiness pin was corrected from this evidence

Round 1 proved the server's §4.1 pin (then `native_pane_input._KIMI_INPUT_RULE`,
locating the composer by a `── input ──` rule from older in-tree fixture
captures) did not match the **installed** 0.29.2, whose bundle renders the
composer as an **untitled rounded box** (`╭─…─╮` / `│ > … │` / `╰─…─╯`) and
contains no `── input ──` string at all (verified against `dist/main.mjs`,
sha256 `2ee6e2f1…`). Consequence in round 1: emptiness was unprovable, so every
declared command failed **closed** (`composer-nonempty`, zero bytes — safe,
never a wrong delivery; case-08's `03-declared-compact-response.json` from that
round is preserved in `run/round-1/`).

Per the spec's evidence discipline ("an entry whose evidence fails live
acceptance is removed or corrected, never approximated"), the pin was corrected
to the rounded-box region determination (`kimi-composer-box`), live-verified in
round 2: case-08's declared `/compact` is **accepted** on a proven-empty
composer and the transcript shows the compaction UI; case-07's refusal reads
the proven-nonempty variant with the prefill byte-identical. kimi 0.29.0/0.29.1
are deliberately **unpinned** (declared commands refuse `provider-unsupported`)
because only 0.29.2's composer is live-verified — the older builds are pinned
only when their own live evidence exists. The claude 2.1.220 pin matched from
the start (cases 11/12).

## D1 — the §10.3 fused menu form races the picker mount (kimi)

`text("/model") enter up*3 enter` as one fused batch is **accepted** (all events
`sent`) but does not deterministically change the model: the picker mounts after
the `/model` Enter, and the Up/Enter keys land before the mount — the capture
shows the picker open with the selection still at the current row and no
`Switched to …` status (`case-01-kimi-model-menu/04-after-fused.txt`). The paced
form (case-04) **is** deterministic: separate batches produced
`Switched to K2.7 Coding Highspeed with thinking max.`
(`case-04-kimi-cadence-grace/08-after-enter-history.txt`).

## D2 — reasoning-level navigation on kimi 0.29.2

`/model` + `left*2 right enter` produced **no** thinking/effort change: the
current model (K2.7 Coding) renders `Thinking [ On ]  Off (Unsupported)` — a
single selectable segment, so Left/Right are no-ops, and Enter re-saved the
same value (`Saved K2.7 Coding with thinking max as default.` — written to the
shim config copy, not the operator's). The installed bundle also exposes a
separate `/effort` command (`handleEffortCommand`); the reasoning control is
not where §10.3's menu path assumes. See `case-02-kimi-reasoning-menu/`.

## D3 — mid-turn steer: the fused v3 form is gate-refused; the v2 steer control delivers

- Fused v3 `[text "Please reconsider this path", chord C-s]` mid-turn →
  **refused `pane-busy`** ("the receiver is processing, not idle…"), zero bytes
  — any composer-class event readiness-gates the whole batch
  (`case-05-kimi-steer/03-fused-steer-response.json`).
- Bare v3 `[chord C-s]` mid-turn → accepted (interrupt-class, exempt).
- The deployed v2 steer control `{"text": …, "chord": "C-s", "enter": false}`
  mid-turn → **accepted**, `chord_sent: true`, and the transcript shows the
  steer land while the turn was still streaming: `✨ Please reconsider this path`
  (`case-05-kimi-steer/06-v2-steer-response.json`, `07-after-steer-history.txt`).
  The kimi TUI itself advertises this path mid-turn: `Tip: ctrl-s to add
  guidance without waiting for the turn to finish`.

## L1 — Home/End are ignored by the kimi /model menu (F4/OD5)

With the picker open and the selection moved to row 3 (`K3`), `Home` and `End`
(both accepted at the wire) left the selection unmoved — captures
`03-after-down-down` ≡ `05-after-home` ≡ `07-after-end`
(`case-03-kimi-home-end-menu/`). The keys arrive byte-exact (the §10.2 suite
proves `ESC[1~`/`ESC[4~`); this build's picker simply does not consume them.
Home/End do work in the kimi composer line editor (case-09's composer clear
used `[Home, Delete×31]` successfully).

## Escape and prefill facts (both providers)

- Prefill **survives one Escape** on **kimi 0.29.2** (`case-07`, captures
  02 vs 04) and on **claude 2.1.220** (`case-11`, styled captures 03 vs 05) —
  the r5 Claude evidence generalizes; blind Escape-clearing would be wrong on
  both.
- `escape` interrupts an active turn on both: kimi shows `Interrupted by user`
  (`case-06`), claude's spinner disappears with no explicit marker
  (`case-13`).
- Operator clear used: `[Home, Delete×31]` (kimi case-09, claude case-12) —
  accepted and verified; no blind server-side clearing anywhere.

## Claude launch note: workspace-trust dialog

On 2.1.220, `--dangerously-skip-permissions` does **not** bypass the
first-run workspace-trust dialog ("Quick safety check … ❯ 1. Yes, I trust this
folder"). In an untrusted directory it blocks startup before SessionStart, so
the launch preflight fails at 90 s (`launch-claude/90-pane-1-on-launch-failure.txt`
from the first attempt). The dialog cannot be answered through control-input
(Enter is composer-class and a permission prompt is exactly what the readiness
gate refuses). The harness answers it as an operator keystroke via tmux during
the launch window (`launch-claude/91-workspace-trust-dialog-2.txt`), which
records claude's standard trust entry for the disposable worktree — the same
state any interactive first run would write.

## Sanitization

All captures pass through a redaction table before being written: the real
`$HOME` and user name (`<HOME>`, `<USER>`), scratch/state paths (`<SCRATCH>`,
`<STATE_ROOT>`, `<WORKTREE-*>`, `<TMUX_SOCKDIR>`), and account-identifying
tokens harvested from local config files (`<ACCOUNT>`). No secret values
appear anywhere in this directory; env vars are referred to by name only.
