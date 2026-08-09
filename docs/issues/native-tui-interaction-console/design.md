# Native TUI Interaction Console — Execution Specification

Status: canonical execution specification (supersedes the initial design
brief, reconciled against the deployed source on 2026-07-28; scope addendum
integrated per steers `root-image-composer-addendum-steer-001` and
`root-image-screenshot-path-steer-001`; kimi staged-path image delivery
revised from live proof on pinned 0.29.2 in round
`native-tui-console/spec-kimi-staged-path-r3`; post-review adjudication
applied in round 4 — Sol/xhigh and Fable-5/Ultracode findings F10-F13,
with steers `root-r4-attachment-speed-guard-008` and
`root-r4-fable-delta-gate-009`; command-class guard added in round 5
(F14, live prefilled-composer command-drift evidence); Fable delta-gate
remnant fixed in round 6 (P1-Δ1 stale never-retry parenthetical; P2-Δ1..Δ3
adjudicated per steer `root-r6-p2-cheap-015` — gate-retry interval is a
client-pinned constant, no protocol growth; discriminators pinned, no
typed sub-reason); command declaration carrier added in round 7
(P1-Δ2, root-confirmed: optional `payload_class: "command"` under schema
v4, never shape-derived); Lane B session-card header composition pinned in
round 8 (selectable metadata + dedicated chevron toggle, owner-approved);
Lane A live results reconciled in round 9 (D1 paced menus, D2 no `/model`
reasoning selector on 0.29.2, D3 mid-turn steer forms, L1 Home/End, F1
pin scope, OD2/OD5 resolved live); cond-0031 steer-state plan added in
round 10 (§4.2: state-aware activation, no blind replay, truthful
outcomes); declared-command outcome two-close rule pinned in round 11
(F16, Sol/high PR #48 review: accepted only with execution evidence) —
implementation NOT green-lit; Lane A's PR #48 correction requires a fresh
Fable lead delta gate at the amended head first; cond-0194 interactive
streaming added in round 15 (§6.7, owner override: `payload_class:
"interactive"` under schema v4 bypasses only the turn gate and dispatch
grace — a focused Fable P1 delta gate is required before that amended
contract merges; r16 keeps its acceptance honest per build — Claude
2.1.220 queues `/model`-class commands mid-turn rather than opening a
menu (pinned provider limit, live-proven; Kimi's full menu/steer
acceptance passes); M3 generation-fence amendment recorded on 2026-08-09
(absorbing `generation-fenced` refusal, no post-park provider bytes))

Task ID: `native-tui-console/spec-initial-v1`

This document is the implementable, reviewable specification for the Native
TUI Interaction Console track. It reconciles the initial brief against the
exact deployed source, pins the contract the implementation lanes build
against, and partitions the work P0/P1/P2. Where the brief and the deployed
source disagreed, the source won and the divergence is named in §13
(Unresolved decisions) rather than silently smoothed over.

## 0. Repository, base, and ownership pins

| Pin | Value |
|---|---|
| Repository / design worktree | `/Users/colin/Projects/cli-agent-orchestrator-worktrees/native-tui-console-spec` |
| Deployed base SHA | `82e5d9230d800b80d11b84991f69a3146fb332d6` (`origin/main` at branch time) |
| Design branch | `feature/native-tui-interaction-console` |
| `origin` remote | `https://github.com/colindmurray/cli-agent-orchestrator.git` |
| `upstream` remote | AWS Labs repository (read-only reference) |
| PR target | `origin/main` via the integration branch (§9) |
| Integration head (r14) | `38ad00ee8857acf90fc5bb56a484f8aecc1a079d` on `origin/feature/native-tui-interaction-console` — A+B merged and installed-GO (PR #49 visual-QA fixes, PR #50 390×844 fitted-`.xterm` geometry; installed Sol/xhigh retest P4 GO, bundle `index-DOXq5tKp.js` live on localhost + tailnet); supersedes the r13 row below |
| Integration head (r13) | `ada0d1982cb00dbef5492bc4a1c0d0ed2debfc3e` on `origin/feature/native-tui-interaction-console` — Lane A (PR #48), r9-r12 docs, and Lane B (PR #47, seamed at `6fc348e`, merged 2026-07-28) are all integrated; supersedes the r12 row below |
| Integration head (r12) | `8687af27ee0bcd5d7f37b46f0bee2061260762bf` on `origin/feature/native-tui-interaction-console` (Lane A merged, PR #48); Lane B (PR #47, `8c2d4a0`) serializes next per §9's seam |
| Canonical document | this file, on the design branch |
| Visual baseline for Lane C | `docs/issues/native-tui-interaction-console/baseline-dashboard.png` (operator screenshot of the deployed dashboard, 2026-07-27; **predates the cond-0175 recorder row** — the deployed bundle renders that row whenever the native composer is visible, so the current render has one more control row than the image shows. The image remains authoritative for the compact single-composer footprint; §10.5 visual acceptance measures the current deployed render — F13) |
| Kimi staged-path proof | `docs/issues/native-tui-interaction-console/evidence/kimi-0.29.2/` (sanitized transcript excerpts + fixture, committed at r4; full artifacts remain under `.conductor/reports/r3/`) |

Every file:line reference below was verified against the deployed base SHA
above. If implementation rebases onto a newer `main`, the references must be
re-verified and this section amended.

## 1. Control-compatibility baseline (verified deployed facts)

This section is the ground truth all lanes build on. It exists so a reviewer
can check every claim without re-walking the tree.

### 1.1 The wire contract (server)

`src/cli_agent_orchestrator/services/control_input_contract.py` pins:

- Protocol literal `cao-control-input-v1` (`:82`) and request schema versions
  1 (text+enter), 2 (+`chord`), 3 (ordered `events` array) (`:379-381`,
  `:597-598`). Each version travels under its own digest domain
  (`cao-control-input-request-v1/-v2/-v3`, `:368/:374/:597`), so requests of
  different versions can never collide.
- v3 event types: `text`, `key`, `chord` (`:610-615`).
- **v3 normalized key set today:** `SEQUENCE_KEY_NAMES = {"Escape", "C-c",
  "C-s", "Enter", "Backspace"}` (`:625`). **No navigation keys exist on the
  deployed base.** This is the P1 gap.
- Caps: `MAX_SEQUENCE_EVENTS = 32`, `MAX_SEQUENCE_TEXT_BYTES = 512` (aggregate
  across text events) (`:631-632`). The 512-byte cap is a *contract feature*:
  the deployed 422 refusal reads "a control input is a command or one short
  line, not a document" (Finding F8).
- Typed outcomes `accepted | refused | ambiguous | unsupported` with a closed
  reason set; every reason is bound to exactly one outcome in
  `REASON_OUTCOMES` (`:209-240`). Only `refused` is reattemptable
  (`REATTEMPTABLE_OUTCOMES`, `:98`), because only a refusal is decided before
  the first byte. `ambiguous` is terminal for automation and is reconciled by
  an exact-request-id query, never by re-sending.
- Refusal reasons that already cover this track: `unsupported-chord` (`:172`),
  `unsupported-key` (`:184`), `unrepresentable-event` (`:189`),
  `stale-generation`, `identity-mismatch`, `pane-dead`, `pane-busy`,
  `copy-mode-active`, `write-deadline`, `request-rebound`, `response-lost`,
  `write-incomplete`, `owner-lost-before-write`, `owner-lost-mid-write`.
- **M3 amendment (2026-08-09):** `generation-fenced` is a typed
  `refused` outcome decided under the managed successor → generation fence
  lock before any literal chunk or submit key. It is absorbing for that
  generation: the client must advance to a successor and must not retry the
  parked control id or payload.
- Key/chord membership is deliberately **not** enforced by the digest path
  (`:617-624`): a digest must be computable for a request the server will
  then refuse, so the two sides never disagree about which requests exist.
  Membership is a service-layer, capability-advertised decision — this is
  what makes the §3 in-place key-set extension legal.
- Bracketed-paste sentinels are screened in both ESC and C1 spellings
  (`:792-808`); the control path never emits them.

### 1.2 Delivery path and arbiter

- HTTP routes (`src/cli_agent_orchestrator/api/main.py`):
  `POST /terminals/{id}/control-input` (`:2871`, WRITE/ADMIN scope),
  `GET /control-input/capabilities` (`:2792`, unauthenticated),
  `GET /control-input/{control_id}` (`:2804`, READ+; keyed by control id
  alone, deliberately not terminal-scoped, `:2809-2814`),
  `GET /terminals/{id}/control-identity` (`:2825`), and
  `GET /terminals/{id}/composer-observation`
  (`:3101`, READ+; cond-0324 read-only observation of the pinned composer
  region, advertised only when the terminal is managed, `native_tui`,
  identity-resolved, and the provider/build has a pinned observation
  layout).  Terminal-level failures are `200` with a typed outcome; `422`
  is reserved for shape errors and protocol mismatch; `404` is reserved
  for "server has no control route at all" (contract docstring `:27-33`).
  Composer-observation adds `409` for identity/build drift and
  unsupported/unpinned builds, returning `observed: false` with a typed
  refusal and never raw composer text.
- `deliver_control_input` (`services/control_input_service.py:1327`)
  pipeline: protocol check → v3 either/or shape rule → normalization →
  per-text screening → caller-digest comparison → identity resolution +
  expectation screen → per-event representability → pane/server formation →
  journal intent → replay check → **pane-input lease** → under-lease live
  identity re-proof (pane alive, `window_id`, `pane_pid`, tmux server socket
  — the contract's internal "§24.7" server-identity rule) → copy-mode guard
  → readiness gate → `claim_write` (commits before
  the first byte) → dispatch. **Gate scope (exact):** the readiness gate
  (kimi dispatch grace + live-turn IDLE/COMPLETED observation) runs only on
  the v3-sequence path (`:3195-3232`) and the native-inbox path
  (`:4021-4051`); the v1/v2 path runs the composer/identity preflight but
  no turn-state gate (`_observe_turn_state` has exactly those two call
  sites).
- The pane-input arbiter (`services/pane_input_arbiter.py:170`) is an
  in-process per-pane `threading.Lock` plus a cross-process `fcntl.flock` on
  `CAO_HOME_DIR/pane-input-locks/pane-*.lock`. Non-reentrant by design. No
  TTL — it is a `with`-block lease. Time-bounding: the control-input and
  inbox holders thread `WRITE_DEADLINE_SECONDS = 20.0`
  (`control_input_service.py:150`) into every tmux call as
  `deadline_monotonic`; the legacy send-input holder
  (`terminal_service.py:2418`) does **not** (its tmux subprocess is
  untimed — a deployed fact this track does not change). Contention →
  `PaneBusyError` → journaled `pane-busy` refusal (zero bytes proven,
  reattemptable).
  Current lease holders (exhaustive): control-input delivery
  (`control_input_service.py:1648`), native inbox payloads (`:3983`), and
  ordinary legacy send-input (`terminal_service.py:2418`).
- The journal (`services/control_input_journal.py`, SQLite schema v5 at
  `CAO_HOME_DIR/db/control-input.sqlite3`) records intent before the first
  byte; states `intent → writing → delivered | ambiguous`, `intent →
  refused`, with **no `writing → refused` edge** (`:130-149`).
  `claim_write` is a `BEGIN IMMEDIATE` CAS — exactly one claimant across
  threads and processes. Same-`control_id` semantics (exact, deployed):
  `get(request_id)` lookup is always zero-I/O; an identical POST replays a
  stored **`delivered` or `ambiguous`** terminal answer with zero new I/O;
  an identical POST after **`refused`** takes the deliberate
  `refused → intent` re-arm edge (`:711-742` — the live row is cleared and
  the control may be written, because refusal proves zero bytes) — it does
  **not** replay the refusal; a divergent binding is refused
  `request-rebound`. `sweep_stranded` resolves dead-owner `intent` →
  `refused/owner-lost-before-write` and dead-owner `writing` →
  `ambiguous/owner-lost-mid-write`. `get(request_id)` is the exact-id
  reconciliation query.
- Every tmux write primitive **in the control path** re-proves the pane's
  canonical server socket identity immediately before the first byte
  (`clients/tmux.py:1329-1340`, `:1423-1434`, `:1494-1505`, `:1563-1574`).
  Scope caveat (exact): `services/native_pane_input.py`'s `TmuxPaneInput` —
  the transport the native adapters' composer plans write through, and the
  one §8.3's operator-message path would inherit — performs **no**
  per-write socket re-proof; §8.3 pins how the operator-message path meets
  the same promise.
- Copy-mode guard (cond-0178): the only non-payload keystroke the managed
  write boundary may send is `send-keys -X cancel`; any unproven step is
  `refused/copy-mode-active` before any payload byte
  (`control_input_service.py:2052`).

#### 1.2.1 Read-only composer observation (cond-0324)

A conductor that has already sent a control may need to know, without
sending another byte, whether the exact text it expected is still resting
in the provider's composer.  The fork provides a read-only observation
route for this purpose.

- Wire surface: `GET /terminals/{id}/composer-observation?expected_text_sha256=<64 lowercase hex>&expected_text_bytes=<positive integer>`.
- Capability is advertised in the per-terminal `control_input` block as
  `composer_observation: {supported: bool, protocol: "cao-composer-observation-v1"}`
  only when the terminal is managed, `native_tui`, identity-resolved, and
  the provider/build has a pinned composer observation layout
  (`control_input_service.py`).  Currently pinned builds are Codex
  0.146.0 and Kimi Code 0.29.2/0.33.0; an unpinned or unproven build advertises
  `supported: false` and the route refuses with `provider-unsupported`.
- The route resolves control identity, takes the pane-input lease, and
  re-proves the live pane/server identity under the lease exactly as the
  write path does.  It captures the pinned composer region and extracts
  the operator-typed text using the same layout pins as the §4.1
  composer-emptiness guard (`native_pane_input.py`).  It returns
  `observed: true` only when the extracted text's SHA-256 and UTF-8 byte
  length exactly match the caller's expectation and submission is not
  proven to have occurred.
- Response fields (`cao-composer-observation-v1`): `protocol`,
  `observed`, the complete declarable control identity (`terminal_id`,
  `terminal_incarnation`, `terminal_generation`, `pane_birth_id`,
  `provider_process_id`, `provider`, `native_session_id`, `execution_mode`,
  `session_name`), the sampling fields `pane_id`, `pane_pid`, and
  `provider_version`, `submission_observed` (`unsubmitted`/`submitted`/
  `unknown`), `content_sha256` and `content_bytes` only when
  `observed: true`, and `evidence_ref`.  Raw composer text is never
  returned or logged.  Typed refusals carry `refusal: {reason, detail}`.
- The extractor returns a digest only for an unwrapped, single-row payload
  whose frame padding is unambiguous.  It preserves internal whitespace and
  prompt-like payload characters.  Wrapped or leading/trailing-whitespace-
  ambiguous captures fail closed rather than inventing a canonical payload;
  the conductor's raw UTF-8 fingerprint remains authoritative.

### 1.3 Event → tmux mapping (deployed)

| v3 event | tmux argv | Source |
|---|---|---|
| `text` (not Enter-fused) | `send-keys -t %N -l -- <chunk>` (1024-char chunks) | `clients/tmux.py:1234-1358` |
| `text` fused with following `key:Enter` | literal chunks, then `send-keys -t %N Enter` | `control_input_service.py:3621-3706`, `clients/tmux.py:1359-1365` |
| `key` | `send-keys -t %N <name>` via `send_sequence_key`, wire→tmux translation table `_TMUX_SEQUENCE_KEY_NAMES` (today only `Backspace→BSpace`) | `clients/tmux.py:1443-1512`, table at `:40` |
| `chord` | `send-keys -t %N <chord>` via `send_steer_chord`, sink pattern `^C-[A-Za-z]$` | `clients/tmux.py:1514-1581`, `:126` |

Critical sink property (`clients/tmux.py:31-40`): `send-keys` without `-l`
**never errors on an unknown key name — it sends the argument as literal
bytes.** Every managed write therefore goes through an explicit allowlist and
translation table; passing an unvetted name to tmux is a silent wrong-bytes
bug, not an error.

### 1.4 Provider-control facts (deployed)

- There is **no single provider-control registry** on the base. Capabilities
  live in: the adapter table `native_control_adapter` (kimi_cli, claude_code
  only — `services/managed_launch_v2.py:1022-1032`); Kimi's build-pinned
  `_PROVEN_STEER_CHORDS` (`kimi_native_control.py:290-296`, `C-s` for
  0.29.0/0.29.1/0.29.2); `CONTROL_COMPACT = "/compact"` in both adapters
  (`kimi_native_control.py:127`, `claude_native_control.py:92`, the latter
  with `ADVERTISED_CONTROLS = ("/compact",)` at `:93`); the wire key set
  (§1.1); and the sink allowlists.
- Codex has **no** native control adapter and **no** native-TUI launch
  binder (`native_tui_launch.py` supports "the two supported providers",
  `:15-20`). Managed Codex is ACP-only on this base.
- Interrupt-key evidence: Claude Code shows "esc to interrupt"
  (`providers/claude_code.py:104`); Codex shows "esc to interrupt"
  (`providers/codex.py:72-78`); Kimi's official keyboard reference documents
  `Esc` = "Close a popup / cancel completion / interrupt streaming output or
  context compaction" and `Ctrl-C` = "Interrupt the current streaming output"
  (primary source, Appendix A.6). There is no per-provider interrupt table in
  the repo today; `_SEQUENCE_INTERRUPT_KEYS = {"Escape","C-c","C-s"}`
  (`control_input_service.py:723`) is provider-agnostic.
- Version pins: `SUPPORTED_VERSIONS` kimi `("0.29.2","0.29.1","0.29.0")`,
  claude `("2.1.220",)`, codex `("0.146.0",)`
  (`services/provider_contracts.py:77-81`).
- Only `execution_mode == native_tui` panes accept control input; ACP panes
  are refused `managed-acp-pane` (`control_input_service.py:831-847`).
- **Adapter composer plans:** both native adapters carry build-pinned
  multi-line composer evidence (`_PROVEN_COMPOSER_NEWLINE`,
  `kimi_native_control.py:217-280` for 0.29.0-0.29.2 — `C-j` line breaks,
  `End` burst reset, 0.25 s settle; `claude_native_control.py:124-141` for
  2.1.220 — `C-j`, 2.0 s settle), and journaled at-most-once operation
  stores (`queue`/`steer`/`control` with states intended → posted →
  accepted/completed, frozen `ambiguous` resolvable only by exact-id
  reconcile). These are the deployed mechanisms Lane C's operator-message
  path builds on (§8).

### 1.5 Dashboard (deployed)

- `web/src/components/TerminalView.tsx` gates the native composer on
  `managed && execution_mode === 'native_tui'` (`:562`), then on four
  capability facts (`:77-80`). Every send re-proves identity via
  `GET /terminals/{id}/control-identity` and binds all nine
  `expected_identity` fields (`:166-179`). Sends are v1 `{text, enter:true}`
  (`web/src/api.ts:228-245`) or v3 `{events}` (`TerminalView.tsx:278-354`).
  Outcomes are read from the typed body; ambiguous/unknown transport results
  are reconciled by `GET /control-input/{control_id}` and **never resent**
  (`:186-191`, `:337-338`). No client queue; one `controlBusy` flag.
- The existing **Compact button sends `/compact` as ordinary literal text**
  through the identity-bound path (`TerminalView.tsx:618`) — the macro
  built-in preserves exactly this behavior as a v3 sequence.
- The v3 recorder (`web/src/lib/sequenceRecorder.ts`, cond-0175) captures
  Escape/Enter/Backspace/C-c/C-s + printable text, merges trailing text,
  enforces 32 events / 512 bytes, and **refuses arrows, Tab, F-keys, and all
  other modifier combos with a message, never approximates** (`:124-137`).
  It is preview-only: **no notation parser, no repetition syntax, no
  persistence** — recordings die with component state.
- Exactly one websocket: `/terminals/{id}/ws`. Client→server frames are
  `{"type":"resize"}` and `{"type":"input","data":...}`. The input frame is
  sent only when the pane is unmanaged, or managed + wheel-mouse report
  (`TerminalView.tsx:513-527`; server filter `_web_terminal_input_bytes`,
  `api/main.py:3892-3903`). **There is no raw keystroke path into a managed
  pane today, and this track must not create one** (§6.6).
- No `localStorage`/`sessionStorage` usage anywhere in `web/src`. No mobile
  layout (single `sm:grid-cols-3` in `DashboardHome.tsx:243` is the only
  responsive class; `TerminalView` is a fixed fullscreen overlay `:566`).
  Touch: single-finger swipe is translated to synthetic line-mode wheel
  events (`:412-470`); `.xterm` opts out of browser pan/zoom
  (`web/src/index.css:19-25`).
- Web tests: vitest only, `web/src/test/` (incl. `terminalView.test.tsx` 686
  lines, `sequenceRecorder.test.ts`). **No web e2e infra** (Playwright exists
  only in `cao_mcp_apps/`).
- **Visual baseline:** the deployed control area is one compact header row
  (identity chips left, close right) + a single composer row (full-width
  input, green **Send**, indigo **Compact**) + one quiet status line — see
  `baseline-dashboard.png` (§0). Lane C must preserve this vertical
  footprint.

### 1.6 Settings persistence (deployed)

- `settings.json` at `CAO_HOME_DIR` (`services/settings_service.py:14`) is
  the operator-level per-installation store, but it has **no schema version
  and non-atomic plain write_text saves** (`:37-40`). It is unsuitable as the
  macro library home (Finding F1).
- The versioned-store precedent is the control-input journal (SQLite,
  `journal_meta` schema version, snapshot-before-migration) and the
  `mkstemp`+`os.replace` atomic receipt writers
  (`services/wake_receipts.py:136-142`).
- `CAO_HOME_DIR` is `~/.aws/cli-agent-orchestrator` by default, relocatable
  via `CAO_STATE_ROOT` (`constants.py:75,86-142`) — the macro store and the
  Lane C attachment staging area inherit this, which is what "tied to the
  local CAO operator installation, not one campaign database or terminal
  generation" means mechanically.

### 1.7 Baseline test status

- Python control-input tier at the base SHA: **529 passed** (contract,
  service, journal, arbiter, endpoint suites; run
  `uv run pytest test/services/test_control_input_contract.py
  test/services/test_control_input_service.py
  test/services/test_control_input_journal.py
  test/services/test_pane_input_arbiter.py
  test/api/test_control_input_endpoint.py -q --no-cov`).
- Web vitest tier at the base SHA: **107 passed, 8 files** (`npm ci && npm
  test` in `web/`).
- Live tiers: `pytest -m e2e` deselected by default (`pyproject.toml:152`),
  gated on tmux/provider binaries (`test/e2e/conftest.py:41-125`, incl.
  `require_kimi` `:75`, `require_claude` `:61`). Private-server fixtures:
  `test/fixtures/tmux_server.py` (isolated `-S` socket, sanitized env,
  ownership-proven teardown), `test/fixtures/cao_server.py` (real
  `cao-server` subprocess, redirected `$HOME`). Existing live control-input
  evidence: `test/e2e/test_control_input_live.py` (real pane byte
  assertions), `test_tmux_isolation.py`, and
  `test/clients/test_tmux_sequence_key_boundary.py` (live, inline-gated).

## 2. Architecture decisions

Numbered decisions are the contract of this spec. Alternatives considered are
recorded with the rejection reason so review can challenge the reasoning, not
reopen settled ground by accident.

- **D1 — Extend the v3 key set in place; no new digest domain.** Add the
  eleven navigation/editing keys of §3.2 to `SEQUENCE_KEY_NAMES` under
  request schema v3. The digest preimage shape is unchanged (a key name is an
  opaque string inside `events`), and membership is explicitly a
  service-layer, capability-advertised decision (§1.1). An old server typed-
  refuses a new key `unsupported-key` (zero bytes, safe); a new client gates
  on the advertised `sequence.keys`. *Rejected alternative:* schema v4 with a
  new digest domain — the domain versioning exists for preimage-shape
  changes; burning a version on a membership change would force every client
  to track two dimensions for no safety gain.
- **D2 — One write path for control input. Streaming and macros reuse
  `POST /terminals/{id}/control-input` (v3); no new control write endpoint,
  no websocket input for managed panes, no second arbiter.** Every batch is
  one identity-bound v3 sequence: one `control_id`, one journal intent, one
  lease, one claim. (Lane C's operator messages are a *separate typed
  operation* — not control input — with equal discipline; see D11 and §8.)
- **D3 — Streaming is a client mode, not a transport.** The server already
  delivers ordered v3 sequences atomically under one lease; streaming is the
  dashboard converting focused keystrokes into bounded v3 batches with strict
  client-side serialization (§6). *Rejected alternative:* a websocket
  streaming channel — it would either bypass the arbiter (prohibited) or
  re-implement the journal/claim/reconcile machinery on a second transport.
- **D4 — Provider-control registry as a new server module.** Lane A creates
  `services/provider_controls.py` as the single source for Compact/Stop/
  Steer (and Lane C's message/image capability blocks), **consuming** the
  adapters' existing pins (it imports `CONTROL_COMPACT`; it does not retype
  `"/compact"`). Advertised through additive blocks in
  `/control-input/capabilities`. *Rejected alternative:* a frontend table —
  the brief's explicit prohibition, and it would drift from the
  version-pinned evidence.
- **D5 — Macro library is a new versioned JSON store, not a settings
  section.** `CAO_HOME_DIR/macros.json`, `schema_version`, atomic
  mkstemp+rename writes under an flock, migration with explicit quarantine
  (§5). *Rejected alternatives:* a `settings.json` section (unversioned,
  non-atomic — Finding F1); SQLite (the journal pattern is correct for an
  append-only audit log and overkill for a small CRUD library a human should
  be able to inspect).
- **D6 — Built-ins are synthesized, never persisted.** The API merges
  registry-derived built-ins (`origin:"builtin"`, `mutable:false`) with file
  records at read time. Immutability is then structural — there is nothing on
  disk to overwrite — and duplication mints a real user record.
- **D7 — Chord admission stays provider-pinned and build-gated.** New chords
  enter only through the registry with evidence; the wire `chord` event and
  `unsupported-chord` refusal are unchanged. `ctrl+c` in notation maps to the
  deployed `key` event `C-c` (provider-agnostic interrupt); every other
  `ctrl+x` maps to a `chord` event. Multi-modifier and non-letter chords are
  parser-refused as unrepresentable until a registry pin admits them (§3.3,
  OD1).
- **D8 — Capture reuses the recorder's focused-div mechanism, not an xterm
  key hook.** The deployed recorder pattern (focused `div`, `onKeyDown`,
  `preventDefault`) is extended for streaming and recording. xterm.js keeps
  zero input handlers for managed panes, so double-encoding is structurally
  impossible (§6.2). *Rejected alternative:*
  `attachCustomKeyEventHandler`+`return false` on the xterm instance —
  workable (Appendix A.5) but it puts the capture surface underneath the
  output surface, splits focus management, and must re-implement what the
  recorder already does under test.
- **D9 — The advertised capability set, not the schema version, gates client
  UI.** New-server/old-client and old-server/new-client behavior is defined
  by capability membership (§3.5). A client that sends an unadvertised key
  has a bug; the server's typed refusal is the backstop, never the mechanism.
- **D10 — Mobile is a first-class acceptance surface, tested with
  Playwright.** Lane B adds a minimal Playwright suite to `web/` (in-repo
  precedent: `cao_mcp_apps/playwright.config.ts`) with desktop and
  mobile-viewport projects (§10.5).
- **D11 — Operator messages (long text, multi-line, attachments) are a
  distinct typed operation, never an extension of control-input.** Lane C
  adds a sibling service with its own journaled at-most-once operations,
  its own limits, and the same identity/lease/reconciliation discipline
  (§8). Control-input's 512-byte short-command invariant stays exactly as
  deployed — the refusal is correct; what changes is that the composer
  stops implying the field is a general message box (F8). *Rejected
  alternative:* a control-input schema v4 with a larger text budget — it
  would blur the one invariant that makes the control contract auditable
  ("a control input is a command or one short line, not a document") and
  would force macros/streaming semantics to co-evolve with message
  semantics for no gain.
- **D12 — Images are delivered as staged-file references, never as bytes.**
  No supported provider can receive image bytes through injected keystrokes —
  all three CLIs read the OS clipboard in-process (Appendix A.9-A.11). The
  only faithful delivery mechanism available to a tmux-keystroke server is a
  validated file staged on disk whose path is delivered as text per the
  provider's documented contract (§8.4). A provider/build without evidence
  for an image mechanism is refused honestly; the pinned kimi 0.29.2
  staged-path mechanism is live-proven (§8.2, §8.4, §10.6).

## 3. The versioned shared event contract

This section is the frozen interface Lane B consumes. Lane B must not invent
an event name, key name, chord, or capability key absent from this section;
a needed-but-absent item is a spec amendment, not a PR decision. (Lane C
consumes §3.6 plus §8 only.)

### 3.1 Unchanged foundations

Protocol literal, digest domains, outcome/reason vocabulary, identity fields,
caps (32 events / 512 UTF-8 bytes aggregate), HTTP status discipline, and the
request-ID reconciliation rule are exactly the deployed contract (§1.1-1.2).
An ordered sequence remains one identity-bound operation: one `control_id`,
one journal intent, one pane-input lease, one claim, per-event outcomes
(`sent | attempted | skipped | refused`) journaled on failure.

### 3.2 Extended normalized key set (P1)

`SEQUENCE_KEY_NAMES` becomes exactly this set of sixteen names (the
deployed five plus eleven new):

```text
Escape  C-c  C-s  Enter  Backspace                (deployed)
Up  Down  Left  Right                             (new, P1)
Home  End  PageUp  PageDown                       (new, P1)
Delete  Insert  Tab                               (new, P1)
```

Advertisement order is **non-normative**: both deployed capability blocks
serialize `sorted(SEQUENCE_KEY_NAMES)` (`control_input_service.py:4191-4205`,
`:4243-4247`), so the wire array is lexicographic. Clients and tests assert
set membership, never array order (§10.1 names the repins).

**Readiness-gate intent class (pinned, not an implementation question):**
every one of the eleven new keys is **composer-class** — the deployed
default, since `_SEQUENCE_INTERRUPT_KEYS = {"Escape","C-c","C-s"}`
(`control_input_service.py:723`) and `_sequence_event_intent` falls through
to composer-class for every other key (`:726-733`). One composer-class
event readiness-gates the whole sequence (`:735-738`): on managed
native_tui panes the sequence is refused `pane-busy` unless the kimi
dispatch grace has expired and the live turn state reads IDLE/COMPLETED
(§1.2 gate scope). Keeping the new keys composer-class is deliberate —
interrupt-class membership would exempt them from the idle gate, and no
per-provider evidence supports that. The streaming policy for gate
refusals is §6.4; the live cadence/menu verification is §10.3.

Wire→tmux mapping, added to `_TMUX_SEQUENCE_KEY_NAMES`
(`clients/tmux.py:40`). Every new name maps to the tmux key name in the
second column (the canonical name, never an alias — the sink passes
exactly the table value, and tmux accepts `PageUp`/`PageDown`/`Delete`/
`Insert` as primary names; the aliases `PPage`/`NPage`/`DC`/`IC` are
documented but unused); tmux then encodes for the pane's mode (Appendix
A.1-A.3):

| Wire name | tmux `send-keys` arg | Bytes the pane receives | Mode dependence | Gate class |
|---|---|---|---|---|
| `Up`/`Down`/`Right`/`Left` | same | `ESC[A/B/C/D` | tmux emits `ESC O A/B/C/D` (SS3) when the pane application set DECCKM application-cursor mode; correct for both readline-style and fullscreen TUIs — tmux performs the translation, the spec does not | composer |
| `Home` | `Home` | `ESC[1~` | **not** DECCKM-switched by tmux (hard-coded, Appendix A.3); live answer (L1, r9): byte-exact delivery proven, but kimi 0.29.2's `/model` menu does not consume it — transport guaranteed, menu effect not promised | composer |
| `End` | `End` | `ESC[4~` | same live answer (works in the composer line editor) | composer |
| `PageUp` | `PageUp` (alias `PPage`) | `ESC[5~` | none | composer |
| `PageDown` | `PageDown` (alias `NPage`) | `ESC[6~` | none | composer |
| `Delete` | `Delete` (alias `DC`) | `ESC[3~` | none | composer |
| `Insert` | `Insert` (alias `IC`) | `ESC[2~` | none | composer |
| `Tab` | `Tab` | `0x09` | none | composer |
| `Enter` | `Enter` | `0x0D` | deployed | composer |
| `Backspace` | `BSpace` | `0x7f` (tmux `backspace` option default) | deployed | composer |
| `Escape` | `Escape` | `0x1B` | deployed | interrupt (deployed) |
| `C-c` | `C-c` | `0x03` | deployed; provider-agnostic interrupt | interrupt (deployed) |
| `C-s` | `C-s` | `0x13` | deployed as key; also a registry steer chord for kimi | interrupt (deployed) |

Rules:

- The sink translation is table-driven and total: a wire name absent from
  `_TMUX_SEQUENCE_KEY_NAMES` never reaches `send-keys` (§1.3 sink property).
- `BTab` (Shift+Tab, `ESC[Z`), modified arrows (`C-Up` → `ESC[1;5A`), and
  `F1`-`F12` are **not** in the P1 set. The mechanism exists in tmux
  (Appendix A.1) but no managed provider has evidence of consuming them;
  they are refused `unsupported-key` until a registry pin admits them (OD1).
- Browser capture maps `KeyboardEvent.key` values `ArrowUp/ArrowDown/
  ArrowLeft/ArrowRight/Home/End/PageUp/PageDown/Delete/Insert/Tab` to these
  wire names one-to-one (Appendix A.4: `key` values are standardized by UI
  Events; `key`+modifier booleans is the portable identification).

### 3.3 Chord support and refusal rules

- A v3 `chord` event remains the only chord carrier; membership is decided by
  `_steer_chord_refusal` against the registry before any write (deployed:
  `control_input_service.py:1268-1301`).
- **Notation mapping (Lane B parser, server re-validates):** `ctrl+c` →
  `{type:"key", key:"C-c"}`; `ctrl+s` → `{type:"chord", chord:"C-s"}`; any
  other `ctrl+<letter>` → `{type:"chord", chord:"C-<letter>"}` (server will
  refuse unless the registry pins it for this provider+build).
- **Refused at capture/parse, with a specific explanation, never
  approximated:**
  - Multi-modifier chords (`ctrl+shift+x`, `ctrl+alt+x`, `alt+x`,
    `meta/cmd+anything`): no standard-mode byte encoding exists; tmux would
    inject the base key or a wrong encoding (Appendix A.1, A.3 — e.g. `C-;`
    parses as a tmux key but injects a plain `;`). Refusal text names the
    combination and states that the terminal byte stream cannot represent it.
  - Browser/OS-owned combinations (`ctrl+w`, `ctrl+n`, `ctrl+t`, `⌘q`, `⌘w`,
    `F11`, `ctrl+shift+delete`, …): these never reach page JavaScript
    (Appendix A.4; no normative list exists — the vendor lists are treated as
    non-exhaustive). The recorder/streaming surface states this class of
    refusal statically in its help text, because it cannot observe the key at
    all: "combinations owned by the browser or OS never reach this page and
    cannot be recorded or streamed."
  - Case-distinct chords (`ctrl+shift+c` vs `ctrl+c`): distinguishable in the
    DOM but **not** in the byte stream (both are `0x03`); the capture layer
    must not claim the distinction — `ctrl+shift+letter` is refused as
    unrepresentable rather than folded to `ctrl+letter`.
- IME composition during streaming/recording: `compositionstart` suspends
  capture with a visible notice ("IME composition is not streamed; use the
  literal composer for composed text"); composed characters are never
  partially forwarded.

### 3.4 Batch and request-ID reconciliation (streaming and macros)

- One batch = one v3 request = one `control_id` (UUID). At most **one batch
  in flight per terminal**; keystrokes arriving during a flight accumulate
  into the next batch in order.
- Response handling is the deployed taxonomy: typed body outcome wins;
  `{404,405,501}` → `unsupported`; `{408,425,500,502,503,504}` and any
  no-response → `ambiguous`; other 4xx → `refused`.
- Any `ambiguous` transport result (including a lost response) is reconciled
  by exactly one `GET /control-input/{control_id}`; the journaled record is
  the answer. **A batch is never re-sent.** Only a `refused` outcome may be
  followed by a fresh attempt with a **new** `control_id`, and the streaming
  disarm policy (§6.4) decides whether that attempt is automatic (only in
  §6.4's two pause cases) or operator-initiated.
- Server-side same-`control_id` semantics (exact, deployed — §1.2): the
  `GET` lookup is always zero-I/O; an identical POST replays a stored
  `delivered`/`ambiguous` terminal answer with zero new I/O; an identical
  POST after `refused` **re-arms** (`refused → intent`) and may write —
  refusal's zero-byte proof is exactly what licenses that retry; a
  divergent binding is `request-rebound`. Client policy is therefore:
  reuse the id only to reconcile; an operator-initiated or §6.4-scheduled
  retry uses a **new** `control_id`.

### 3.5 Capabilities and old/new compatibility

`GET /control-input/capabilities` gains additive keys (JSON: old clients
ignore unknown keys):

```jsonc
{
  // … every deployed key unchanged (§1.2) …
  "sequence": {
    "event_types": ["text", "key", "chord"],
    "keys": ["Escape","C-c","C-s","Enter","Backspace",
             "Up","Down","Left","Right",
             "Home","End","PageUp","PageDown",
             "Delete","Insert","Tab"],
    "max_events": 32,
    "max_text_bytes": 512
  },
  "streaming": { "supported": true, "max_in_flight": 1, "coalesce_window_ms": 200 },
  "provider_controls": {
    "kimi_cli":    { "compact": { "events": [ {"type":"text","text":"/compact"}, {"type":"key","key":"Enter"} ] },
                     "stop":    { "events": [ {"type":"key","key":"Escape"} ] },
                     "steer_chords": ["C-s"],
                     "dispatch_grace_ms": 5000 },
    "claude_code": { "compact": { "events": [ {"type":"text","text":"/compact"}, {"type":"key","key":"Enter"} ] },
                     "stop":    { "events": [ {"type":"key","key":"Escape"} ] },
                     "steer_chords": [] }
  }
}
```

- **Chord send authority is the per-terminal block, keyed by exact provider
  AND build.** The per-terminal block on
  `GET /terminals/{id}/control-identity` grows the same `sequence.keys` and
  a `provider_controls` entry for that terminal's provider resolved at that
  terminal's build — its `steer_chords` is the exact set the server would
  admit for this pane (`_steer_chord_refusal` decides against the
  build-pinned table, §3.3). The top-level capabilities union (above) is
  **discovery only** — it tells the client chord events exist; it never
  licenses a send. A chord absent from the per-terminal advertised set is
  refused **locally** at capture/compose time with zero POSTs (§6.2,
  §10.4) — the client never uses a server refusal as capability discovery
  (D9).
- `dispatch_grace_ms` is the server-owned pacing fact behind §6.3/§6.4's
  kimi grace policy (deployed value: `NATIVE_KIMI_DISPATCH_GRACE_SECONDS =
  5.0`, `control_input_service.py:159`); providers without a grace omit the
  key.
- Providers with no registry entry (codex and all others on this base) have
  **no** `provider_controls` entry; the dashboard hides the built-ins and
  states why (§13, OD3).
- **New server / old client:** old clients read only deployed keys;
  `literal_write` path byte-identical; additive keys ignored. No migration.
- **Old server / new client:** `sequence.keys` lacks navigation keys →
  navigation UI hidden/disabled with the stated reason; `streaming` absent →
  the toggle is disabled with "server predates streaming"; `provider_controls`
  absent → built-ins hidden, user macros still available if `sequence` v3 is
  advertised; `/macros` 404 → the library UI is hidden behind a notice, never
  a fallback to local storage as the authoritative store; `command_controls`
  absent → built-in Compact is offered only with the §4.1 guard-absent
  notice and no `payload_class` field is ever sent.
- A client never sends a key/chord the server did not advertise; the server's
  typed refusal is the backstop for buggy clients, not the mechanism.
- Lane C's `operator_message` and `image` capability blocks (§8.6) follow
  the same additive rule when Lane C lands.

### 3.6 Interface Lane B consumes (frozen)

1. v3 event schema and `sequence.keys` from §3.2 (live, per-server,
   per-terminal — never hardcoded).
2. `provider_controls` from §3.5 for built-ins and steer-chord gating.
3. The macro API of §5.4.
4. The notation parse endpoint of §5.3 (server-authoritative) plus the
   pinned grammar of §5.3 for live client preview.
5. Reconciliation rules of §3.4.
6. The §4.1 command declaration (`payload_class: "command"`) — consumed
   **only** by registry built-in sends and supervisor/provider command
   controls, only after the `command_controls` block is advertised;
   streaming, macros, and prose never set it.
7. The §6.7 interactive declaration (`payload_class: "interactive"`) —
   consumed **only** by the armed streaming capture surface, only after
   the per-terminal `interactive_streaming` block is advertised; every
   other send path leaves it absent.

## 4. Provider-control registry (Lane A)

New module `src/cli_agent_orchestrator/services/provider_controls.py`:

```python
ProviderControls = TypedDict  # {"compact": sequence | None, "stop": sequence | None,
                              #  "steer_chords": tuple[str, ...],
                              #  "dispatch_grace_ms": int | None,
                              #  "operator_message": {...} | None,   # Lane C, §8.6
                              #  "image": {...} | None,               # Lane C, §8.6
                              #  "evidence": dict}
PROVIDER_CONTROLS: dict[str, ProviderControls]
def controls_for(provider: str, provider_version: str | None) -> ProviderControls | None
    # Send authority. Build-exact: steer chords resolve through the
    # adapter's build-pinned table, so a provider on an unpinned build gets
    # the entry with an empty chord set — never the union of all builds.
def advertised_provider_controls() -> dict[str, ProviderControls]  # capabilities shape (discovery only)
```

Contents and sourcing:

| Provider | Compact | Stop | Steer | Evidence |
|---|---|---|---|---|
| `kimi_cli` | `[text("/compact"), key(Enter)]` — imported from `kimi_native_control.CONTROL_COMPACT` (`:127`) | `[key(Escape)]` | `C-s` (build-gated by the existing `_PROVEN_STEER_CHORDS`, consumed not copied) | Kimi keyboard reference (Appendix A.6): Esc interrupts streaming output/compaction |
| `claude_code` | `[text("/compact"), key(Enter)]` — imported from `claude_native_control.CONTROL_COMPACT` (`:92`) | `[key(Escape)]` | none | `providers/claude_code.py:104` "esc to interrupt" |

Rules:

- The registry never restates a literal the adapters already pin; it imports
  and re-shapes. Adding a provider is adding one row plus its evidence — no
  wire-schema change (this is the codex on-ramp, OD3).
- **Chord membership is decided per exact build**, mirroring the deployed
  `_steer_chord_refusal` (`control_input_service.py:1268-1301`, which
  resolves `adapter.steer_chords(provider_version)`). `controls_for` takes
  the normalized provider version (`provider_contracts.normalized_version`)
  for exactly this reason; `advertised_provider_controls()` unions builds
  for discovery and is never send authority (§3.5).
- Entries carry an `evidence` dict (source pointers) and are gated to
  `SUPPORTED_VERSIONS` builds (`provider_contracts.py:77-81`). An entry whose
  evidence fails live acceptance is removed or corrected, never approximated.
- Compact travels as ordinary composer text through the v3 path — identical
  to the deployed Compact button (`TerminalView.tsx:618`). The kimi adapter's
  `control()` gating on provider-advertised commands applies to the adapter
  operation path, not the composer-text path; the registry documents this
  distinction.
- Stop for kimi is `Escape` (single key; matches claude; Ctrl+C remains
  available as the provider-agnostic `C-c` key event). Live acceptance on the
  pinned 0.29.x build is the verification (§10.3, OD2).

### 4.1 Provider command controls — command-class (r5 P1 amendment)

Origin: live owner evidence (2026-07-28, terminal `f4d25eb9`, native Claude
session `80a8272f-01ed-42e4-b0e0-5be4e1c0d627`, control record
`root-fable-agents-panel-013`): a queued text prefill survived `Escape` in
the Claude 2.1.220 composer, and an identity-bound `/agents` command was
then **appended to the prefill and submitted as one ordinary prompt** — the
command never executed standalone. The durable record proves identity-bound
posting (HTTP 200, `accepted`) and honestly records
`provider_completion.observed: false`; it does not prove standalone command
execution, and this contract never treats transport acceptance as command
execution.

**Definition and the declaration carrier (r7 P1-Δ2).** A control is
**command-class** only when the caller *declares* it: the request schema
gains one optional additive field, `payload_class`; absent means
**prose**. Two values are defined: `"command"` (this section) and
`"interactive"` (§6.7 manual interactive streaming — a distinct intent
with a distinct server policy, never the command grammar). Command-class
is **never
derived from payload shape** — a batch whose text happens to begin with
`/` (e.g. a streamed utterance split so a batch starts `/tmp/x`) is
undeclared prose and never enters the composer guard; declaration is the
only trigger. Only two callers declare: the registry **Compact** built-in
send and supervisor/provider command controls (`/agents`-class). Streaming
capture, ordinary macros (including user macros whose text begins `/`),
and the prose composer **never declare**.

Wire shape, following the contract's own v2-chord amendment pattern:
request **schema version 4** = v3 + optional `payload_class`, digesting
under its own domain `cao-control-input-request-v4` with preimage order
`("domain","schema_version","control_id","events","payload_class",
"expected_identity")` — the field participates for the same reason `chord`
does: a request that declares command-class is a different request from
one that does not, and a non-participating field would digest a declared
and undeclared request of the same id and events alike (rebound
blindness). v1/v2/v3 requests and their domains are byte-unchanged; a v4
request with `payload_class` absent is valid and means prose. (The r6
no-wire-growth steers 014/015 were scoped to the streaming-retry P2s;
this P1 requires the additive carrier, and it takes the additive
capability-gated form those steers protect.)

**Declaration validity.** A declared command is valid only for the command
grammar: exactly one `text` event whose text begins with `/`, optionally
followed by one `key:Enter` (the fused submitting Enter — the registry
Compact shape). Any malformed declaration — an unknown `payload_class`
value, a non-string value, or a declared command whose events do not match
the grammar — is a typed refusal **`malformed-command-declaration`** (new
reason, bound to `REFUSED`: decided before any write, zero bytes,
reattemptable). It is never approximated into prose and never executed
partially. (Implementation note, accepted r9: this refusal is answered
**pre-identity and ephemerally** — like the deployed `request-rebound`
refusal it is not journaled; a re-attempt re-refuses identically, and the
journaled `composer-nonempty` refusal remains the durable, exact-id
reconcilable one.)

**Live-verified pin scope (r9, from Lane A evidence):** composer-emptiness
determinations exist for exactly **kimi 0.29.2** (the rounded composer box
`╭─╮ / │ > … │ / ╰─╯` on the escape-free capture — the older
`── input ──` fixture rule did not match the installed bundle, and the
guard failed closed until the pin was corrected from live evidence) and
**claude 2.1.220** (prompt-box on a styled capture distinguishing the dim
placeholder from normal-video prefill). **kimi 0.29.0/0.29.1 are honestly
unpinned** — declared commands there refuse `provider-unsupported` until
live-verified, never guessed (rule 4).

**Rules.**

1. **Never concatenate.** Before the first command byte, under the same
   pane-input lease, the server must prove the composer **empty** through
   the adapter's composer observation — built on the deployed
   `capture_pane_screen` primitive (`native_pane_input.py:253-270`) with a
   per-provider+build pinned composer-region determination (same evidence
   discipline as the codex submission-barrier table,
   `native_pane_input.py:400-435`; the emptiness determination itself is
   new pinned logic, live-verified per build in §10.3). A composer that is
   non-empty — or whose emptiness cannot be proven — is a typed refusal
   **`composer-nonempty`** (new refusal reason, bound to `REFUSED`:
   decided before any write, zero command bytes, reattemptable, prefill
   untouched). The guard applies **only** to declared command-class
   requests. The only permitted alternative is a separately proven,
   per-provider+build-pinned **atomic clear/replace** after which exactly
   the command is submitted; none is pinned today. **Blind
   Escape/Enter/key-count clearing is prohibited** — the r5 evidence shows
   prefill surviving Escape, so no keystroke-count ritual may be specified
   as a clear.
2. **Same arbiter, same record.** Command-class rides the v3/v4
   control-input route unchanged: 9-field identity, pane-input lease,
   journal intent/claim, exact-id reconcile. No raw websocket, no
   unmanaged write path, no second operation kind (D2).
3. **Honest outcome, never forced completion.** A command-class control
   records transport acceptance plus the deployed `submission_observed`
   vocabulary at most (`submitted` = the composer was seen to give up the
   text). **Provider command execution is a pane-observable fact and is
   never inferred from transport or submission.** The close rule for
   declared commands is therefore exactly two-shaped (r11, blocking):
   - **Execution observed** — the per-provider+build **command-execution
     observation** (pinned below) proves the command's own UI/effect
     within a bounded window → `accepted`, journaled with the execution
     evidence reference attached. `accepted` for this class means
     "delivered *and* the execution signal observed" — never provider-task
     completion (the provider may still refuse or error inside the
     command's own UI, which is itself the observed answer, e.g.
     `⎿ Not enough messages to compact.`).
   - **Execution unproven** within the bound → **`ambiguous` with the
     deployed `submission-unproven` reason** (bound to `AMBIGUOUS`,
     `control_input_contract.py:157`, terminal for automation and never
     upgraded by a later observation), with a command-class detail string
     naming the unobserved signal. Exact-id reconcile replays the
     journaled record; the operator judges the pane. **No retry licence
     is created** — `ambiguous` is never reattemptable, and the declared
     command is never blindly resent.
   `mark_delivered` without the execution observation attached is a
   contract violation (the PR #48 defect class), not a shortcut.
   Crash and response-loss behavior is the
   deployed discipline, unchanged: a lost response is resolved by exactly
   one exact-id `GET` (never a resend), and a dead owner mid-write sweeps
   to `ambiguous/owner-lost-mid-write` (pre-write:
   `refused/owner-lost-before-write`, reattemptable) — §1.2/§3.4.
   Cross-repo coordination note (grounded, r5):
   the installed conductor record schema (cao-conductor 0.1.7,
   `conduct/lib/control_input.py:151-166`) defines `partial-ambiguous` and
   `resolved-manual` but makes them **unreachable from `posted`** —
   `LEGAL_TRANSITIONS["posted"] == frozenset({"completed"})` (`:156`) and
   `completed` requires an observed provider completion (`:693-695`), with
   the transition validator refusing any other target (`:852-855`). A
   posted command with unknown provider meaning can therefore only be
   falsely completed or stranded on the conductor side. This is a
   conductor-tooling gap for the supervisor to route (the conductor's own
   schema evolution adding a `posted → partial-ambiguous` edge); the CAO
   wire vocabulary already carries `ambiguous` and needs no change.

   **Command-execution observation (r11 pin).** Beside the
   composer-emptiness determination, each command-capable provider+build
   carries a pinned **execution observation**: a bounded post-write pane
   observation (same `capture_pane_screen` primitive) matching the
   command's own UI/effect — kimi 0.29.2: the compaction notice/UI or the
   context-percentage transition the command drives; claude 2.1.220: the
   command echo plus its response region (distinct from an ordinary
   prompt echo). The window is bounded and fits the write deadline; the
   evidence (capture ref) is journaled with the `accepted` record. A
   provider/build **without** an execution pin refuses declared commands
   `provider-unsupported` pre-write — declared commands require both the
   emptiness pin and the execution pin, never one alone. This is additive
   to the wire (existing outcomes, reasons, and fields; no new outcome
   vocabulary).
4. **Advertisement and compatibility.** Additive capability block
   `"command_controls": { "composer_nonempty_guard": true }` alongside
   `provider_controls`, and `4` joins `request_schema_versions`, when Lane
   A lands the carrier. The top-level block is discovery; the
   **per-terminal** block on `control-identity` is the honest per-build
   signal — it reports `"composer_nonempty_guard": false` when the
   terminal's provider build has no emptiness pin (accepted r9 deviation:
   emitted explicitly rather than omitted), and Lane B greys/hides
   command controls accordingly. **The client sends `payload_class` only
   when the
   block is advertised** — never earlier, never as a shape probe. An old
   server without the block offers no guard and receives no v4 field: the
   client must say so where a command control is offered
   ("prefill-concatenation guard unavailable on this server"), never imply
   it. An old client against a new server is unchanged (v1-v3 paths
   byte-identical). A provider/build without a proven emptiness
   determination refuses command-class controls `provider-unsupported`
   rather than guessing. No guessing based on payload shape, ever.

**Known limitation (accepted r9; no safety impact):** the journal schema
is unchanged (§11) and stores no `payload_class`, so a v4 record replayed
via `GET /control-input/{id}` reports `request_schema_version: 3` while
the direct answer reports 4. The digest still binds the declaration
(rebound protection intact); the discrepancy is documented here so clients
do not trip on it, and the wire-consistency fix is §17 backlog.

The registry Compact built-in (§5.5) is command-class and gains this
guard; the stop built-in (a bare key) is unaffected.

### 4.2 State-aware steer activation — the cond-0031 plan (r10)

Origin: existing P1 **cond-0031** (cross-ref **cond-0072** — a delivered
row can remain outside the model turn), live evidence 2026-07-28: v2
text+C-s controls `root-lane-a-native-status-024` (terminal `0e803ed3`)
and `root-lane-b-native-status-023` (terminal `2e267118`) posted into
**idle** Kimi composers and parked (steer envelopes record schema v2,
chord `C-s`, text+key posted, `resolution: null`); after an exact
identity/capability recheck, separate fresh schema-v3 key-only Enter
controls activated each exactly once — with **no** original-text replay.
This section is the docs-only plan for that issue; it does not file a
duplicate and does not broaden Lane A/B/C scope.

1. **The steer effect is state-dependent (grounded).** The deployed v2
   path treats the chord as "the … submit/steer effect … the chord presses
   the provider-pinned key that submits or steers it"
   (`control_input_service.py:2995-3034`). Live evidence proves that on
   Kimi the effect is steer-only: `C-s` steers an **active turn** and is
   ineffective against an **idle** receiver — the text rests in the
   composer unsubmitted. The v1/v2 path has **no turn-state readiness
   gate** (§1.2 gate scope), so choosing the correct activation form is
   the *caller's* obligation, and `accepted` + `chord_sent` must never be
   read as "entered the turn".
2. **State-aware selection rule (caller discipline, pinned):** classify
   the receiver through the deployed turn-state observation (the readiness
   gate's own detector) immediately before choosing the form:
   - **Active turn** → steer forms: v2 control (`text`+`chord`,
     `enter:false`) for steer-with-text, or bare v3 `[chord C-s]` (pure
     interrupt-class) for chord-only — both live-proven (§10.3 D3).
   - **Idle/completed** → the same operator intent is an ordinary
     message: composer-class submission (text+Enter, v1 or v3), which is
     readiness-gated and submits. **Never** a steer chord against an idle
     receiver.
   - **Unknown/unobservable state** → no chord: use the composer-class
     form (the gate arbitrates busy-ness honestly) or refuse and ask.
     Never a blind steer chord.
3. **Identity/lease binding (unchanged).** Every form rides the same
   9-field identity, pane-input lease, journal intent/claim, exact-id
   reconcile. An activation after a park is a **separate fresh control** —
   new `control_id`, fresh `expected_identity`, its own lease and journal
   intent — never a continuation, replay, or re-use of the parked
   control's id.
4. **No blind replay (park recovery discipline):** (a) reconcile the
   parked control by exact id — the durable record is the truth of what
   posted; (b) re-prove identity and capability fresh; (c) observe the
   composer holds exactly the expected parked text (pane capture, the
   §4.1 observation primitive); (d) only then send a fresh key-only Enter
   (v3 `[key Enter]`), activating the existing text exactly once — an
   ordinary message submission, not a steer; (e) if the composer does not
   hold the expected text → stop and report; the operator decides. The
   original payload is **never** retyped automatically — retyping into a
   composer that already holds it doubles the text.
5. **Truthful outcomes.** A control's record distinguishes transport
   acceptance from turn entry, using the deployed submission vocabulary:
   `submitted` (the composer/turn provably took the text), `unsubmitted`
   (the text provably rests in the composer — the park signature),
   `unknown` (unobserved → ambiguous class, manual reconcile). **A posted
   input plus an ineffective chord remains unresolved** — never recorded
   as entered or completed, and `unsubmitted`/`unknown` never upgrade
   later. Server-side pin for the steer path (future Lane-A-adjacent work,
   **not** PR #48 scope): a v2 steer result on kimi includes the
   post-chord composer observation where provable (text left the composer
   vs. still present) instead of today's unobserved null.
6. **Focused acceptance (§10.3).** Installed Kimi, **idle** target: v2
   text+C-s posts and parks (record carries `unsubmitted`); a fresh v3
   key-only Enter after identity/capability recheck submits **exactly
   once** (transcript shows one submission, no doubled text). **Active**
   target: v2 steer accepted with the steer observable mid-stream.
   **State-transition race:** the turn completes between selection and
   delivery → the control lands unresolved (`unsubmitted`), the
   activation Enter activates it once, and the record never claims
   entered-before-activation. No replay anywhere: a second activation is
   never sent without the fresh identity+content proof.

## 5. Durable operator macro persistence (Lane B)

### 5.1 Store

- File: `CAO_HOME_DIR/macros.json` (inherits `CAO_STATE_ROOT` relocation).
- Shape:

```jsonc
{
  "schema_version": 1,
  "macros": [
    {
      "id": "uuid",
      "name": "Model K2.7",
      "description": "optional",
      "scope": { "kind": "global" },                       // or {"kind":"provider","provider":"kimi_cli"}
                                                           // or {"kind":"profile","profile":"spec-writer-k3"}
      "events": [ {"type":"text","text":"/model"}, {"type":"key","key":"Enter"},
                  {"type":"key","key":"Up"}, {"type":"key","key":"Up"},
                  {"type":"key","key":"Up"}, {"type":"key","key":"Enter"} ],
      "favorite": true,
      "created_at": "…Z", "updated_at": "…Z"
    }
  ]
}
```

- The stored/transmitted correctness boundary is the v3 event array; every
  record is validated through the contract's `normalize_sequence_events` on
  load and on every write. Notation never touches disk.
- **Writes are atomic and serialized:** an flock on
  `CAO_HOME_DIR/macros.json.lock` (same flock discipline as the pane-input
  locks) wraps read-modify-write; the write itself is `mkstemp` in the same
  directory + `os.replace` (receipt-writer precedent,
  `services/wake_receipts.py:136-142`), file mode `0600`.
- Only the CAO server writes this file. Browser `localStorage` may keep
  ephemeral UI state (last filter, modal size) and is never the authoritative
  library (today the dashboard uses no web storage at all, §1.5).

### 5.2 Migration and quarantine

- `schema_version` newer than supported, unparseable JSON, or a top-level
  shape violation → the whole file is moved aside to
  `macros.quarantine-<utc-isots>.json`, the store starts empty, and every
  list response reports `quarantine: {"count": N, "path": "…"}` until the
  operator deletes the quarantine file. The server never fails to start over
  a macro file.
- Per-record validation failure on load (bad events, unknown scope kind,
  missing id/name) → that record is appended to the quarantine file and
  dropped from the working set; the rest load. **Nothing is ever silently
  dropped.**
- Version upgrades migrate in place where lossless; a migration that cannot
  preserve a record quarantines it. Migration runs under the same flock.

### 5.3 Macro notation (editing surface)

Grammar (pinned; the two parsers — TS preview, Python authority — are tested
against shared golden vectors, mirroring the digest golden-vector precedent):

```text
sequence := event (WS+ event)*
event    := text | named | chord | repeat
text     := '"' JSON-string '"'                 # JSON escaping exactly; , + / \ are literal inside quotes
named    := [a-z][a-z0-9-]*                     # enter escape up down left right home end
                                                # page-up page-down delete insert tab backspace
chord    := 'ctrl+' [a-z]                       # ctrl+c ctrl+s … (D7 mapping)
repeat   := (named|chord) '*' [1-9][0-9]*       # up*3; expansion counts toward the 32-event cap;
                                                # any over-budget count (incl. integer-overflow
                                                # magnitude) is a parse error with offset, never a 500
```

- Notation names map to wire names: `enter→Enter`, `escape→Escape`,
  `page-up→PageUp`, …, `backspace→Backspace`; `ctrl+c→key C-c`, other
  `ctrl+x→chord C-x` (D7).
- Parse errors carry an offset and a message; unparseable or unrepresentable
  macros cannot be saved or sent (client disables the action; server 422s).
- The normalized preview renders the canonical token form
  (`"text" [Enter] [Up]×3 [Ctrl+S]` — extending the deployed
  `previewToken`) and the exact event JSON before save/send.
- Server authority endpoint: `POST /macros/parse-notation` `{notation}` →
  `{events, preview}` or `422 {errors: [{offset, message}]}`.

### 5.4 API (FastAPI; READ scope for list, WRITE scope for mutations — same
discipline as the settings routes, `api/main.py:1331-1410`)

| Route | Behavior |
|---|---|
| `GET /macros?provider=&profile=` | Visible set = registry built-ins for `provider` (synthesized, D6) + user records whose scope is global, provider-matching, or profile-matching. Each item annotated `origin: builtin|user`, `mutable: bool`. **Server-side ordering (pinned):** favorites first, then non-favorites; within each group by scope rank (global → provider → profile), then case-insensitive name. Response includes `quarantine` when present. |
| `POST /macros` | `{name, description?, scope, events` **or** `notation, favorite?}`. Server validates (notation via §5.3; events via contract normalization) → 422 with errors. |
| `PUT /macros/{id}` | User records only; a built-in id → `409`. Full replace of mutable fields; `updated_at` bumps. |
| `DELETE /macros/{id}` | User records only; built-in → `409`. |
| `POST /macros/{id}/duplicate` | Mints a user record (new id; name editable); works on built-ins — this is the only way to "edit" a built-in. |
| `POST /macros/parse-notation` | §5.3. |

Sending a macro is **not** a store operation: the client takes the resolved
`events` and sends an ordinary v3 control-input request (D2). The store never
writes to panes.

### 5.5 Built-ins

For each registry provider (§4): **Compact** and **Stop**, `origin:"builtin"`,
`mutable:false`, visually distinguished, `favorite:true` in resolution order
(they sort first; an operator cannot un-favorite a built-in — duplicating it
makes a user macro that can). Scope: built-ins resolve in the provider scope
group. The deployed standalone Compact button is removed from the header;
Compact becomes this built-in favorite (§7.1).

**Deterministic built-in IDs (pinned):** built-ins carry stable namespaced
IDs — `builtin:<provider>:compact` and `builtin:<provider>:stop` (e.g.
`builtin:kimi_cli:stop`). The `builtin:` namespace is reserved: user-record
IDs are UUIDs and the store rejects any user ID with the `builtin:` prefix.
IDs are therefore stable across responses and restarts, so
`POST /macros/{id}/duplicate` resolves the same built-in the client fetched
(§5.4). Stability/collision tests are §10.4.

## 6. Streaming mode (Lane B UI on the Lane A contract)

### 6.1 Arming

Streaming is an explicit toggle in the header, available only when:
`nativeManaged`, the live capabilities advertise `streaming.supported` and the
full §3.2 key set, and identity resolved. On arm the client fetches
`managed-control`, `control-identity`, and capabilities fresh, pins the
9-field `expected_identity`, and displays provider / agent profile /
generation in the armed header (§7.3). When the per-terminal
`interactive_streaming` block is advertised (§6.7), every armed batch
declares `payload_class: "interactive"`; when it is absent, the surface
runs with the §6.4 readiness behavior and an honest notice. Arming replaces the composer with the
capture surface; the ordinary literal composer is restored on disarm with its
draft preserved (attachments, once Lane C exists, survive arm/disarm as draft
state — §8.7).

### 6.2 Capture

- The capture surface is a focused `div` extending the deployed recorder
  mechanism (D8): `onKeyDown` with `preventDefault`/`stopPropagation`.
  `KeyboardEvent.key` mapping: single printable chars (`.length === 1`, no
  ctrl/meta) → trailing text event merge; `Enter/Backspace/Escape/Tab/Delete/
  Insert/Home/End/PageUp/PageDown/Arrow*` → named key events per §3.2;
  `ctrl+c` → `key C-c`; `ctrl+s` → `chord C-s` **only if the per-terminal
  advertised chord set (§3.5) contains `C-s`**; any other `ctrl+letter` →
  a chord event only if that exact chord is advertised for this terminal's
  provider+build, otherwise **refused locally with zero POSTs** — the
  capture layer holds the advertised chord set from arm time and never
  relies on a server refusal for capability discovery (D9); everything
  else → refused in place with the §3.3
  explanation, never approximated, and the refusal is trace-visible.
- `paste` on the capture surface → `preventDefault`; clipboard text becomes a
  `text` event (screened against the remaining byte budget; over-budget
  refused with message). IME per §3.3. (Clipboard *images* on the capture
  surface are refused with a pointer to the composer's attachment flow —
  streaming is keystrokes only.)
- xterm.js keeps **no** input handlers for managed panes; output rendering,
  selection copy, and wheel/touch behavior are exactly as deployed.
- **Prohibition:** the streaming layer never synthesizes key events from
  mouse-wheel or touch-scroll input. Wheel/touch scrolling keeps its deployed
  semantics (§1.5) and never becomes provider keystrokes.

### 6.3 Batching and the wire-to-arbiter sequence

Coalescing and flush (values advertised by the server, §3.5, so policy stays
server-owned):

1. Keystroke → normalized event appended to the pending batch (trailing text
   merged).
2. Flush the pending batch when: a non-text event **other than Enter**
   follows text (boundary), the quiet timer `coalesce_window_ms` (default
   200 ms) fires with pending events, or the pending text event reaches 48
   chars. **Enter-after-text fusion:** a trailing `Enter` while text is
   pending is appended to the same batch (caps permitting) so the text and
   its submitting Enter travel in one request — the deployed service
   delivers text+Enter fused (`control_input_service.py:3621-3706`), and
   splitting them across two leases is the interleave hazard the arbiter's
   own docstring names ("the Enter belonging to the first submits whatever
   prefix of the second has already landed",
   `pane_input_arbiter.py:4-6`). An Enter with no pending text is its own
   batch immediately. Hard caps always: 32 events / 512 UTF-8 bytes
   (server-enforced); if fusion would exceed a cap, the text flushes first
   and the Enter rides the next batch — the documented residual below then
   applies.
3. **Dispatch-grace pacing (kimi):** the client withholds composer-class
   batches while the advertised `dispatch_grace_ms` window (§3.5) is still
   running after a locally-accepted batch that contained Enter — the
   deployed server stamps that grace on delivered Enter-carrying writes
   and refuses readiness-gated composer input inside it
   (`control_input_service.py:3195-3207`). Pacing prevents the refusal
   rather than reacting to it; the §6.4 pause rule is the backstop for
   clock skew.
4. Flush → `POST /terminals/{id}/control-input` `{control_id: <new uuid>,
   events, expected_identity: <pinned at arm>}` with the deployed 15 s
   timeout. The client does **not** refetch identity per batch — the server's
   under-lease re-proof is the authoritative gate (§1.2); a drifted identity
   comes back as a typed refusal and disarms (§6.4).
5. Exactly one batch in flight (§3.4); events arriving during a flight form
   the next batch.
6. Server side per batch (all deployed): journal intent → pane-input lease
   (cross-process, `control-input:{control_id}` holder) → live identity
   re-proof → copy-mode guard → claim → ordered event writes with per-write
   server-identity proof → outcome.
7. Response: `accepted` → trace appends the batch outcome and streaming
   continues. Lost response → one `GET /control-input/{control_id}` → the
   journaled record is the truth (§3.4) → then §6.4.

**Residual inter-batch hazard (named, bounded, not closed):** the lease is a
per-batch `with`-block, so between two batches of one utterance another
lease holder (a supervisor tell via send-input, a native inbox payload) can
write into the same composer line. Enter fusion (step 2) removes the hazard
for the common text+Enter case; the residual applies only to utterances
spanning batches (cap splits, key boundaries). Observable behavior: the
other writer's activity surfaces as `pane-busy` refusals or as composer
content the operator can see; armed streaming is **advisory-exclusive, not
exclusive** — the header states this ("other automation may still write
between batches"), and §10.3's acceptance keeps utterances fused wherever
caps allow. No lease TTL, token, or hand-off is introduced (§15).

### 6.4 Pause and disarm conditions (each with an explicit on-screen reason)

Outcome taxonomy. Streaming distinguishes **pause** (a reattemptable,
zero-byte refusal whose cause is transient provider busy-ness) from
**disarm** (everything else). Auto-retry exists only in the two pause
cases below, is bounded to one scheduled re-attempt with a **new**
`control_id` each time (licensed because `refused` proves zero bytes,
§3.4), and never applies to `ambiguous` — an ambiguous batch's delivery
state is settled by the journal, never by resending.

- **Pause — dispatch grace:** `refused/pane-busy` whose reason detail is
  the kimi dispatch grace (§6.3 step 3) → the trace shows "paused
  (dispatch grace)"; one automatic re-attempt with a fresh `control_id` is
  scheduled for when the advertised `dispatch_grace_ms` window expires; if
  that re-attempt is also refused, disarm with the reason.
- **Pause — readiness gate:** `refused/pane-busy` whose reason detail
  matches the pinned turn-state-gate discriminator (§6.4 routing note
  below) → the trace shows
  "provider busy"; one automatic re-attempt with a fresh `control_id`
  after the **client-pinned** `STREAMING_GATE_RETRY_MS = 1000` constant
  (a client-side streaming constant, deliberately **not** a server
  capability — no protocol growth; it is not advertised anywhere); if also
  refused, disarm
  with the reason. This is the mid-menu/mid-turn behavior for the §3.2
  composer-class keys: navigation during an open menu passes the gate only
  when the receiver reads IDLE/COMPLETED — live-verified in §10.3's
  cadence acceptance.
- **Disarm — lease contention:** `refused/pane-busy` from the arbiter
  itself ("input lease is held by another thread/process") means
  **concurrent input** — disarm and explain (the brief's rule; the
  operator decides whether to re-arm).
- **Disarm — every other non-accepted outcome:** `refused` with any other
  reason (`stale-generation`, `identity-mismatch`, `pane-dead`,
  `copy-mode-active`, `unsupported-key`, `write-deadline`, …), `ambiguous`
  (any reason), `unsupported`, and
  any unknown typed outcome (fail closed). `write-deadline` is
  infrastructure, not provider busy-ness — it is plain disarm with the
  reason; the operator re-arms.
- **Disarm — environment:** reconciliation query fails; identity refetch
  shows a changed generation/pane; capabilities/identity fetch fails at
  arm (streaming does not arm); output websocket closes while armed;
  `visibilitychange → hidden` / `pagehide`; the **Stop streaming** button
  (a visible mouse/touch control, never a keyboard shortcut that may
  itself have been forwarded).

**Disarm is an atomic client transition:** the quiet timer is cancelled,
all unsent pending events are discarded (no queue drain — nothing typed
after the point of disarm is ever sent), only trace metadata is retained,
and exact-ID reconciliation for the already in-flight batch continues to
completion. A component test proves input arriving during a refused
in-flight batch produces no second POST (§10.4).

**Pause-vs-disarm routing note (pinned discriminators, no new wire
shape):** the three `pane-busy` flavors share one deployed reason code and
are distinguished by the reason **detail** string. Routing keys on these
pinned discriminator substrings — dispatch grace: `"inside its dispatch
grace"` (deployed at `control_input_service.py:3204-3206`); turn-state
gate: `"not idle"` (`:3225-3231`); arbiter contention: `"input lease is
held by"` (`pane_input_arbiter.py:160`, `:213-215`). A `pane-busy` whose
detail matches none of the three is **disarm** (fail closed) — never
guessed into a pause. §10.1 contract-tests the three deployed detail
strings against these discriminators so a server wording change fails
loudly instead of silently re-routing a pause into a disarm (or
contention into a pause). No typed sub-reason is added to the wire.

On disarm the surface shows the reason and the terminal trace, offers
**Re-arm**, and — on any identity refusal — refetches identity so the
explanation names the new generation.

### 6.5 Diagnostics

Bounded trace (last 50 batches): per batch the normalized preview, short
`control_id`, outcome + `reason_code`, event/byte counts. Target line:
provider, agent profile, short generation, armed indicator in a
high-contrast active color (§7.3). **Clear trace** button. The trace is
diagnostic only — never an input buffer; editing it edits nothing.

### 6.6 The websocket invariant (restated, test-enforced)

For managed panes the websocket input frame remains wheel-mouse-only
(deployed filter, `api/main.py:3892-3903`). Streaming, macros, literal sends,
and provider controls carry **zero** `{"type":"input"}` frames on managed
panes. Lane B extends the deployed vitest guard
(`terminalView.test.tsx:312-349`) to assert the streaming surface sends no
websocket input while armed; Lane C's attachment/message flow likewise sends
none (uploads and message submission are ordinary HTTPS). The
unmanaged-terminal raw channel is unchanged and out of scope.

**Resize frames (named, bounded):** the only other client→server frame is
`{"type":"resize","rows":N,"cols":M}`, which is **accepted viewer
geometry**, not pane input: it is unfiltered for managed panes and reflows
the bound TUI (this is what makes desktop/mobile viewing usable), and a
malformed frame currently tears down the viewer websocket (fail-closed).
Resize carries no keystroke content and cannot write to the composer, so
the non-bypass invariant is unaffected. Lane A adds a small hardening
(P2): server-side validation/clamping of resize dimensions (positive,
≤ 500×200) and a type check that rejects malformed frames with a typed
close reason instead of a teardown, with tests.

### 6.7 Declared interactive streaming (cond-0194 owner override, r15)

Owner product decision (authoritative, cond-0194): **provider/model turn
activity is not a write prohibition for manual native-TUI interactive
streaming.** While a provider turn is active, the operator must be able to
queue/enter manual text, navigate or safely cancel provider model/effort
menus, issue Stop, and use supported steering. The previously observed
`pane-busy` disarm of an armed streaming session in those states is a P1
defect. Automated delivery (inbox, prose composer sends, macros, Lane C
messages) remains separately readiness-gated and must never silently
inherit this bypass. (Per-build menu honesty, r16: where a pinned build
queues a `/model`-class command instead of opening a menu mid-turn —
Claude 2.1.220, live-proven — the override means "the command queues and
the turn continues", never a claimed menu action; §10.3.)

- **Carrier (minimal, deployed-compatible):** a second defined value of
  the r7 v4 declaration field — `payload_class: "interactive"` under the
  **existing** request schema v4 (`cao-control-input-request-v4`, pinned
  preimage order with `payload_class` spliced, §4.1). No new digest
  domain and no shape inference: the field already participates, so
  declared-interactive, declared-command, and undeclared requests of the
  same id and events all digest distinctly. Legal payload: any
  v3-valid event sequence (the streaming batch grammar); no command
  grammar applies. Unknown values and non-strings remain
  `malformed-command-declaration` (the declaration-validity refusal
  covers the field generally; no new reason code, no typed sub-reason).
- **Declarer discipline (the inheritance fence):** only the **armed
  streaming capture surface** (§6.1-6.2 — human keystrokes) may declare
  `"interactive"`. Macros, favorites/built-ins, the literal composer,
  Lane C, inbox delivery, and every automated path **never** set it, and
  §10.1/§10.4 assert that mechanically. Declaration is the only trigger:
  the server applies the interactive policy to declared requests only.
- **Exact server policy:** for a declared interactive request the server
  bypasses **only** the provider IDLE/COMPLETED turn-state refusal and
  the kimi dispatch grace for that batch. It does **not** bypass: pane
  lease contention (`pane-busy` from a real concurrent lease owner still
  refuses and §6.4 still disarms), terminal/generation/incarnation/
  pane/socket/native-session proof and under-lease re-proof, the
  copy-mode guard, malformed/declaration/representability refusals,
  journal ambiguity (one exact-id reconcile, never auto-resend), the
  write deadline, or the §3.2/§3.3 key/chord admission rules. Outcome
  truthfulness is unchanged: `accepted` means bytes delivered; whether the
  provider queued or consumed the text is recorded per the deployed
  submission vocabulary (kimi/claude visibly queue mid-turn composer
  text; steering rides the pinned chord, §4.2) — never inferred.
- **Advertisement and old/new behavior:** the per-terminal
  `provider_controls` entry gains `"interactive_streaming": { "supported":
  true }` (discovery at top level; the per-terminal block is send
  authority, D9). A declared interactive request to a provider/build
  without the pin refuses `provider-unsupported` pre-write. New clients
  send `payload_class: "interactive"` **only** when the per-terminal block
  advertises it; an old server omits the block and the armed surface
  falls back to the §6.4 readiness behavior with an honest notice — never
  a speculative bypass. Old clients are unchanged (they never declare).
  The distinction is explicit: `"command"` = §4.1 command-class (grammar +
  composer-empty guard); `"interactive"` = this manual-streaming intent
  (sequence grammar + turn-gate bypass); absent = prose/automated.
- **Client status honesty (r15 pin):** the dashboard normalizes typed
  backend detail into typed status: `reason_code` plus the §6.4 pinned
  detail discriminators map to specific status text; an unrecognized
  detail renders the **raw detail string**, never an empty or
  "unrecognized" reason. And no blind disarm after an explainable
  `accepted`/queued/steered result: an accepted interactive batch —
  including text the provider visibly queues mid-turn — is a trace entry
  with its submission observation, not a disarm trigger. Disarm follows
  the §6.4 taxonomy only.
- **§6.3/§6.4 deltas (narrow):** armed streaming includes
  `payload_class: "interactive"` in each batch when the per-terminal
  block advertises it (the declaration rides the same POST body;
  §6.3's wire sequence is otherwise unchanged). §6.4's two pause cases
  (dispatch grace, readiness gate) apply to **undeclared** batches; they
  do not fire for declared interactive batches because the server no
  longer issues those refusals for them.

**Acceptance (§10.3):** active-turn disposable Kimi **and** Claude
sessions: manual printable text lands (queued per provider semantics with
an honest submission observation); Stop interrupts. Menu behavior is
**per-provider/build-pinned and honest** (r16, root-r15-claude-limit-125):

- **Kimi 0.29.2 (live-proven):** active-turn menu navigation delivers and
  `Escape` safe-cancels the menu; queued text followed by a declared
  `C-s` is consumed (the steer effect). This is required acceptance and
  it passes.
- **Claude 2.1.220 (live-proven provider limit, not a CAO defect):** an
  active-turn `/model` (or effort command) is **accepted and queued in
  the native composer** — it does **not** open a model/effort menu. The
  acceptance proves the command is queued, claims **no** menu open,
  setting change, or menu cancellation, and shows the active turn
  continuing unchanged. This is pinned provider behavior for this build;
  it is never classified as a product P1 and never reported as a menu
  success. Dashboard/provider-status honesty follows: where the pinned
  build queues a command, UI and evidence say **"queued command"**, never
  a menu action.

True concurrent lease contention refuses and disarms.
Stale identity and copy mode refuse with zero bytes. Response loss
resolves by exact id, never resend. Managed websocket input stays
wheel-only (resize is geometry). Old/new capability combinations behave
honestly (§6.7 advertisement rules). Desktop and mobile status evidence
is captured for the interactive states (armed banner, queued
observation, pause/disarm notices).

## 7. Layout and accessibility acceptance (Lanes B and C)

### 7.1 Header (all widths)

One primary row (wraps to two on narrow widths):

```text
[📎] ┌ Send a message to the native composer… ┐ [Send] [Streaming] [Macros]
```

- The attachment button (`📎`/paperclip icon) exists only when Lane C has
  landed and the terminal's provider advertises `operator_message` (§8.6);
  until then the row is exactly the deployed composer + Send + Streaming +
  Macros.
- Composer takes remaining width; **Send** disabled until content (text or
  attachment); **Streaming** is a toggle with a clear armed state;
  **Macros** opens the library modal with a count badge when the visible set
  is non-empty.
- The composer is no longer labeled as if it were only a short-command
  field, and never silently truncates: a live status line names the delivery
  path the current draft will take (§8.5 routing). The deployed 512-byte 422
  stays correct for the control-input path (F8).
- The standalone **Compact** button is removed (built-in favorite macro,
  §5.5). The recorder row (deployed) is absorbed into the macro modal's
  recorder; the header keeps no third row.
- Acceptance: row height ≤ 48 px at ≥1024 px; ≤ 96 px total (two rows) at
  390 px; terminal viewport keeps ≥ 50 % of viewport height with header +
  favorite strip visible at 390×844. The ≥ 50 % rule is measured on the
  **visible fitted `.xterm` child**, not its wrapper: FitAddon floors the
  fit to whole rows, so wrapper floors must include row-quantization slack
  (the merged implementation pads `50dvh` by +10 px; installed A+B QA
  measured the visible `.xterm` at 432 px ≥ 422 px at 390×844 and
  400 px ≥ 400 px at 360×800 — GO, PR #50). (Matches the §0 visual baseline's
  compact footprint.)

### 7.2 Favorite strip

Appears only when favorites exist; horizontally scrollable, never wraps:

```text
Global: [Compact] [Stop]   Kimi: [Steer] [Model K2.7]   This agent: [Max effort]
```

- Grouping labels carry scope; buttons do not repeat it. Order = server
  order (§5.4). Tooltip (desktop) / long-press (touch) shows the normalized
  preview before send. Strip height ≤ 48 px (leaves border/focus-ring
  budget); buttons ≥ 44×44 px touch target
  (Apple HIG goal; WCAG 2.2 SC 2.5.8 floor 24×24 px).
- Sending from the strip is one tap = one v3 request (D2) with the deployed
  outcome/status reporting.

### 7.3 Streaming surface

```text
● STREAMING TO kimi_cli / spec-writer-k3 · gen a1b2c3
  text("/model") [Enter] [Up] … accepted (8f3a)       [Clear trace] [Stop streaming]
```

- Armed border/indicator in a high-contrast active color (contrast ≥ 4.5:1
  against the terminal background); `role="status"` + `aria-live="polite"`
  for outcome changes. Target line shows provider, profile, short generation.
- Trace bounded per §6.5. **Stop streaming** always visible and reachable by
  mouse/touch and keyboard focus order.

### 7.4 Macro modal

- Desktop (≥1024 px): two-pane modal ≤ 920 px wide. Left: search, scope
  filter, favorites filter, list (each row with a direct **Send**), **New
  Macro**. Right: name, description, scope selector, favorite toggle,
  recorder (event tokens + notation), editable notation with live normalized
  preview and parse errors, **Send Test**, **Save**, **Duplicate**,
  **Delete**. Built-in rows: badge, no edit/delete, **Duplicate** and
  **Send** only.
- Mobile: full-height sheet (`100dvh`) with list view and editor view (back
  navigation); every list row keeps its direct **Send**.
- Recorder: captures per §6.2 with the §3.3 refusal messaging; recorded
  events render as editable tokens and as notation, kept in sync (editing
  either re-derives the other through the pinned parser).
- Focus trap while open; `Escape` closes the modal unless a capture surface
  is actively recording (then `Escape` is recorded input); focus returns to
  the invoking control on close.

### 7.5 Accessibility acceptance (WCAG 2.2 AA)

- Text contrast ≥ 4.5:1; armed-streaming indicator not color-only (icon +
  text).
- All controls keyboard-operable; visible focus ring; icon-only buttons have
  `aria-label`.
- Touch targets ≥ 44×44 px on the streaming/strip/sheet surfaces.
- Modal `role="dialog"` + `aria-modal`; sheet respects safe-area insets.
- Honor `prefers-reduced-motion` for the streaming indicator animation.
- Lane C additions in §8.7 item 8.

### 7.6 Session-card header (Lane B dashboard composition, r8 owner-approved)

Ground truth: the deployed session card (`DashboardHome.tsx:392-412`) makes
the **entire header one toggle `<button>`** wrapping the chevron, session
name, agent count, type badges, status summary, and timestamps — so header
text cannot be selected without toggling the card. Lane B re-composes it:

- The header is a plain container (no button role). **All displayed
  metadata — name, badges, counts, status summary, timestamps — is
  selectable/copyable text** (`user-select: text`, default cursor).
  Clicking, double-clicking, or drag-selecting any metadata **must not**
  expand/collapse the card (no click handler on the container or metadata).
- Expand/collapse belongs **exclusively** to a dedicated chevron
  `<button>`: accessible name including the session name (e.g.
  `aria-label="Expand session cao-fleet"` / `"Collapse session cao-fleet"`
  — state-speakable), `aria-expanded` bound to card state, `aria-controls`
  naming the terminals region's `id`, keyboard activation (Enter and
  Space — native button semantics, no custom keymap), a visible focus ring,
  and a ≥ 44×44 px touch target consistent with §7.5. The chevron must not
  intercept or swallow selection gestures on adjacent metadata.
- Existing separate controls (delete-session, per-terminal actions) are
  unaffected; no nested interactive elements inside the chevron button.

## 8. Lane C — image-capable operator-message composer (scope addendum)

Origin: `.conductor/tasks/native-tui-console-image-composer-addendum.md`
(owner request 2026-07-27), integrated per steer
`root-image-composer-addendum-steer-001`. Lane C is part of the P2 UI
overhaul and changes nothing about the P1 identity-bound control path; it is
a **separate typed operation** (D11) delivered in its own PR and isolated
worktree after the interaction contract (Lanes A/B) is accepted.

### 8.1 The defect being fixed (F8)

The deployed composer looks like a general message box but is backed by the
control-input 512-byte short-command cap; the owner's 866-byte message was
refused with a 422. The refusal is *correct contract behavior* — the defect
is the UX contract mismatch: one field implying two different operations.
Lane C resolves it by giving long text and attachments their own typed
operation (§8.3) and making the composer honest about which operation a
draft will use (§8.5). Control-input is **not** enlarged (D11).

### 8.2 Provider delivery evidence (researched, primary sources)

- **No provider accepts image bytes via keystrokes.** Claude Code, Codex,
  and Kimi ≥0.43 all read the OS clipboard inside the app on their paste
  chord (Appendix A.9-A.11). A tmux-keystroke server cannot paste an image;
  injecting `C-v` would read the *server host's* clipboard — wrong machine,
  wrong semantics, prohibited.
- **claude_code (pinned 2.1.220):** documented image-via-path flow —
  "Provide an image path to Claude. E.g., 'Analyze this image:
  /path/to/your/image.png'" (Appendix A.9). The model reads the staged file
  with its tools. Anthropic API limits: JPEG/PNG/GIF/WebP, ≤ 5 MB per image,
  ≤ 8000×8000 px (Appendix A.9). No CLI-level format/size documentation —
  CAO pins its own limits (§8.3).
- **kimi_cli (pinned 0.29.x): staged-path image delivery is live-proven on
  0.29.2.** A disposable native Kimi Code 0.29.2 session, handed ordinary
  composer text naming an absolute staged PNG path, invoked the provider's
  own `ReadMediaFile` tool on that exact file and reported the known visual
  fact correctly (round-3 acceptance, §10.6). The earlier inference that
  pinned kimi has no image support came from the *predecessor* kimi-cli
  changelog's 0.43 clipboard-paste entry (Appendix A.10) — paste-UI history,
  not this build's staged-path capability — and is superseded by the live
  evidence. Clipboard image *paste* into the kimi TUI remains unproven and
  refused; non-PNG formats remain unproven and refused (F9). Long/multi-line
  *text* messages remain deliverable via the adapter's build-proven composer
  newline plan (§1.4).
- **codex:** no native-TUI managed path exists on this base (§1.4), so codex
  is out of Lane C's scope regardless of its TUI's image features
  (Appendix A.11 recorded for the future codex track).

### 8.3 The operator-message operation (new typed path)

New sibling service `services/operator_message_service.py` + routes — the
same discipline as control-input, different operation:

| Property | Pinned value |
|---|---|
| Submit route | `POST /terminals/{id}/operator-message` (WRITE/ADMIN scope) |
| Reconcile route | `GET /operator-message/{operation_id}` (READ+; keyed by operation id alone, mirroring the control-input reconcile) |
| Request | `{operation_id: uuid, text: str, attachments: [attachment_id, …], token_map: {"1": attachment_id, …}, expected_identity: <9 fields>}` |
| Outcomes | the same closed vocabulary — `accepted / refused / ambiguous / unsupported` — with message-specific refusal reasons added: `provider-unsupported` (reused), `attachment-unknown`, `attachment-not-ready`, `attachment-too-large`, `attachment-type-unsupported`, `message-too-large`, `multiline-unproven` (build lacks a proven composer-newline plan) |
| Text limit | ≤ 8192 UTF-8 bytes (pinned; 16× the control cap). Multi-line allowed **only** through the provider's build-proven composer-newline plan (§1.4) — unproven build → refused `multiline-unproven`, zero bytes |
| Attachments | ≤ 4 per message; each ≤ 5 MB; dimensions ≤ 8000×8000 px (matches the tightest documented downstream limit, Appendix A.9) |
| At-most-once | journaled through the provider adapter's operation store (deployed `queue` semantics: intended → posted → accepted/completed, frozen `ambiguous`, exact-id reconcile). Duplicate `operation_id` + identical payload replays the stored answer; divergent payload is refused |
| Arbiter | the service acquires `pane_input_lease(pane_id, holder="operator-message:{operation_id}")` around the whole plan execution — it does **not** ride the unleased v2 admission path (F2); readiness/idle gating and the copy-mode guard apply exactly as in the control path |
| Transport re-proof | the adapters' composer plans write through `TmuxPaneInput`, which performs **no** per-write server-socket re-proof (§1.2). The operator-message service therefore performs the same under-lease identity re-proof the control path does — pane alive, `window_id`, `pane_pid`, canonical server socket — immediately **before** invoking the plan, and re-proves after any copy-mode cancel, so the gap between proof and first byte is bounded by the plan's own settle windows, with the write deadline applying to the whole execution |
| Identity | the same 9-field `expected_identity`; the server re-proves pane, window, pid, server socket, generation, provider/native session under the lease before the first byte |
| Reconciliation | lost response → one `GET /operator-message/{operation_id}` → the journaled record is the answer; **a message is never re-sent automatically**; `refused` permits an operator-initiated fresh attempt with a new id |

### 8.4 Attachment staging and delivery contract

- **Upload:** `POST /terminals/{id}/attachments` (multipart). The server
  validates *content*, not filenames: magic-byte sniff + structure/dimension
  decode (PNG signature + IHDR at minimum); type allowlist per provider
  evidence (PNG mandatory for all; JPEG/GIF/WebP admitted only for providers
  with documented support — claude_code per Appendix A.9). Over-limit,
  corrupt, or mismatched-content uploads fail with an actionable inline
  error; nothing is half-written (temp file + rename, like every other CAO
  store).
- **Storage:** `CAO_HOME_DIR/attachments/{terminal_id}/{attachment_id}.{ext}`
  — server-generated names only; the client-supplied filename is display
  metadata, never a path component. Files are mode `0600`, owned by the
  operator's CAO installation (the provider process runs as the same local
  user and can read them).
- **Typed state:** `staging → ready | failed`, `ready → removed | submitted`.
  Only `ready` attachments may be referenced by a first submit (a replayed
  submit for the same `operation_id` reads its existing `submitted`
  binding — §8.4 manifest rule). `submitted`
  attachments are retained read-only for a pinned TTL (24 h) so the provider
  can still read the path mid-turn; `removed` and expired files are deleted
  by a sweep at server start and periodically. Orphans from a crashed upload
  are swept the same way.
- **Attachment metadata and operation binding (minimal pin, per the owner
  speed guard):** records persist as a small versioned manifest
  `CAO_HOME_DIR/attachments.json` under exactly the D5 discipline (flock +
  `mkstemp`/`os.replace`, `0600`): attachment id, terminal ownership,
  validated type/dimensions/bytes, state, timestamps, staged path, display
  filename, `bound_operation_id`. The **at-most-once authority for the
  send itself is the operation store** (§8.3): a duplicate POST replays by
  `operation_id` there before attachment state is ever consulted, so basic
  text+image send correctness does not depend on a ledger CAS. The one
  binding rule that is pinned now: `ready → submitted(operation_id)`
  happens under the manifest lock; an identical replay for the same
  `operation_id` reads the existing `submitted` binding, and a different
  operation referencing a `submitted` attachment is refused
  `attachment-not-ready`. A SQLite/CAS ledger mirroring the control-input
  journal is **future hardening (§17 backlog)** — deferred per the owner
  speed guard as neither clearly necessary for basic send correctness nor
  worth blocking Lane C's gate on; crash-window and sweep behavior are
  covered by restart tests against the manifest (§8.8).
- **Token mapping:** the composer draft carries `[Image #N]` as editable
  text; the client maps `#N` → `attachment_id` in `token_map`. At submit the
  server verifies every referenced attachment is `ready` (or bound to this
  same `operation_id` on replay) and owned by this
  terminal, then performs the provider's reference substitution (§8.6
  `image.reference_template`): for claude_code the template inserts the
  absolute path at the token position, matching the documented
  path-in-prompt flow; for kimi the template is the **proven directive
  phrasing** (§8.6), since that is the trigger form the live acceptance
  exercised. A token without a mapping, or a mapping to a
  non-ready attachment, is a 422/refusal — never silently dropped, never
  partially submitted.
- **Container path translation:** profiles may declare
  `container.path_maps` and the provider layer translates host→guest by
  longest-prefix match (`models/agent_profile.py:27-57`,
  `providers/base.py:558-587`). Reference substitution happens
  **after** staging and **through the same translation**: the path placed
  into the provider-bound text is the translated guest path when the
  terminal's profile has a matching map, the host path otherwise. The
  staged file must therefore live under a mapped host prefix for
  containerized profiles — an attachment whose staged path maps to no
  guest path for that profile is refused `attachment-not-ready` with the
  explanation, never substituted as an unreadable host path.
- **Delivery mechanism per provider (honest matrix):**

| Provider (pinned build) | Long/multi-line text | Image attachments |
|---|---|---|
| `claude_code` 2.1.220 | adapter composer plan (proven C-j newline) | staged path inserted as text per documented flow; live acceptance required (§10.6) |
| `kimi_cli` 0.29.x | adapter composer plan (proven C-j newline) | **supported — staged-path-text via provider `ReadMediaFile`**: staged absolute path inserted at the token position; PNG proven by live acceptance on pinned 0.29.2 (§10.6); other formats refused as unproven (F9) |
| all others | refused `provider-unsupported` | refused `provider-unsupported` |

### 8.5 Composer routing rule (the honest-UX half of F8)

One composer, two explicitly-labeled operations — never implicit magic:

- Draft is **text-only and ≤ 512 bytes** → Send uses the deployed
  identity-bound control-input v1 path, byte-identical to today.
- Draft has **attachments, is multi-line, or is > 512 bytes** → Send uses
  the operator-message path (§8.3). If the provider does not advertise
  `operator_message`, Send is disabled with the reason. If attachments are
  present for an image-unsupported provider (any provider without a registry
  `image` block, or a format outside the advertised `image.formats` — e.g.
  non-PNG for kimi), the inline error names the provider limitation.
- The status line always names the path the current draft will take
  ("control input — short command" / "operator message — 1.2 KB, 1 image" /
  "operator message unavailable for this provider"), with a live byte
  counter. **Never** silent truncation, never a surprise 422.

### 8.6 Capability advertisement (additive, when Lane C lands)

`provider_controls` entries gain, per provider:

```jsonc
"operator_message": { "supported": true, "max_text_bytes": 8192, "multiline": true,
                      "max_attachments": 4 },
"image": { "supported": true, "formats": ["png","jpeg","gif","webp"],
           "max_bytes": 5242880, "max_width": 8000, "max_height": 8000,
           "mechanism": "staged-path-text", "reference_template": "{path}" }
```

kimi_cli advertises `"operator_message": {…, "multiline": true}` with
`"image": { "supported": true, "formats": ["png"], "max_bytes": 5242880,
"max_width": 8000, "max_height": 8000, "mechanism": "staged-path-text",
"reference_template": "Use the ReadMediaFile tool to read the image file
at {path} and analyze it in the context of this message.",
"evidence": "live acceptance on pinned 0.29.2 (§10.6)" }` — PNG only,
because PNG is the format the live proof covers, and the template is the
**proven directive phrasing**, because that is the trigger form the live
acceptance exercised; bare-path substitution is unproven for kimi and is
not claimed (a future bare-path acceptance would be new evidence, §10.6).
Every format outside `formats` is refused as unproven rather than assumed
(F9). Absent blocks (old server) → the
dashboard hides the attachment button and routes over-limit drafts to a
disabled Send with explanation (D9).

### 8.7 Lane C UI composition (per addendum + §0 visual baseline)

1. Identity/status row stays visually quiet (baseline row 1 untouched).
2. Attachment chips appear in a compact, horizontally scrollable strip
   **only when attachments exist** — thumbnail, display filename/type/size,
   state (`staging` spinner, `failed` inline error with retry/remove),
   remove action. Strip height ≤ 56 px.
3. Main composer row stays text-first: attachment button, text field, Send,
   Streaming, Macros (§7.1).
4. `[Image #N]` remains editable draft text, visually linked to chip `N`
   (matching number badge); deleting the token detaches the attachment
   (state → `removed`); removing the chip selects/offers to delete the
   token.
5. Mobile: chips scroll horizontally; controls wrap at most once; the
   terminal keeps ≥ 50 % viewport height (consistent with §7.1).
6. Paste: `Cmd+V`/`Ctrl+V` with an image on the clipboard while the composer
   is focused → upload (state `staging`) + `[Image #N]` token at the caret +
   chip. Paste with plain text → ordinary text paste. Paste while streaming
   is armed → refused per §6.2.
7. File picker: keyboard-operable button (`<input type="file"
   accept="image/png,image/jpeg,image/gif,image/webp">` filtered per the
   advertised `image.formats`), same staging flow.
8. Accessibility: chips have descriptive alt text (`"Image #1: screenshot
   .png, 244 KB, ready"`); upload progress and failures announced via
   `aria-live="polite"`; remove buttons ≥ 44×44 px with `aria-label`;
   failed uploads keep focus on an actionable control; the file chooser is
   reachable in tab order.

### 8.8 Lane C test requirements

- Unit: staging validation (magic bytes, dimension decode, size/count
  limits, MIME spoofing), state machine transitions, token-map validation
  and substitution per provider template, cleanup sweep.
- Service/API: routes (upload/list/delete/submit/reconcile), typed outcomes
  incl. every §8.3 reason, duplicate-operation replay, identity drift
  refusal, lease contention with a concurrent control-input write
  (`pane-busy`), never-resend after a lost response.
- Component (vitest): chip strip states, token/chip linkage, routing-rule
  status line, paste and picker flows, unproven-format refusal messaging
  (e.g. non-PNG for kimi), old-server degradation (hidden button).
- Live provider (installed, §10.6): claude_code native session receives a
  staged PNG reference and the model can read the file; kimi native session
  receives a staged PNG reference via the operator-message path (upstream
  capability already proven, §10.6) and refuses non-PNG formats with the
  unproven-format explanation.
- Browser/visual QA (Playwright, §10.5): desktop 1280×800 and mobile
  390×844 against the §0 baseline — chip strip, wrap behavior, terminal
  height rule, streaming-off/on scrolling unaffected; screenshots in the PR.
- Regression: ordinary text-only sends byte-identical (deployed suites
  unchanged); macros, streaming, scrolling, and §3.5 old/new compatibility
  all green.

## 9. Delivery lanes and integration ordering

Three lanes, each its own PR (and Lane C its own isolated worktree, per the
addendum), serialized at the integration branch.

### Lane A — transport/protocol (server)

Deliverables: §3.2 key-set extension (`control_input_contract.py`,
`clients/tmux.py` mapping table, `control_input_service.py` representability
path if needed); §4 registry + capabilities extension; §3.5 compatibility
behavior; server notation parser + `/macros/parse-notation` **authority**
(co-located with the contract; consumed by Lane B's §5.4 routes);
tests per §10.1-10.3.

### Lane B — UI/persistence (dashboard + macro store)

Deliverables: §5 store + routes + TS preview parser; §6 streaming; §7
layout (minus Lane C items); §10.4-10.5 tests. Lane B writes **no** contract
code and consumes §3.6 only.

### Lane C — image-capable operator-message composer

Deliverables: §8 (service + routes + staging + registry blocks + composer
routing + UI + tests). Lane C consumes the accepted Lane A contract and the
Lane B header it extends; it must not modify the control-input contract,
the macro store schema, or the streaming capture layer — shared types and
identity primitives only (per the addendum's isolation requirement).

### Ordering and the serialization points

1. This spec is the frozen interface (§3.6, §8). Lanes A and B may implement
   in parallel against it.
2. **Lane A merges first.** Serialization point 1 is the merge of the
   contract+registry+capabilities change: until it lands, Lane B integrates
   against a stubbed capabilities response pinned to §3.5.
3. **Lane B merges second** (integration of UI + macro persistence).
4. **Lane C starts after the Lane A contract is accepted** and merges last,
   in its own PR/worktree. Serialization point 2 is the acceptance of the
   operator-message contract (§8.3) at spec review; Lane C's only
   cross-lane touchpoints are the registry blocks (additive), the header row
   it extends, and the capabilities response.
5. Any lane-discovered need for a contract change is a spec amendment in
   this file (with the decision log updated), agreed before implementation —
   never a frontend invention (D9).

Suggested branches (one PR per lane, serialized into the integration
branch): `feature/native-tui-console-lane-a`, `…-lane-b`, `…-lane-c`, then
`feature/native-tui-interaction-console` → `main` as one reviewed PR.

### Integration status and the Lane B seam (r12, verified topology)

- **r14 update — A+B installed-GO; only Lane C remains:** canonical head
  `38ad00ee8857acf90fc5bb56a484f8aecc1a079d` (PR #50 merge) carries the
  A+B visual-QA fixes (PR #49: streaming quiet-timer binding + mobile
  overlay layout — semantics unchanged; PR #50: armed-terminal floor
  padded past row quantization). The installed Sol/xhigh retest is **P4
  GO** at this head (fitted `.xterm` 432 px ≥ 422 px at 390×844,
  400 px ≥ 400 px at 360×800; desktop first-printable identity batch;
  zero raw managed-pane websocket input). The A+B staging gate is
  **complete**; the only remaining product lane is the isolated Lane C
  PR/worktree (§9, merges last). The campaign is not complete until Lane
  C lands. (Tooling note: the deploy `--dry-run --bounce`
  activation-receipt contradiction is a known dry-run-only tooling P2 —
  cond-0087/cond-0116/cond-0138 — not a product blocker.)
- **r13 update — integration complete through Lane B:** canonical head
  `ada0d1982cb00dbef5492bc4a1c0d0ed2debfc3e` (merge of PR #47,
  2026-07-28) contains Lane A (PR #48), the r9-r12 docs merge
  (`b796043`), and the Lane B seam (`6fc348e`): duplicate
  `macro_notation.py`/`macro_builtins.py` removed, the approved repeat
  fix ported onto Lane A's canonical parser (pre-conversion >2-digit
  rejection, offset-422 never-500), `macro_store.py` synthesized from
  `services/provider_controls.py`, Lane A's parse-notation route kept,
  `/macros` CRUD additive, golden vectors carrying the `up*100`/30-digit
  cases byte-identical across TS/Python. **Lane C is not implemented at
  this head** (only the future-facing `provider_controls` docstring
  exists; no attachment/operator-message routes, branch, or PR) — the
  Lane C image/operator-message PR starts next as its own isolated lane
  per §9, and upload checks belong to its acceptance, not to the current
  A+B QA checkpoint.
- **Lane A merged** into the integration branch at
  `8687af27ee0bcd5d7f37b46f0bee2061260762bf` (PR #48, final exact-head
  Sol/high review at `7b3674f44f0289aedb9cfca67638fd951396b1d6`; includes
  the r11 two-close repair). Canonical services now merged:
  `services/provider_controls.py`, `services/macro_notation.py`,
  `POST /macros/parse-notation`, `test/fixtures/notation_vectors.json`,
  the §10.3 evidence bundle, and the resize hardening.
- **Canonical spec docs** r9-r11 live on the retained design branch
  (`c612b1e`, `6fdb633`, `a51191f`) based on `a624c69`. The integration
  branch's copy of this file is **byte-unchanged since `a624c69`**
  (verified zero-diff), so a plain non-force merge of the design branch
  into the integration branch is conflict-free for this document and
  preserves both Lane A code/evidence and the r9-r11 docs. Sequence
  (supervisor-executed, no force, no GitHub alteration by the writer):
  merge design branch → integration branch, push; then Lane B serializes
  onto that head (PR #47 at `8c2d4a0c7f091c420c96a584cd40ae23c4c2fec5`,
  currently CONFLICTING against it, as expected — it was based on the
  older integration base).
- **Lane B seam (minimal, pinned):** (a) delete Lane B's temporary
  duplicate `services/macro_notation.py` and keep Lane A's canonical,
  **porting the approved macro-repeat fix onto it** (pre-conversion
  >2-digit rejection at the token offset, display-bounded token, parser +
  endpoint offset-422 regressions — PR #47's `8c2d4a0` content applied to
  the canonical file, which still carries the unguarded conversion);
  (b) delete the temporary `services/macro_builtins.py` and repoint
  `macro_store.py`'s built-in synthesis to `services/provider_controls.py`
  (D6 — the merged registry is the single authority); (c) keep Lane A's
  `/macros/parse-notation` route registration; Lane B's `/macros` CRUD
  routes are additive; (d) the shared golden vectors gain the `up*100` /
  30-digit repeat cases and must stay byte-identical between the TS
  preview and the canonical Python authority; (e) the CI-trigger hunk is
  identical on both branches by design.
- **Explicitly outside the seam** (owner pin): the deferred PR #47 P2s
  and the editable-token product decision are not taken here — no product
  drift.
- **Rollback:** the docs integration is an ordinary merge commit (revert
  it; Lane A's merge is untouched; the design branch retains the docs
  regardless). Lane B's seam is a normal PR — revert it independently.

## 10. Test matrix and acceptance

### 10.1 Server unit/contract (Lane A; default pytest suite)

- Contract: extended `SEQUENCE_KEY_NAMES` pinned verbatim as a set; new keys
  digest identically under v3 (golden vector unchanged for old keys);
  refusals for `BTab`, `C-Up`, `F1`, empty/unknown names
  (`unsupported-key`).
- tmux client: `send_sequence_key` argv for every §3.2 row (mock subprocess;
  extend `test/clients/test_tmux_literal_input.py` pattern); unknown name
  never reaches argv; the aliased keys pass the **canonical** name
  (`PageUp`, `PageDown`, `Delete`, `Insert`), never the alias.
- Service: v3 sequences mixing text+navigation+chord delivered in order with
  per-event outcomes; refusal before any write leaves zero tmux calls;
  readiness-gate classification pinned per §3.2 (all eleven new keys
  composer-class; the deployed interrupt keys unchanged).
- Registry: contents pinned; `controls_for` honors exact build (an unpinned
  kimi build yields an empty chord set — assert against
  `_PROVEN_STEER_CHORDS` versions and a bogus version);
  `advertised_provider_controls` shape; no literal
  duplication of adapter pins (assert import identity).
- Capabilities endpoint: additive keys present; deployed keys unchanged.
  **Repins (named):** the extended `sequence.keys` changes three deployed
  exact-equality assertions — `test/api/test_control_input_endpoint.py:340`
  and `:349`, and `test/services/test_control_input_contract.py:462` — which
  are updated to assert set equality against the §3.2 set (advertised order
  is sorted and non-normative, §3.2); the golden-diff for every other
  capability key stays exact.
- Notation parser: golden vectors shared with the TS parser (checked into
  `test/fixtures/notation_vectors.json`, consumed by both suites). **Every
  malformed repeat is an offset-bearing 422, never a 500** (r11 P2): a
  repeat lexeme whose count cannot fit the remaining event budget —
  including an integer-overflow-sized count such as `up*` + 5000 nines —
  is rejected before integer conversion (or the conversion error is
  caught) and returns the normal `{errors:[{offset,message}]}` shape at
  both parser and endpoint layers.
- **Declared-command close rule (§4.1, r11):** with the execution
  observation proven, a declared command closes `accepted` **with the
  evidence reference journaled**; with the observation unproven, it closes
  `ambiguous`/`submission-unproven` (terminal, exact-id reconcile replays
  the record, never resent); a provider/build without an execution pin
  refuses declared commands `provider-unsupported` **pre-write**; no code
  path may `mark_delivered` a declared command without the observation
  attached (regression test against the PR #48 shape: accepted with
  `submission_observed: null` is forbidden).
- **Command-class guard (§4.1):** schema v4 digests under its own domain
  with `payload_class` in the pinned preimage order (golden vector; v3
  digest bytes unchanged); a **declared** command-class sequence against a
  composer observed non-empty is refused `composer-nonempty` with **zero**
  tmux calls and the prefill untouched; an unobservable composer fails
  closed identically; an observed-empty composer delivers exactly the
  command; both new reasons are bound to `REFUSED` in `REASON_OUTCOMES`
  (import-time assert covers them); a malformed declaration (unknown value,
  non-string, non-grammar events) is the typed zero-write
  `malformed-command-declaration`; **undeclared payloads — including a
  batch whose text begins `/` — never trigger the guard** (the streamed
  `see /tmp/x` split case sails through as prose); non-command payloads
  never trigger the
  guard; the `command_controls` capability block is additive-only.
- **Pane-busy detail discriminators (§6.4 routing note):** the three
  deployed detail strings are contract-tested verbatim — dispatch grace
  (`control_input_service.py:3204-3206` contains `"inside its dispatch
  grace"`), turn-state gate (`:3225-3231` contains `"not idle"`), arbiter
  contention (`pane_input_arbiter.py:160`, `:213-215` contains `"input
  lease is held by"`) — and the three discriminators are asserted
  pairwise-disjoint, so a server wording tweak fails the suite loudly
  rather than silently changing streaming's pause/disarm routing.
- **Interactive declaration (§6.7):** `payload_class: "interactive"`
  digests distinctly from declared-command and undeclared v4 requests
  (golden vectors); a declared interactive batch skips **only** the
  turn-state gate and dispatch grace while every other refusal fires
  (lease contention, stale identity, copy mode, malformed/representable,
  deadline — each asserted with zero bytes where pre-write); an
  interactive declaration to an unpinned provider/build refuses
  `provider-unsupported` pre-write; unknown values/non-strings remain
  `malformed-command-declaration`; and no automated path (macro send,
  inbox payload, Lane C message, literal composer) ever emits the field
  (asserted at their call sites).

### 10.2 Live tmux (Lane A; `pytest -m e2e`, isolated server fixtures)

- `test/e2e/test_control_input_live.py` pattern, real pane running a
  byte-recorder: assert exact bytes for `Up` (normal vs application cursor
  mode via a mode-setting helper app), `Home`/`End` (`ESC[1~`/`ESC[4~`),
  `PageUp`/`PageDown`, `Delete`, `Insert`, `Tab`.
- Ordered mixed sequence lands byte-exact and in order; concurrent lease
  contender gets `pane-busy`; copy-mode guard still precedes payload.

### 10.3 Installed provider acceptance (Lane A + supervisor-run;
`require_kimi` / `require_claude` fixtures, real `cao_server`)

- Disposable **Kimi** native-TUI session (pinned 0.29.x): `/model` menu
  navigation changes the model selection deterministically **when paced**
  (separate requests at human cadence — live D1: an all-at-once fused
  `text("/model") enter up*3 enter` is delivered byte-exact but races the
  picker mount; the fused form remains a valid *transport* acceptance, the
  deterministic *menu outcome* acceptance is the paced form; the
  text+Enter fusion contract is untouched). **Reasoning/effort is not a
  `/model` Left/Right selector on 0.29.2** (live D2: the model row renders
  a single `Thinking [ On ]` segment and Left/Right are no-ops; effort
  changes ride the separate `/effort` command — the brief's
  `left*2 right enter` example does not exist on this build and is
  corrected, not approximated). **Mid-turn steer (live D3):** a fused v3
  `[text, chord]` batch is composer-gated and refused `pane-busy` mid-turn
  by design (§3.2); the supported mid-turn forms are the deployed **v2
  steer control** (`text`+`chord`, `enter:false` — live-proven,
  `chord_sent: true`) and a bare v3 `[chord C-s]` sequence (pure
  interrupt-class). Lane B implication (called out, no contract change): a
  text+chord favorite/macro delivers only when the receiver is
  idle/completed — an honest gate refusal, not a bypass; true mid-turn
  steer-with-text rides the deployed v2 path, which a later registry row
  may expose as a product decision. `escape`
  interrupts streaming output — **OD2 verified live**: kimi shows
  `Interrupted by user`; claude's spinner disappears (2.1.220 leaves no
  textual marker; spinner-gone is the observable).
- Disposable **Claude** native-TUI session: built-in Compact executes;
  `escape` interrupts (spinner-gone observable, above).
- Codex: no native-TUI path exists (§1.4); acceptance asserts the registry
  advertises no codex entry and the dashboard hides the built-ins with the
  stated reason (OD3).
- **Home/End live answer (L1; closes F4/OD5):** delivery is byte-exact
  (`ESC[1~`/`ESC[4~`, proven in §10.2 — tmux's non-DECCKM-switched
  encoding is *not* a defect); kimi 0.29.2's `/model` menu simply does not
  consume Home/End (selection unmoved), while the composer line editor
  does. Keyboard transport guarantees stand; no menu effect is promised
  and no registry restriction is needed (none was pinned).
- **Streaming-cadence acceptance (new, the §3.2 gate-class verification):**
  against a disposable Kimi native-TUI session, stream (as separate batches,
  not one fused request) `text("/model") enter`, then several `Up`/`Down`
  batches at human cadence into the open menu, then `enter` — proving (a)
  the menu's turn state reads IDLE/COMPLETED so composer-class navigation
  passes the readiness gate, and (b) a batch sent inside the 5 s dispatch
  grace after the submitting Enter is handled by the §6.4 pause rule
  (withhold → one scheduled re-attempt with a fresh id → accepted), not by
  disarm. If the menu does not read idle, the deviation is recorded here
  and §3.2/§6.4 corrected before Lane A closes.
- **Command-class guard acceptance (r5, §4.1; r7 carrier):** on disposable
  Kimi and Claude native-TUI sessions, seed the composer with queued text,
  press Escape (the r5 evidence shows prefill may survive it — that is the
  point
  of the case), then issue a **declared** command-class control (`/compact`
  built-in; `/agents`-class — both declare `payload_class: "command"`):
  prove the typed `composer-nonempty` refusal with zero
  command bytes delivered and the prefill byte-identical, **or** — only if
  a proven atomic clear/replace has since been pinned — standalone command
  execution with zero concatenation (pane transcript shows the command's
  own UI, not an ordinary prompt echo). Prove the r7 carrier case: an
  **undeclared** streamed utterance `see /tmp/x`, split at the quiet-timer
  or cap boundary so a batch begins `/tmp/x`, is delivered as prose with
  no guard refusal and no disarm. Cover response loss (exact-id
  reconcile answers from the journal; never resend), crash windows
  (dead-owner sweep outcomes), and old/new compatibility (new client
  against a server without the `command_controls` block sends no v4 field
  and shows the guard-absent statement; old client against the new server
  is unchanged). Keep the Kimi/Claude live command acceptance (declared
  Compact against an empty composer executes) — with the r11 close rule
  asserting the record carries the execution evidence reference, and the
  artifacts (`lane-a-10.3` case-08/case-12 shapes re-run) showing
  `accepted` **with** evidence rather than `submission_observed: null`.
- **Live execution record (Lane A, head `d79785f`, 2026-07-28):** this
  acceptance ran on this host against kimi 0.29.2 and claude 2.1.220 (15
  cases; 14 pass + 1 red-by-design that caught and corrected the F1
  composer pin). Evidence:
  `docs/issues/native-tui-interaction-console/evidence/lane-a-10.3/` (on
  `feature/native-tui-console-lane-a`, per-case directories with sanitized
  captures and exact request/response JSON). Outcomes folded into this
  spec: D1 (paced menu delivery), D2 (no `/model` reasoning selector on
  0.29.2), D3 (mid-turn steer forms), L1 (Home/End), OD2 observables, F1
  (composer-emptiness pin scope, §4.1). The §6.4 grace pause behavior was
  verified live end-to-end (post-Enter batch refused `pane-busy`, paced
  navigation accepted after the grace).
- **Steer-state acceptance (cond-0031, §4.2):** installed Kimi — idle
  target: v2 `text`+`C-s` (`enter:false`) posts and parks, the record
  carries `unsubmitted`, and a fresh v3 key-only Enter after
  identity/capability recheck submits exactly once (transcript shows one
  submission, no doubled text); active target: v2 steer lands mid-stream;
  race: a turn completing between selection and delivery leaves the
  control unresolved (never "entered"), activated once by the Enter
  discipline; no second activation without the fresh identity+content
  proof.
- **Interactive-streaming acceptance (cond-0194, §6.7; menu behavior
  per-provider-pinned per r16):** active-turn
  disposable Kimi **and** Claude sessions — manual printable text lands
  and is visibly queued per provider semantics (honest submission
  observation, no inference); Stop interrupts.
  **Kimi 0.29.2 (required, passes live):** active-turn menu navigation
  delivers and `Escape` safe-cancels; queued text followed by declared
  `C-s` is consumed (steer effect proven).
  **Claude 2.1.220 (required honest-limit proof):** an active-turn
  `/model` (or effort command) is accepted and **queued in the native
  composer** — the acceptance proves the command queued, claims no menu
  open, no setting change, and no menu cancellation, and shows the active
  turn continuing unchanged. This is pinned provider behavior for this
  build (root-r15-claude-limit-125), not a CAO identity/lease/interactive
  failure; it is never classified as a product P1 and UI/evidence says
  **"queued command"**, never a menu action. True concurrent lease
  contention refuses `pane-busy` and
  disarms. Stale identity and copy mode refuse with zero bytes. Response
  loss resolves by exact id, never resend. Managed websocket input stays
  wheel-only (resize is geometry). Old/new capability combinations behave
  per §6.7's advertisement rules (fallback with honest notice, never a
  speculative bypass; old client unchanged). Desktop and mobile status
  evidence is captured for the armed banner, queued observation, and
  pause/disarm notices.

### 10.4 Web unit + component (Lane B; vitest)

- Extended recorder/capture mapping for §3.2 + §3.3 refusals; notation TS
  parser against the shared golden vectors; streaming batching/serialization/
  reconcile/pause/disarm logic (mock api); macro store client + scope grouping +
  server ordering rendering; built-in immutability UI; old-server capability
  degradation (each §3.5 row as a test); the §6.6 websocket-silence guard
  while streaming; layout smoke (header/strip/modal presence by viewport
  size via jsdom stubs where feasible).
- **Chord local-refusal (Sol P1-2 acceptance):** with the per-terminal
  advertised chord set held at arm — Claude terminal (empty set) pressing
  `Ctrl+S` → locally refused, **zero POSTs**; kimi terminal on an unpinned
  build pressing `Ctrl+S` → locally refused, zero POSTs; any arbitrary
  `Ctrl+letter` absent from the set → locally refused, zero POSTs. The
  deployed recorder's unconditional `C-s` shape
  (`web/src/lib/sequenceRecorder.ts:101-112`) does not survive into Lane B.
- **Atomic disarm (Sol P2-1 acceptance):** input arriving during a refused
  in-flight batch is discarded with the quiet timer cancelled — assert no
  second POST occurs and the trace retains only metadata; `unsupported`
  and unknown typed outcomes disarm identically.
- **Enter fusion + grace pacing:** a trailing Enter after pending text
  produces one fused request; on kimi, composer batches are withheld while
  `dispatch_grace_ms` runs after an accepted Enter-carrying batch.
- **Built-in ID stability:** synthesized built-ins always carry
  `builtin:<provider>:compact` / `builtin:<provider>:stop`; user records
  cannot claim the `builtin:` prefix; duplicate-by-id round-trips.
- **Session-card header (§7.6):** click/double-click/drag-select on name,
  badges, counts, and timestamps leaves `aria-expanded` and card state
  unchanged (selection intent, including via `window.getSelection()`);
  chevron pointer click and Enter/Space keydown toggle exactly once and
  update `aria-expanded`; chevron has an accessible name containing the
  session name and `aria-controls` resolves to the terminals region; tab
  order reaches the chevron before the terminals region with a visible
  focus ring; metadata has `user-select: text` and no click handler.

### 10.5 Browser + mobile evidence (Lanes B and C; new Playwright suite in
`web/`)

Projects: desktop Chromium 1280×800 and mobile Chromium 390×844
(device-scale, touch). Against a stubbed server (MSW-style route mocks or
the `cao_mcp_apps` harness pattern): header/strip/modal render and operate at
both widths; terminal keeps ≥ 50 % height at mobile width (measured on the
visible fitted `.xterm` child with row-quantization slack, §7.1); streaming arms,
captures, shows trace, and Stop-streaming works with touch; macro sheet
list→editor navigation; Lane C chip strip, picker, paste, and wrap behavior
against the §0 visual baseline; wheel scrolling works with streaming off and
on (synthetic wheel events); screenshots checked into the PRs as evidence.
Accessibility: axe-core scan of header/strip/modal/streaming/chip surfaces,
zero serious violations. **Session-card header (§7.6, both projects):** at
1280×800 and 390×844 — drag-select the session name and a timestamp, then
copy, with the card state unchanged throughout; chevron tap/click and
keyboard activation toggle and update `aria-expanded`; chevron touch target
≥ 44×44 px; metadata does not show pointer/press affordances.

### 10.6 Lane C installed live-provider acceptance

- Disposable **Claude** native-TUI session: staged PNG submitted via the
  operator-message path; transcript observation confirms the path reference
  reached the composer and the provider can read the staged file.
- Disposable **Kimi** native-TUI session (pinned 0.29.x): staged PNG
  submitted via the operator-message path; transcript observation confirms
  the path reference reached the composer and the provider's `ReadMediaFile`
  reads the staged file correctly. **Upstream capability already proven
  (2026-07-28, round 3):** a disposable native Kimi Code 0.29.2 session
  (session `session_4eea95f9-2873-4be9-920c-24566f6e37f6`, model K3) was
  handed ordinary composer text naming the absolute staged fixture path
  (120×80 PNG, 213 B, sha256 `02f597dc…bdd49`, left half red / right half
  blue by construction); the transcript shows `Used ReadMediaFile
  (…/.conductor/reports/r3/stripe-fixture.png) · image (image/png, 213 B)`
  and the correct observation "red on the left half and blue on the right
  half" (committed sanitized evidence:
  `docs/issues/native-tui-interaction-console/evidence/kimi-0.29.2/` —
  README, fixture, transcript excerpt with SHA-256s; full raw artifacts
  remain under `.conductor/reports/r3/`). The proven prompt used the
  explicit `ReadMediaFile` directive form, so §8.6 pins that phrasing as
  kimi's `reference_template`; bare-path substitution is not claimed. What
  remains for
  Lane C is the *server-path* acceptance above (operator-message operation +
  token substitution), not the upstream capability.
- At-most-once drill: killed response mid-submit → reconcile by
  `operation_id` → no duplicate provider submission.

### 10.7 Compatibility and regression gates

- Old-client/new-server: the deployed `terminalView.test.tsx` **wire-shape
  assertions** pass unchanged against the new capabilities shape
  (additive-only assertion). **Repins (named):** §7.1 removes the standalone
  Compact button and absorbs the recorder row into the modal, so the
  deployed cases that pin those controls — the Compact-by-role cases
  (`terminalView.test.tsx:216-303`, `:509`) and the recorder-row cases
  (`:515-686`, including the v3-fallback notice at `:676-684`) — are
  re-pinned to the favorites-strip built-in and the modal recorder
  respectively; behavior assertions (identity-bound send shape, wheel
  gating, refusal taxonomy, reconcile, cancel-writes-nothing,
  v3-absence degradation) survive and must not be weakened.
- New-client/old-server: §3.5/§8.6 degradation tests (10.4).
- The deployed literal composer retains its identity binding, its single
  in-flight `controlBusy` send discipline (there is deliberately no client
  queue, §1.5), and no-bracketed-paste behavior: the deployed Python + web
  suites are the
  regression gate; §10.1 capabilities golden diff proves additive-only.
- Identity/no-duplicate: deployed journal/arbiter suites unchanged and
  green — including the deployed same-ID re-arm-after-refusal cases
  (`test_control_input_endpoint.py::TestConcurrencyAtTheBoundary`,
  `test_control_input_journal.py:484-514`), which pin the exact semantics
  §3.4 now states; new streaming/message tests assert never-resend and
  exact-id reconcile.

### 10.8 CI integration

Default `uv run pytest` and `npm test` stay green; the live tiers run
on-demand (`-m e2e`) per deployed gating; Playwright runs in the Lane B/C CI
jobs with the two projects of §10.5.

## 11. Deployment and rollback

- **Deployment:** additive server capabilities + new store files + new
  dashboard UI, per lane in §9 order. No database migration; the
  control-input journal schema is untouched. Server first within each lane.
- **Rollback:** revert the lane PR. New server with old dashboard: old
  dashboard ignores additive capability keys; literal path byte-identical.
  Old server with new dashboard: §3.5/§8.6 degradation. `macros.json` and
  `attachments/` are inert to a rolled-back server (unread files; left in
  place — deleting them would destroy operator data and is never part of
  rollback). Lane C's routes simply 404 on a rolled-back server and the
  dashboard hides the affordances.
- **Feature flags:** none required; capability advertisement *is* the flag.

## 12. Priority partition

- **P0 (blockers):** none found. Baseline is green (§1.7); no defect on the
  base blocks starting Lane A.
- **P1 (compatibility core — ships first):** §3.2 key-set extension + tmux
  mapping + §3.3 refusal completion; §4 registry (Compact/Stop/Steer blocks)
  + capabilities; §10.1-10.3 tests including live navigation acceptance; the
  built-in Compact/Stop favorites *data* (registry entries); the §4.1
  command-class guard (`composer-nonempty`) that keeps provider commands
  from concatenating with composer prefill (r5, F14). Rationale:
  navigation keys and provider controls are currently lost when a session
  becomes managed; every later feature rides on them.
- **P2 (product overhaul — after P1 merges):** §5 macro store + notation +
  library UI; §6 streaming; §7 layout overhaul; §8 Lane C operator-message
  composer; §10.4-10.6. The streaming and message *mechanics* are spec'd now
  so later lanes need no contract amendment; *polish* (trace ergonomics,
  coalesce tuning, chip styling) is P2.
- **Explicitly out (non-goals, restated):** reconstructing physical keyboard
  state the browser/terminal/provider cannot distinguish; shell-script
  macros; cloud sync of macros; hard-coded dashboard copies of provider
  menus; clipboard-injection image delivery (impossible by construction,
  §8.2); kimi clipboard-paste image delivery and non-PNG kimi formats
  (unproven on the pinned build — refused, F9).

## 13. Unresolved decisions (flagged for product/supervisor)

- **OD1 — Multi-modifier chords.** The brief's acceptance lists "supported
  multi-modifier chords". No managed provider has evidence of consuming any
  multi-modifier chord, and chords without legacy byte encodings cannot be
  delivered faithfully (Appendix A.1/A.3). This spec ships the admission
  mechanism (registry-pinned chord events) with an empty multi-modifier table
  and honest refusals. Admitting e.g. `C-Up` requires per-provider,
  per-build evidence — recommend P2 follow-up issue with live experiments.
  **Needs product sign-off** that "supported multi-modifier chords" is
  satisfied by "the registry mechanism + refusal honesty, zero admitted
  chords today".
- **OD2 — RESOLVED live (r9, Lane A case-06/13).** Kimi Stop = Escape
  verified: kimi 0.29.2 shows `Interrupted by user` on Escape mid-turn;
  claude 2.1.220's spinner disappears (this build leaves no textual
  marker — spinner-gone is the pinned observable). Registry Stop entries
  stand as pinned.
- **OD3 — Codex built-ins.** No codex native control adapter or native-TUI
  launch binder exists on this base (§1.4), so codex Compact/Stop cannot be
  delivered through the managed path. The brief's "as supported" phrasing is
  read as conditional: codex is excluded, registry and UI designed to admit
  it without schema change. **Confirm** that enabling codex native TUI is a
  separate provider-enablement track, not this one.
- **OD4 — Coalesce defaults.** 200 ms quiet window / 48-char text flush are
  starting values, server-advertised (§3.5) so they can be tuned without a
  client release.
- **OD5 — RESOLVED live (r9, L1).** tmux injects `ESC[1~`/`ESC[4~`
  regardless of the pane's DECCKM mode (Appendix A.3, source-observed, not
  man-page-documented). Live §10.2/§10.3 prove the bytes arrive exactly
  as pinned; kimi 0.29.2's `/model` menu does not consume Home/End (its
  own binding gap, not an encoding defect), while the composer line
  editor consumes them. Keyboard transport guarantees stand; no menu
  effect is promised anywhere; no registry restriction was needed.
- **OD6 — Operator-message at-most-once store.** §8.3 pins the provider
  adapter's operation store (deployed, journaled, reconcile-ready) rather
  than a third journal. Review noted the trade (Fable B2): one
  frozen-`ambiguous` operator message freezes every managed operation on
  that native session, and the id-alone reconcile route spans two
  per-provider stores; the alternative — a dedicated operator-message
  journal mirroring `control_input_journal` — is recorded in §17 backlog
  for Lane C to weigh at implementation time.
  Flagged so the choice is conscious.
- **OD7 — RESOLVED by live evidence (round 3, 2026-07-28).** The earlier
  refusal plan rested on an inference from the predecessor kimi-cli
  changelog's 0.43 clipboard-paste entry. The pinned Kimi Code 0.29.2 build
  in fact delivers staged-path images: a disposable native session invoked
  the provider's own `ReadMediaFile` on a staged absolute PNG path and
  observed the known visual fact correctly (§8.2, §10.6). The spec therefore
  supports pinned kimi staged-path image delivery (PNG only). Retained
  honesty: clipboard-paste delivery stays unproven/refused, non-PNG formats
  stay refused (F9), and any future build drift re-verified by §10.6.
- **OD8 — Message text limit.** 8192 bytes is a spec pin, not a measured
  provider limit (no provider documents one). Lane C live acceptance may
  lower it per evidence; raising it requires re-review.

## 14. Findings register (from reconciliation; P-ranked)

- **F1 (P2, resolved by design D5):** `settings.json` has no schema version
  and non-atomic writes (`settings_service.py:37-40`) — unsuitable for the
  macro library; the store is a new versioned file instead. No change to
  settings_service in this track.
- **F2 (P2, deferred):** the v2 native *admission* write path
  (`managed_launch_v2.py:4533`) writes through `TmuxPaneInput` without the
  pane-input lease (idle-gated and adapter-journaled instead). Pre-existing;
  reachable only over WRITE/ADMIN-scope HTTP (server-to-server, never from
  browser code — the websocket admits no such bytes, §6.6), and disjoint
  from this track's lanes;
  Lane C deliberately does not reuse it (§8.3 takes the lease itself).
  Recommend a follow-up issue to bring the admission path under the arbiter.
- **F3 (P3, noted):** v2 results report `request_schema_version: 1`
  (`control_input_service.py:1007-1010` — "v1 and v2 results both report
  1"). Cosmetic; fix opportunistically if Lane A touches that code.
- **F4 (P2, verification pinned in §10.3):** tmux Home/End injection ignores
  DECCKM (OD5).
- **F5 (P2, accepted into plan):** no web e2e infra; Lanes B/C add
  Playwright to `web/` (precedent: `cao_mcp_apps/`). New dev dependency —
  flagged.
- **F6 (P2, design constraint):** no normative list of browser-reserved
  shortcuts exists (Appendix A.4); refusal UX is designed for unobservable
  keys (static messaging) rather than per-key detection.
- **F7 (P3, noted):** the arbiter's own docstring still lists ordinary
  delivery as "once it is migrated" (`pane_input_arbiter.py:12-14`);
  ordinary `/input` is in fact leased today (`terminal_service.py:2418`).
  Comment drift only.
- **F8 (P1-UX, resolved by Lane C):** the deployed composer's 512-byte
  control-input cap refuses real operator messages with a correct-but-
  surprising 422 (the owner's 866-byte case). Resolved by the §8.5 routing
  rule and the §8.3 operator-message operation — without enlarging
  control-input (D11).
- **F9 (P2, design constraint):** no provider CLI documents image
  format/size limits; only Anthropic's API-level limits are documented
  (Appendix A.9). Lane C therefore pins CAO-side limits (§8.3) and treats
  undocumented provider behavior as refused.
- **F10 (P1-spec, corrected in r4):** the r1-r3 spec misstated deployed
  same-ID semantics ("duplicate control_id + identical binding replays the
  stored answer") — after a typed `refused` the deployed journal re-arms
  (`refused → intent`, `control_input_journal.py:711-742`) and the retry may
  write; only lookup and `delivered`/`ambiguous` terminal replay are
  zero-I/O. Corrected in §1.2/§3.4 (Sol P1-1, accepted).
- **F11 (P2, corrected in r4):** the spec's registry interface
  (`controls_for(provider)`) could not enforce its own exact provider+build
  chord rule, and §6.2 allowed server-gated chords that §3.5 forbids
  sending — corrected to build-exact `controls_for(provider,
  provider_version)` with local capture refusal (Sol P1-2, accepted).
- **F12 (P2, corrected in r4):** three streaming-shape defects — Enter
  split from its text across leases (arbiter docstring's own interleave
  case), kimi's 5 s dispatch grace turning §6.4's disarm-on-refusal into
  self-disarm after every line, and the eleven new keys' readiness-gate
  class left as an open question — corrected in §6.3/§6.4/§3.2/§10.3
  (Fable P1-1/P1-2/P1-3, accepted).
- **F13 (P3, annotated):** `baseline-dashboard.png` predates the cond-0175
  recorder row (the deployed bundle renders it whenever the composer is
  visible); the baseline is annotated rather than re-captured (§0/§1.5),
  and §10.5's visual acceptance measures the current deployed render.
- **F14 (P1, amended in r5):** live owner evidence (terminal `f4d25eb9`,
  record `root-fable-agents-panel-013`) proved a provider command
  concatenates with composer prefill that survived Escape and is submitted
  as ordinary prompt text — identity-bound posting without standalone
  command execution. Root causes pinned: no composer-emptiness gate exists
  for command payloads, and the installed conductor schema (0.1.7) strands
  the honest partial state (`posted → partial-ambiguous` illegal; only
  `completed`, requiring observed completion). Amended as the §4.1
  command-class contract (`composer-nonempty` typed refusal, prohibited
  blind clearing, honest ambiguity with manual reconcile, guard
  capability advertisement) + §10.1/§10.3 acceptance. The conductor-side
  schema edge is a supervisor-routed coordination item, not edited here.
  The r7 follow-up (Fable P1-Δ2, root-confirmed) added the missing
  declaration carrier: optional `payload_class: "command"` under request
  schema v4 (own digest domain, v2-chord amendment pattern); command-class
  is never shape-derived, so streamed prose beginning `/` can never trip
  the guard.
- **F15 (P1, planned in r10):** cond-0031 (cross-ref cond-0072) — a v2
  text+C-s steer control against an **idle** Kimi receiver parks the text
  (the chord steers only an active turn), while `accepted`+`chord_sent`
  reads as if it entered. Live evidence: steer envelopes
  `root-lane-a-native-status-024` / `root-lane-b-native-status-023`
  (posted, `resolution: null`), each activated exactly once afterward by a
  fresh v3 key-only Enter with no text replay. The §4.2 plan pins
  state-aware selection, separate-fresh-control activation, no blind
  replay, truthful `submitted`/`unsubmitted`/`unknown` outcomes, and
  focused acceptance. The v1/v2 path carries no turn-state gate (§1.2), so
  selection is caller discipline plus the pinned future steer-observation.
- **F16 (P1, blocking PR #48; corrected in r11):** the declared-command
  path closed records `accepted`/`delivered` with
  `submission_observed: null` — execution unobserved, read as success
  (Sol/high review of `d79785f`, reproduced against case-08/case-12
  artifacts). §4.1 rule 3 now pins the two-shaped close (accepted only
  with journaled execution evidence; `ambiguous`/`submission-unproven`
  otherwise; `provider-unsupported` without an execution pin) and the
  command-execution observation as a required per-build pin beside the
  emptiness determination. Same root theme as F15 (unobserved effect
  recorded as success) but a distinct path — v4 declared commands, not
  the v2 steer surface; cond-0031 is not duplicated.
- **F17 (P1, owner override applied in r15):** cond-0194 — the deployed
  readiness gate made manual interactive streaming self-disarm during
  provider turn activity, which the owner ruled is **not** a write
  prohibition for manual streaming (queue/enter text, navigate/cancel
  menus, Stop, supported steering must work mid-turn). Amended as §6.7:
  a declared `payload_class: "interactive"` under existing schema v4
  bypasses **only** the turn-state gate and dispatch grace, with every
  other guard preserved and automation fenced out by declarer
  discipline. Automated delivery remains readiness-gated. The r16
  follow-up keeps acceptance honest per build: Kimi 0.29.2 active-turn
  menu navigation + Escape cancel + queued-text→C-s steer passes live;
  Claude 2.1.220 queues `/model`-class commands mid-turn without opening
  a menu (pinned provider limit, not a CAO defect, never reported as a
  menu success — UI/evidence says "queued command").

## 15. Challenges applied (per track mandate)

- **Second write path:** rejected everywhere. Streaming/macros/provider
  controls reuse the one control-input route (D2/D3); the operator-message
  path is a *sibling typed operation* with the same arbiter, identity, and
  reconciliation discipline — not a bypass (D11, §8.3); the websocket input
  frame stays wheel-only for managed panes with test-enforced guards (§6.6).
- **Weakened identity binding:** rejected. Every streaming batch and every
  operator message carries the full 9-field `expected_identity`; the server
  re-proves pane, window, pid, server socket, generation, and
  provider/native session under the lease before the first byte (deployed,
  §1.2). No "stream token" or lease hand-off exists; the lease is acquired
  per operation.
- **Encodings the browser/terminal cannot preserve:** refused, never
  approximated (§3.3): multi-modifier and non-letter chords, case-distinct
  ctrl chords, browser/OS-owned combinations, IME composition during
  streaming. Home/End's tmux encoding caveat is named (OD5) rather than
  claimed away. Clipboard image bytes are undeliverable by construction —
  Lane C stages files instead of pretending (D12, §8.2).

## 16. Handoff for independent spec review

A reviewer can verify this spec by: (1) checking §0 pins against the repo;
(2) spot-checking §1 file:line claims (all verified at the base SHA);
(3) confirming §3.2's byte claims and §8.2's provider claims against
Appendix A primary sources plus the round-3 live kimi artifacts referenced
in §10.6; (4) walking one P1 sequence (`text("/model")
enter up*3 enter`) and one Lane C submission (PNG + 1 KB text to claude)
through §3/§4/§8/§10 end-to-end; (5) confirming no section introduces a
write path outside §1.2's and §8.3's routes. The decisions most likely to
deserve challenge are D1 (in-place key-set extension vs schema v4), D5
(JSON store vs SQLite), D11/D12 (message-path separation and staged-file
images), and OD1 (zero admitted multi-modifier chords) — each records its
alternative and rejection reason.

## 17. Backlog (recorded at r4 review; not Lane A/B/C scope)

Product proposals (Fable R-series — non-binding, need owner approval;
streaming remains the specified capability and is **not** replaced or
weakened by R1):

- **R1 — Deliberate "burst" input mode:** promote the recorder to a
  first-class input bar delivering one fused v3 request per deliberate
  send. Recorded as a possible spec'd Option B; adopting it requires
  owner approval and would subsume much of §6's batching machinery.
- **R2 — Prompt-response quick actions:** registry-mapped response
  sequences for provider permission/trust/confirm dialogs, surfaced only
  while the status layer reports a pending prompt.
- **R3 — Intent-based composer routing** (command vs prose by kind rather
  than the §8.5 size rule); would resolve B3.
- **R4 — Operator-activity timeline per terminal** merging the journals
  into one read-only feed.
- **R5 — "Send when idle" one-shot armed retry chip** for `pane-busy`
  refusals (client-only; each attempt a fresh id).
- **R6 — Small conveniences:** one `{1}` placeholder in macro notation
  (client-side substitution); per-terminal draft persistence in
  localStorage; local ghost-token echo of the pending batch.

Technical backlog (recorded; each names its trigger for promotion):

- **SQLite/CAS attachment ledger** (full form of Sol P2-2) — deferred per
  the owner speed guard; promote if attachment contention or crash-window
  evidence ever outgrows the §8.4 manifest+lock discipline.
- **B1 — GLM route caveat:** a `claude_code` pane may run glm-5.2; the
  registry's image/compact entries derive from Anthropic evidence — key or
  caveat the registry by route when such a profile is first used.
- **B2 — Dedicated operator-message journal** (OD6 alternative): removes
  the frozen-`ambiguous` contagion across one session's managed
  operations; Lane C weighs at implementation time.
- **B3 — §8.5 length-based routing makes idle-gating an accident of draft
  size** (R3 addresses).
- **B4 — Streaming per-batch server cost** (tmux subprocesses + DB reads
  per ≤200 ms batch) is unbudgeted; measure during Lane B.
- **B5 — Two hard-coded web key allowlists** (recorder/capture) must be
  named as Lane B deliverables and derived from capabilities.
- **B6 — Byte-exactness proofs for the eleven new keys live only in the
  `-m e2e` tier;** consider one default-suite live test when tmux exists.
- **B7 — The attachment sweep's "periodically"** is new infrastructure
  (deployed precedent is a one-shot startup sweep with a 14-day cutoff);
  Lane C names the mechanism it actually builds.
- **B8 — Namespace distinction:** `services/native_attachment.py` already
  owns "attachment" for provider-session ownership; Lane C's image store
  must read distinctly (e.g. "image attachments" in routes/docs).
- **B9 — §5 macro validation must also run the service-layer key/chord
  membership screens** (contract normalization deliberately skips
  membership), so an unsendable macro cannot be saved.
- **B10 — v4 GET replay wire consistency:** the journal stores no
  `payload_class`, so a replayed v4 record reports `request_schema_version:
  3` (§4.1 known limitation, accepted r9). A later journal-schema revision
  may persist the declaration; the digest binding already protects
  rebound, so this is consistency, not safety.
- **B11 — Macro pacing directive (D1 product decision):** menu-navigation
  macros sent as one fused sequence race picker mounts (kimi live D1);
  deterministic menu automation today uses paced sends (streaming or
  separate requests). Whether the macro player gains an explicit pacing
  directive (e.g. a per-macro paced-send flag splitting one macro into
  timed batches) is an owner product decision, not pinned here.
- **B12 — kimi 0.29.0/0.29.1 composer-emptiness pins:** declared commands
  refuse `provider-unsupported` on those builds until their composer
  regions are live-verified (§4.1 pin scope); the verification is a small
  future live pass, not a guess.

Deferred P3/P4 (noted by review; no action this round): wheel-filter
shape-only matching; `CAO_HOME_DIR/tmp/{terminal_id}.*` as an additional
staging-precedent citation; §8.3 could name the deployed
`managed-operations` follow-up as the operator-visible long-message
parallel; F2's wording now records that the unleased admission write is
WRITE-scope HTTP-reachable (not browser-reachable); assorted ≤1-line
citation nits.

## Appendix A. Primary sources (external claims)

- **A.1 tmux key names and modifiers:** [tmux(1) man page](https://man7.org/linux/man-pages/man1/tmux.1.html) — special key names "Up, Down, Left, Right, BSpace, BTab, DC (Delete), End, Enter, Escape, F1 to F12, Home, IC (Insert), NPage/PageDown/PgDn, PPage/PageUp/PgUp, Space, and Tab"; modifiers "C- or ^", "S-", "M-"; `send-keys` name lookup vs `-l` literal ("if the string is not recognised as a key, it is sent as a series of characters"). Alias table: [tmux `key-string.c`](https://github.com/tmux/tmux/blob/master/key-string.c).
- **A.2 xterm encodings:** [XTerm Control Sequences](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) — cursor keys `CSI A/B/C/D` normal vs `SS3` application per DECCKM ("The cursor keys transmit the following escape sequences depending on the mode specified via the DECCKM escape sequence"); VT220 editing keypad `CSI 2~/3~/1~/4~/5~/6~` unaffected by DECCKM; backspace 0x08/0x7f per DECBKM.
- **A.3 tmux injection translation:** [tmux `input-keys.c`](https://github.com/tmux/tmux/blob/master/input-keys.c) — cursor keys carry `KEYC_CURSOR` variants (`\033OA` vs `\033[A`), gated by the pane's application mode ("If not in application keypad or cursor mode, remove the respective flags from the key"); Home/End hard-coded `\033[1~`/`\033[4~`; Ctrl letters converted to C0 bytes (`key & 0x1f`) so `C-s` injects `0x13` regardless of keyboard-enhancement protocols; Meta as ESC prefix. Unverified-by-man-page items are flagged as source-observed.
- **A.4 Browser keyboard:** [UI Events key values](https://w3c.github.io/uievents-key/) (standardized `"ArrowUp"`, `"Enter"`, …), [UI Events spec](https://w3c.github.io/uievents/) (shifted vs unshifted `key`), [MDN keydown](https://developer.mozilla.org/en-US/docs/Web/API/Element/keydown_event) ("fired for all keys"), [WICG Keyboard Lock](https://wicg.github.io/keyboard-lock/) (keys "normally reserved by the underlying host operating system" never reach pages without it), [Chrome keyboard shortcuts](https://support.google.com/chrome/answer/157179) (vendor list, treated as non-exhaustive — no normative enumeration exists).
- **A.5 xterm.js interception:** [xterm.js typings](https://github.com/xtermjs/xterm.js/blob/master/typings/xterm.d.ts) — `attachCustomKeyEventHandler` "is run before keys are processed, giving consumers of xterm.js ultimate control as to what keys should be processed"; built-in encoding reference [Keyboard.ts](https://github.com/xtermjs/xterm.js/blob/master/src/common/input/Keyboard.ts).
- **A.6 Kimi keyboard reference:** [Kimi Code Docs — Keyboard Shortcuts](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/keyboard.html) — "Esc: Close a popup / cancel completion / interrupt streaming output or context compaction"; "Ctrl-C: Interrupt the current streaming output".
- **A.7 Accessibility:** [WCAG 2.2](https://www.w3.org/TR/WCAG22/) — SC 2.5.8 Target Size (Minimum) 24×24 CSS px; SC 1.4.3 Contrast (Minimum) 4.5:1. Apple HIG 44×44 pt touch target as the design goal.
- **A.8 Kimi arrow-key menus (supporting):** [Kimi CLI slash commands](https://moonshotai.github.io/kimi-cli/en/reference/slash-commands.html) — "Use arrow keys to select …, press Enter to confirm" (session selector; model/reasoning selectors verified live in §10.3).
- **A.9 Claude Code images:** [Claude Code common workflows — Work with images](https://code.claude.com/docs/en/common-workflows) — "Drag and drop an image … paste it into the CLI with Ctrl+V … Provide an image path to Claude. E.g., 'Analyze this image: /path/to/your/image.png'"; [interactive mode](https://code.claude.com/docs/en/interactive-mode) — paste "Inserts an `[Image #N]` chip at the cursor". Limits: [Anthropic vision docs](https://docs.claude.com/en/docs/build-with-claude/vision) — "JPEG, PNG, GIF, or WebP", "Maximum 5MB per image", larger than 8000×8000 rejected (API-level; no CLI-level limits documented). App-side clipboard capture corroborated by [anthropics/claude-code#43942](https://github.com/anthropics/claude-code/issues/43942).
- **A.10 Kimi image support by version:** [kimi-cli changelog](https://moonshotai.github.io/kimi-cli/en/release-notes/changelog.html) — 0.43 "Support image input if the LLM model supports it" (first *clipboard-paste* entry), 0.83 `ReadMediaFile` tool, 0.85 "Cache pasted images to disk", 1.41.0 xclip/wl-paste headless fallback; [interaction guide](https://moonshotai.github.io/kimi-cli/en/guides/interaction.html) — "`Ctrl-V` to paste text, images, or video files … cached to disk and displayed as an `[image:…]` placeholder". No format/size limits documented. **Note (round 3):** these entries describe the predecessor kimi-cli's paste UI; they do not bound the pinned Kimi Code 0.29.2 build, whose staged-path image capability is proven live in §10.6 — the round-1 inference from this changelog was superseded by that evidence.
- **A.11 Codex images (recorded for the future codex track):** [Codex CLI docs](https://developers.openai.com/codex/cli/) — "`codex --image` … or paste an image into the interactive composer"; [CLI reference](https://developers.openai.com/codex/cli/reference) — "`--image, -i | path[,path...]`"; composer renders `[Image #N]` placeholders and auto-attaches pasted image paths (source: `chat_composer.rs`, paste-handler only — typed paths unverified). Codex has no managed native-TUI path on this base (§1.4).
