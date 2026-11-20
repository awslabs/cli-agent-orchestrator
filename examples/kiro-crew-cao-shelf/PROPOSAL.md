# Proposal: CAO Shelf, a Kiro Crew App

Status: the design record. The app in this folder implements it; where the build or a review
changed a decision, the text below was corrected to match (the README says what was verified).
Companion files: [api-contract.md](api-contract.md) and [schemas/](schemas/) (the contract),
[fixtures/](fixtures/) (anonymised data and expected outputs), [SPIKE.md](SPIKE.md) (verified Kiro
Crew and CAO behaviour), [design/](design/) (the finished mock and screenshots).

**Targets.** Kiro Crew **v0.12.2** (latest stable at 2026-11-15; `v0.13.0-rc.1` is a pre-release and is
not targeted). CAO **2.5.4 and current main** (`31393711`, which still reports 2.5.4 but is 201 commits
past the tag).

**Evidence tags.** `[V: ...]` verified, with the file at the tag or the command I ran (details in
SPIKE.md). `[I]` inferred, not exercised. Where a sentence carries no tag it is design, not a claim.

## 1. Summary

CAO's operators run dozens of sessions that are almost all idle. CAO's own pages show them as a flat,
profile-grouped list with a headline that calls idle terminals "running", no "needs me" view, no
hierarchy and no history. The **CAO Shelf** is a Kiro Crew sidebar page that shows each CAO
session as one card (conductor as the title, helpers as chips), sorted into four tiers (NEEDS YOU,
LIVE, PARKED, STALE), with a History drawer for helpers that CAO has already deleted, and four
actions: ask a conductor, answer a waiting helper, start a session, and clean up stale sessions
behind a listed, unticked, confirmed stop.

It is a Kiro Crew **App** with a small stdlib-only Python backend that polls CAO on loopback and a
prebuilt React page. It is built as an example in the CAO repo, not as a Kiro Crew built-in
(Kiro Crew accepts no new built-ins) [V: `docs/app-kit/README.md`, `publishing-guide.md` s10 at v0.12.2].

**Recommendation: build it, in four phases (section 17), with three corrections to the concept
(section 4.3) that change what the UI may promise.**

## 2. Problem and evidence

Evidence is a read of a live 2.5.4 server (GET only; key sets and types kept, no names or text) and
was cross-checked in this spike (see [SPIKE.md](SPIKE.md)).

| Observation (12 sessions, 20 terminals) | Consequence for the UI |
|---|---|
| 20 of 20 terminals `idle`, 0 `processing`, 0 `waiting_user_answer`; 9 of 20 idle over 48 h | A list that ranks by status shows nothing; age is the signal |
| Exactly one conductor per session, 0 to 3 other terminals; 8 sessions are a lone conductor | The session is the unit, the conductor is its face |
| 4 of 20 terminals have `caller_id`; 1 points at a terminal that no longer exists | Hierarchy and orphans come from `caller_id`; one orphan exists |
| 239 of 255 inbox rows were sent by a terminal that no longer exists (94%) | Closed helpers are visible only through inbox rows |
| CAO web Home: workers grouped by profile name; "running agents: 20"; no tiering; no history | See section 4.1 |
| CAO polls 1 + 12 + 20 = 33 GETs every 10 s on Home | A per-session poll costs far less (section 15) |

[V: the read of a live 2.5.4 server (GET only); `web` Home read there. Session `status` is `active` for every session
on herdr and `detached` on tmux, never a health signal: SPIKE 3.1.]

## 3. Goals and non-goals

Goals

1. One glance answers: does anything need me, what is live, what is quietly rotting.
2. Show a session as one object with a conductor and helpers, with orphans flagged.
3. Never call idle "running". Say "idle 2 h", computed from `last_active`.
4. Keep the closed helpers visible for a while, labelled as rebuilt from inbox rows.
5. Let the user act without leaving the page: ask, answer, start, stop (with a confirm that cannot
   stop anything that was not listed and ticked).
6. Stay cheap: requests grow with sessions that are hot, not with terminals (section 15).
7. Ship so a user installs it with one command and a clear trust step (section 12).

Non-goals

* No cost, token or progress-percent display (CAO exposes none) [V: INVENTORY.md section 3].
* No timeline of "what happened while away" (cards show state, not events).
* No terminal viewer or log reader. The page links nowhere into a terminal in v1.
* No new orchestration state in Kiro Crew; CAO stays the source of truth. The only things the App
  stores are its settings, the tier-toggle choice (browser) and an optional ledger of closed-helper
  ids and times.
* No remote CAO, no auth-enabled CAO in v1 (no secrets in the App), no path-prefix hosting (section
  9.5), no Windows (CAO needs tmux or herdr).
* Not a replacement for `ui://cao/dashboard` or CAO's web UI.
* No AG-UI dependency. AG-UI is off by default [V: INVENTORY.md; `docs/agui.md`]; an optional
  approve/deny route through it is a Phase 3 stretch, not a requirement.

## 4. Why a custom App, and where the concept was wrong

### 4.1 The demonstrated gap versus MCP Apps (issues #580, #582)

#580/#582 allow a custom KiroCrew App Kit dashboard "only when a demonstrated requirement cannot be
met through MCP Apps" and want "a specific unmet requirement and rejected smaller alternative
recorded". This is that record.

What `ui://cao/dashboard` is [V: `cao_mcp_apps/src/dashboard/Dashboard.tsx`,
`services/ui_state_service.py`, `docs/mcp-apps.md`, all at main `31393711`]:

* A flat grid of one tile per terminal. "Supervisors first" is `agent_profile` containing the text
  "supervisor". No session card, no conductor/helper tree.
* Snapshot fields per terminal: `id, session_name, provider, agent_profile, window, status,
  last_active, runner_id`. **No `caller_id`, no `pending_inbox_count`, no inbox rows.**
* One session list poll every 30 s through `render_dashboard`; hosts render it in a sandboxed
  null-origin iframe.
* Off by default (`CAO_MCP_APPS_ENABLED`) and only reachable through a `cao-mcp-server` registered
  with Kiro Crew's MCP gateway.

Requirements the shelf has and the dashboard does not meet, each with its evidence:

| # | Requirement | Why MCP Apps as shipped cannot meet it | Evidence |
|---|---|---|---|
| G1 | Hierarchy from `caller_id`, orphans flagged | Field absent from the snapshot; getting it needs one `GET /terminals/{id}` per terminal | snapshot builder above; SPIKE 3.3 |
| G2 | Idle tiers by `last_active` age with a stale bucket and an action to clean it | Field present, no tiering, no hide/count, no stop action beyond `submit_command` kinds | Dashboard.tsx |
| G3 | History of deleted helpers | Needs inbox reads (up to 100 rows per conductor); no tool or snapshot field does that | `ui_state_service` |
| G4 | NEEDS YOU first, with an answer path that actually works | Dashboard has none; an answer must go through `/terminals/{id}/input`, not an inbox message | SPIKE 3.5, 3.6 |
| G5 | A persistent sidebar page that also remembers tier toggles | MCP App iframes are null-origin with no storage in Kiro Crew's host | INVENTORY.md section 4 `[I]` from v0.8 docs; re-check at 0.12.2 in Phase 0 |
| G6 | Works without turning on CAO's MCP Apps surface or registering an MCP server | Dashboard needs both | `docs/mcp-apps.md` "Enabling" |

Smaller alternatives considered and rejected:

1. **Extend `ui://cao/dashboard` upstream** (add `caller_id`, tiers, inbox history to
   `build_dashboard_snapshot`, regroup the view). Rejected as the *first* step, not forever: it
   changes CAO's public snapshot and a bundle with a size budget (`npm run check:size`), it still
   lacks G5 and G6, and it would need `render_dashboard` to read inboxes server-side. **It is
   recorded as the follow-up that would make this App thinner**; the pure logic in
   `backend/cao_shelf/tiering.py` is written so it can be ported (Phase 4 question to the CAO
   maintainers).
2. **A Kiro Crew `commands` row that prints the grouping as text** (the concept's `/crew shelf`).
   Cheap and useful in the workshop, but it cannot answer, stop or show history, and it has no
   persistent view. Kept as an optional extra, not the solution.
3. **Use `ui://cao/dashboard` and add only the missing bits in a second view.** Two UIs would drift;
   rejected.

Honest limits of this argument: G5 rests on the v0.8 research and must be re-checked against 0.12.2's
MCP Apps host; if MCP Apps in 0.12.2 can persist state and a CAO change adds G1 to G3, the case for a
custom App weakens to "a nicer layout". That is why Phase 0 and Phase 4 include a conversation with
the issue owners (decision 1 in section 18).

### 4.2 Relationship to `ui://cao/dashboard`

Complementary. The dashboard lives inside a chat transcript and is driven by a model call; the shelf
is a standing page. They read the same CAO REST surface. The shelf does not use `/events` or AG-UI,
so it works on a default-config CAO. If both are enabled nothing conflicts.

### 4.3 Corrections to the concept (these change what the UI may promise)

| Concept says | What the spike found | Decision |
|---|---|---|
| Answer a waiting helper with `send_session_message` "as typed input" | `send_session_message` is an inbox message (`send_message` orchestration). For providers that block orchestrated input while `waiting_user_answer`, CAO will not paste it and keeps it `pending` [V: `terminal_service.py:235-261`]. The call that works, and that `answer_user_prompt` uses, is `POST /terminals/{id}/input?message=` [V: ran it on a `waiting_user_answer` mock terminal] | Answer via `/input` after re-checking status. Ask-the-conductor still uses the inbox route (the conductor is idle or completed, so it delivers) |
| `get_terminal_status` per terminal only for non-idle sessions | One `GET /sessions/{name}` returns every terminal's `status`, `last_active`, `pending_inbox_count` [V: scratch + live] and works on 2.5.4. `/sessions/{name}/terminals` has `status` only on main, not 2.5.4 [V: `367f53f3`, live null] | One read per session; `caller_id` is the only per-terminal read, once per id, cached |
| History drawer lists closed helpers by name ("planner", "coder-2") | A closed helper leaves only its 8-hex id in the inbox; no name, no profile [V: inbox row key set] | Show `helper 7a13`. Names in the mock are aspirational. If the CAO events surface is on, a later phase could join names (events ring buffer), not v1 |
| "Closed 26 h ago" | The last inbox row time is the best available | Say "last message 26 h ago" |
| "Handoff in flight" | Handoff and assign cannot be told apart from rows | Say "conductor working, helper `x` working" |
| Needs-you = `waiting_user_answer` | `error` also needs a human (the mock edges it red) | NEEDS YOU includes `error` (red edge) |
| Tier toggles "remembered" | Fine, in `localStorage` [V: works in the in-process page] | Keep; nothing else is remembered in the browser |

## 5. User stories and acceptance criteria

Fixtures: `fixtures/cao/{quiet,busy}` replayed by the fake CAO; expectations in `fixtures/expected`.

**US-1 See what needs me.** As an operator I open the page and see sessions that need an answer first.
* AC1.1 Busy day: first section is NEEDS YOU with `payments-fix`; its card shows the asker `tester`,
  "about 4 min", and Approve/Deny (or an answer box).
* AC1.2 The sidebar badge (`useNavBadge`) shows the NEEDS YOU count and clears when it is 0.
* AC1.3 When a session enters NEEDS YOU while the page is open, a polite live region announces
  "payments-fix needs you" within one poll; focus does not move.

**US-2 Quiet day is quiet.** 
* AC2.1 Quiet day: sections Needs you (0) and Live (0) say "Nothing needs you." / "Nothing is working."
  Parked shows 3 cards; Stale shows `9 (hidden)` in the toggle.
* AC2.2 The count line reads "20 terminals: 0 processing, 0 waiting for you, 20 idle. Idle is not
  running." (built from `counts.by_status`).
* AC2.3 The word "running" appears nowhere as a status; a test greps the rendered DOM and the bundle
  strings for it.

**US-3 Understand a session.** 
* AC3.1 Each card: conductor as title bar with status and "idle 2 h" from `last_active`; helpers as
  chips (max 5, then "+N"); expanding shows terminals with `status`, `last_active`, `caller_id`.
* AC3.2 A helper whose caller is not live shows a dashed red chip "ORPHAN" and the text "caller no
  longer exists"; a no-`caller_id` non-conductor shows "(peer)". When the first terminal itself is
  such a helper, the session has no conductor: the card says "no conductor" and Ask is off with a
  reason.
* AC3.3 Cards dim with age: warm (under 1 h), parked, stale (dimmed; full colour on hover or focus).
* AC3.4 Hostile strings in session names, labels, profile names and prompt text render as text
  (test: `<img src=x onerror=...>` and `javascript:` values inert).

**US-4 See closed helpers.**
* AC4.1 History (N) opens a drawer "Closed helpers, rebuilt from inbox rows; their terminals no longer
  exist" listing `helper <id4>`, rows, "last message 26 h ago". "inbox rows" shows up to 3 rows (live
  read).
* AC4.2 When the window was full the drawer says older ones may be missing (`complete=false`).
* AC4.3 History never lists a helper that is still alive; external senders (`cao-ops-mcp`) appear
  separately.

**US-5 Ask a conductor.** 
* AC5.1 Ask opens a labelled text box; Send posts to `/api/sessions/{name}/ask`; the card then shows
  the conductor `processing`, `completed`, then idle on later polls.
* AC5.2 If the conductor changed since render, the user sees "This session changed, reload" and nothing
  was sent (`conflict_changed`).

**US-6 Answer a waiting helper.** 
* AC6.1 The card loads the prompt tail (max 20 lines, as text). Quick buttons appear only when the
  backend returned `suggested`; otherwise a text box and "Send answer".
* AC6.2 If the terminal is no longer waiting, the user sees "tester is no longer waiting" and nothing
  is sent (`not_waiting`).
* AC6.3 The answer is sent only through `/terminals/{id}/input`; a contract test asserts that the
  inbox route is never called for an answer.

**US-7 Start a session.** 
* AC7.1 Start opens a dialog with profile (from `/api/profiles`), optional provider, working
  directory (required, text), optional first message and optional name. It never sends until Start.
* AC7.2 Success shows the new card within one poll; failure shows the CAO reason without terminal text.

**US-8 Stop and clean up safely.** 
* AC8.1 Stop... on a card and Clean up stale (N)... open a dialog that names every session and every
  terminal it would end, starts with nothing ticked, and disables the red button until something is ticked.
* AC8.2 If any listed session is working or waiting, the dialog shows a red warning naming it.
* AC8.3 Cleanup lists only sessions that are `stale` at plan time, at most 25, with a truncation notice.
* AC8.4 A session that became active, or whose terminal set changed, between plan and execute is
  skipped and reported; nothing is sent to CAO for it.
* AC8.5 No code path can stop a session that is not listed and ticked; there is no "all"; the request
  schema has no such field (tested in A and in the contract suite).

**US-9 Survive trouble.** 
* AC9.1 CAO down: banner "CAO is not reachable at http://127.0.0.1:9889", last snapshot dimmed with
  its age; actions disabled; no empty-state "no sessions".
* AC9.2 CAO answers 401/403: banner explains auth-enabled CAO is not supported by this version.
* AC9.3 Page hidden or tab not active: no polling.

**US-10 Usable by keyboard and at 320 px.** See section 8.6.

## 6. Architecture

```
 Browser (Kiro Crew dashboard, same origin as the gateway)
 +--------------------------------------------------------------+
 |  CAO Shelf page (React ESM, in-process, own CSS)         |
 |   useAppApi().get/post  -- only /apps/cao-shelf/api/ |
 +------------------------------|-------------------------------+
                                | HTTP, same origin
                                v
 Kiro Crew gateway (127.0.0.1:<port>)  -- reverse proxy /apps/<name>/api/*
   signs each request: X-KiroCrew-Proxy (+ -V2, nonce); drops cookies
                                | HTTP, loopback only
                                v
 Shelf backend (child process, Kiro Crew's Python 3.12, stdlib only)
 +--------------------------------------------------------------+
 |  http.py     ThreadingHTTPServer on 127.0.0.1:$PORT          |
 |  auth.py     HMAC v1 + v2 nonce cache                        |
 |  poller.py   one thread; only while a client is active       |
 |  cao.py      CaoClient: urllib, loopback-only, timeouts      |
 |  tiering.py  pure: tiers, roles, orphans, counts   <-- unit  |
 |  history.py  pure: closed helpers from inbox rows  <-- unit  |
 |  actions.py  ask / answer / start / stop plan+execute        |
 |  ledger.py   data/ledger.json (ids + times only)             |
 |  config.py   data/config.json                                |
 +------------------------------|-------------------------------+
                                | HTTP GET/POST/DELETE, 127.0.0.1:9889
                                v
                     cao-server (REST, auth off)
```

Data flow, one refresh: the page asks `GET /api/shelf?since=<rev>` every 5 s while visible. The backend
answers from memory. Separately the poller (running only while a client asked in the last 30 s) reads
CAO on the cadence in api-contract.md, rebuilds the projection with the pure functions, bumps
`revision` if it changed, and updates the ledger. Writes go through `actions.py`, which always re-reads
CAO before it writes.

Components

| Component | Responsibility | Depends on |
|---|---|---|
| `backend/server.py` | 20-line entry: sets `sys.path`, builds the app, serves | cao_shelf |
| `backend/cao_shelf/*` | as the diagram | stdlib only |
| `ui/src/*` | page, cards, dialogs, own CSS, API wrapper | host SDK (externals) |
| `app.json` | manifest | Kiro Crew |
| `e2e/*` | fake CAO, scratch rig, Playwright tests | pytest, playwright |

Why a thread and `http.server`, not aiohttp: the backend runs in the gateway's interpreter, which has
aiohttp, but app code cannot rely on gateway-internal packages across versions, and the stdlib keeps
`requirements.txt` empty, which avoids the "desktop build cannot install into its interpreter" refusal
[V: `manifest-reference.md`, `backend.hooks` section: that refusal applies to `pyproject.toml`/`setup.py`
installs; a deps-free app needs none]. The spike backend is exactly this shape and ran [V].

## 7. Kiro Crew: what the App is at v0.12.2 (question 1)

Manifest (target; final text lives in `app.json`):

```json
{
  "name": "cao-shelf",
  "version": "0.1.0",
  "displayName": "CAO Shelf",
  "description": "Your CAO sessions at a glance: what needs you, what is working, what is parked.",
  "author": "CAO contributors",
  "license": "Apache-2.0",
  "minKiroCrewVersion": "0.12.2",
  "tags": ["cao", "orchestration", "sessions"],
  "iconPath": "assets/icon.png",
  "platform": {"os": ["macos", "linux"]},
  "backend": {"entryPoint": "backend/server.py", "port": "auto", "healthCheck": "/health"},
  "ui": {
    "entry": "bundle/index.mjs",
    "pages": [{"route": "/apps/cao-shelf", "label": "CAO Shelf", "icon": "Layers"}],
    "loading": {"layout": "page"}
  },
  "permissions": {
    "api": ["/apps/cao-shelf/api", "/apps/cao-shelf/api/*"],
    "network": false
  }
}
```

| Question | Answer | Evidence |
|---|---|---|
| Manifest fields | Required `name, version, displayName, description`; ours adds the recommended ones; `backend`, `ui`, `permissions`, `contributes.*` as above | [V: `docs/app-kit/manifest-reference.md`; spike manifest installed] |
| Backend language and lifecycle | Python script (`type` auto-detected), also `asgi`, `node`, `exec`. Spawned by the gateway with its own interpreter, cwd = app dir, `PORT` assigned (9100-9199), health re-polled for life, dropped from the proxy if unhealthy, stopped on disable or trust revoke | [V: spike; `manifest-reference.md`] |
| Route mounting and auth | Browser path `/apps/<name>/api/<p>` becomes backend `/api/<p>`; every request is HMAC-signed; cookies/`Authorization` stripped | [V: `routes_proxy.py`; spike v1 verification] |
| Front-end | React component in the dashboard's process; `react`, `react-dom`, `lucide-react`, `@kirocrew/app-sdk`, `@kirocrew/app-sdk/ui`, `@tanstack/react-query` are host-provided externals; needs a build (Vite library mode) to a single ESM file | [V: `getting-started.md`; spike build] |
| Styling | Host Tailwind is precompiled: arbitrary utilities may not exist. Ship own CSS in a `<style>`; use host variables `--bg --card --border --text --accent --ok --warn --danger --info --panel` so themes apply | [V: computed styles in Chromium] |
| SDK hooks used | `useAppApi` (get/post/del/raw), `useAppInfo().active`, `useNavBadge`, `useNotify` (optional) | `useAppApi` and `useAppInfo` [V: docs, spike for useAppApi]; `useNavBadge` and `useNotify` signatures `[I]` until Phase 0 |
| Install and registration | `kirocrew app install <dir>`; `app enable`; files in `~/.kiro/crew/apps/<name>/`; `app update --source <dir>` re-copies | [V: ran] |
| Where it lives | `ui.pages` sidebar page now; `contributes.panelTabs` later (Phase 4) | page [V]; panel tab accepted by validator [V], rendered [I] |
| Trust | Third-party executable code is denied by default. Needs `agent.apps_allow_third_party=true` (Settings, Security, Third-party apps, "Trust every third-party app") **or** a per-app `agent.apps_trusted` grant given when enabling from the App Store UI | `true` path [V: ran]; per-app grant [I: docs] |
| Distribution | Official catalog is maintainer-curated via a listing-request issue; a team can host a registry (`registries` in config, `app-registry.json` entry with `gitUrl`, `subdirectory`); clone then `npm install && npm run build` in the subdirectory | [I: `publishing-guide.md` s10 to s12, `install_clone.py`; not rehearsed, needs a pushed branch] |
| Limits | 8 panel tabs, 2 session controls; `permissions.api` entries added by an update are held until approved | [V: docs] |

## 8. Front-end design (question: visual reference is `design/`)

Visual reference: [design/mock-11-cao-shelf.html](design/mock-11-cao-shelf.html) (interactive,
invented data) and the screenshots `design/11-cao-shelf-quiet.png`, `11-cao-shelf-busy.png`,
`w800-11-cao-shelf-quiet.png`. Concept text: [design/concept-11.md](design/concept-11.md).
The mock uses hard-coded hex colours. Amended: the App is built from Kiro Crew's own App Kit
controls (`Card`, `Btn`, `Badge`, `ErrorNotice`, `EmptyState`, `Skeleton`, `Input`, `Toggle`, `Modal`
and `Select` from `@kirocrew/app-sdk/ui`) and reads host tokens only, with no fallback values and no
dark-mode branch of its own: amber is `--warn`, green `--ok`, blue `--info`, red `--danger`, edges
`--border`, card `--card`, canvas `--bg`, text `--text`. Status sentences (the in-flight line, the orphan
notes) are `--text` with a small dot in the status colour, so every Shelf-owned text keeps 4.5:1 in both
themes (ruling 7: the host's dark theme sets `--ok-text` equal to `--ok`). Only the notice's "Why" label
(`--warn-text`) and form validation errors (`--danger-text`) still use a `-text` variant. The primary action is the host accent, never `--ok`.

### 8.1 Layout

```
 PageHeader "CAO Shelf"  subtitle "12 CAO sessions at a glance: ..."     [ Start session ]
 [ Needs you 0 ] [ Live 0 ] [ Parked 3 ] [ Stale, over 48 h 9 (hidden) ]      <- toggles, aria-pressed
 20 terminals: 0 processing, 0 waiting for you, 20 idle. Idle is not running.   <- honest line
 (banner: CAO unreachable / auth / snapshot age)                              <- when needed
 NEEDS YOU (1) ------------------------------------------------------------
 +- payments-fix ---- NEEDS YOU -+   card grid: repeat(auto-fill, minmax(min(100%, 300px), 1fr))
 | o conductor (supervisor)  processing
 | (o coder-1 processing) (o tester waiting about 4 min) (o reviewer idle 6 min)
 | [tester asks: "push to origin?"  [Approve push] [Deny]]
 | [Ask]            [History (3)]|   actions: 2 by 2, 4 across on a card 460 px or wider
 | [Remote control] [Stop...]    |
 +-------------------------------+
 LIVE NOW / PARKED / STALE sections the same
 [ Clean up stale (9)... ]  [ All history (20 closed helpers) ]   Cleanup always asks first.
```

* Card parts (a host `Card` with `role="article"`): title button (expands details, `aria-expanded`)
  with the tier as a host `Badge` beside it (NEEDS YOU, LIVE, WARM, PARKED, STALE with the age), conductor row, helper chips (at most 5 then "+N", chip label is the
  CAO window name, status word and, for idle, the age), need box, in-flight line, details table
  (terminal id, status, last_active, caller_id), actions, ask form, history drawer.
* Status is always a word plus a shape or colour, never colour alone. Idle terminals show a hollow dot.
  `processing` pulses unless `prefers-reduced-motion`.
* Stale cards: dimmed to 66% and desaturated; full on hover or `:focus-within`.
* Sections hidden by a toggle are not rendered. Defaults: Needs you, Live, Parked on; Stale off
  ("Stale, over 48 h 9 (hidden)"). Toggle state in `localStorage` key `cao-shelf:tiers`.

### 8.2 States

| State | Rendering |
|---|---|
| Loading first snapshot | PageHeader plus three host `Skeleton` cards |
| Quiet day | as AC2.x; Needs you and Live sections show their one-line "Nothing ..." text |
| Zero sessions, CAO reachable | host EmptyState "No CAO sessions yet." holding the one Start session button (the header has none) |
| CAO unreachable, have snapshot | host ErrorNotice with age; cards dimmed; every action button disabled with a title and `aria-describedby` pointing at the notice |
| CAO unreachable, no snapshot | host EmptyState "Cannot reach CAO at <url>. Start it with cao-server, or change the address in Settings." (plain text: the host subtitle is a string) Not "no sessions" |
| Auth-enabled CAO | EmptyState explaining v1 limit |
| Incompatible CAO | Banner naming the missing field |
| Error in one action | host ErrorNotice (`role="alert"`) under the control; the page stays |

### 8.3 Actions

* **Ask**: inline form under the card; `Escape` closes; Enter sends; after send the button shows
  "Sent" for 2 s; the card then shows the conductor's real status from later polls (the mock's
  "completed decays to idle" appears because CAO reports `completed` and later `idle`).
* **Answer**: need box shows tail lines in a `<pre>` (text only, scrollable, max 20 lines) behind a
  "Show prompt" disclosure; buttons from `suggested`; free text field otherwise; each send re-checks
  status server-side.
* **Start**: modal; fields as AC7.1; working directory is required because CAO launches in it.
* **Stop / Clean up**: one modal component, two entry points; role `dialog`, `aria-modal`, focus
  trapped, focus returns to the opener on close; sessions listed as checkboxes, terminal names under
  each; "Select all" exists only in the clean-up dialog and only ticks the listed ones; the red button
  text states the count: "Stop 3 sessions (7 terminals)".
* **History**: per-card drawer, plus "All history" panel (newest 8, "and N older").

### 8.4 Keyboard and accessibility

* Every control is a `button`, `input` or link in DOM order that matches reading order; no positive
  tabindex. Visible focus ring: amended to the host's own 2 px `--accent` ring (`:focus-visible` in
  the host stylesheet); the page draws none of its own.
* Tier toggles are a `role="group"` of toggle buttons with `aria-pressed`.
* Cards are `article` with `aria-label="session payments-fix, needs you"`.
* One `aria-live="polite"` region announces changes of tier into NEEDS YOU and action results.
* Dialogs are the host `Modal`: `Escape` cancels, `Tab` cycles inside, focus returns to the opener.
  Amended (Gandalf rulings 1 and 2 in the option A design): initial focus is on the host's X
  ("Close"), never on a primary or danger button; pressing it sends nothing. A backdrop click now
  closes a dialog too, which is always the safe direction (closing Stop stops nothing, closing Remote
  control declines). A Start form that holds input ignores `Escape` and the backdrop; Cancel and the X
  still close it. While a request is in flight every dismissal (X, `Escape`, backdrop) is refused, so
  its result shows in the dialog. Footer end-state buttons say "Done", so "Close" names only the X.
* Contrast: status colours are paired with text; check both host light and dark themes (the e2e
  suite screenshots both).
* Targets at least 44 px on touch widths.

### 8.5 Narrow width

Below 640 px the grid is one column, chips wrap, the toggle row scrolls horizontally without
clipping focus. Amended: dialogs keep the host `Modal` width, up to 520 px inside the host's 32 px
padding on each side (296 px wide at 360, 256 px at 320); their footer buttons wrap and share the row. Verify at 320 and 390 px (Kiro Crew's own rule in
`website/docs/page-layout.md`). In a panel tab the same rules apply at the panel's width.

### 8.6 Text and escaping

All CAO-derived strings (session names, window names, profile names, provider, prompt tail, message
bodies) are inserted as React text nodes only. No `dangerouslySetInnerHTML`, no markdown rendering,
no `href` built from CAO data. An ESLint rule (`react/no-danger`) and a test enforce this.

## 9. Security and privacy

### 9.1 Rules and how each is met

| Rule | How |
|---|---|
| Loopback-only binds; no listener beyond Kiro Crew's | The backend binds `127.0.0.1:$PORT` (the port the gateway assigns); it is the app backend process Kiro Crew's manifest provides, not an extra listener. It refuses to start on any other address. [V: scaffold binds `127.0.0.1`; spike] |
| Only the gateway can call the backend | HMAC v1 **and** v2 (nonce cache sized for the poll rate) required on every route except `/health`; a request with v1 alone is refused, because accepting it would let a captured request be replayed by dropping the v2 headers; a missing `KIROCREW_PROXY_SECRET` means the backend refuses all but `/health` [V: the 0.12.2 gateway sets both header sets on every proxied request, and the e2e journeys pass through it] |
| No secrets in the App | None shipped, none stored. CAO auth tokens are unsupported in v1. `.app_secret` stays Kiro Crew's |
| Destructive actions explicit, listed, confirmed | Section 8.3 and the stop contract in api-contract.md; closed request schema; per-session re-verification |
| No shell access | No `subprocess`, `os.system`, `eval`, `exec`, no `shell=True`; CI grep gate in workstream A |
| Input from CAO is untrusted | Text-only rendering (8.6); the backend validates every CAO id and name against regexes before using them in a URL, caps response size at 4 MiB and list sizes, strips control chars and ANSI from tails, and never follows redirects |
| No third-party network access | `cao_url` must be `http://` with host `127.0.0.1`, `localhost` or `::1`; `localhost` is resolved and each address checked as loopback; redirects refused; the only other network use is none. Manifest `permissions.network=false` |
| Trust grant and why acceptable | Needs third-party execution. Admitting an app equals running its code with the gateway's privileges [V: trust model doc]. Acceptable because the code is small, stdlib-only, open (Apache-2.0, in the CAO repo), reviewed in CI, and speaks only to loopback CAO. Users who do not want a blanket grant use the per-app grant. The App's README states this plainly |

### 9.2 What the sandbox does and does not do

On this host the standard sandbox did not stop file writes outside the app or loopback calls [V:
SPIKE 1]. The App therefore does **not** rely on it: it writes only under `data/` (config, ledger),
and a test asserts no other path is opened for writing.

### 9.3 Privacy

Session names, window names and terminal output are the user's data.
* No telemetry, no external calls, no analytics. The App never reads or writes Kiro Crew's heartbeat
  settings.
* Logs (to the gateway's app log) contain: timestamps, endpoint, status, durations, counts, session
  names for stop audit lines only, and error codes. They never contain message bodies, prompt tails,
  answers, working directories or first messages. A test greps log output after the e2e run for the
  sentinel strings used in the tests.
* Prompt tail: fetched on demand, held in memory for 10 s, not logged, not persisted.
* Inbox rows: counted and timestamped; bodies are passed through to the page on request (live) and not
  stored. The ledger holds ids and times, not text. `persist_history=false` disables it.
* `data/` is mode 0700 (set by Kiro Crew) [V]; the backend creates files 0600.

### 9.4 Threat notes

* A malicious CAO terminal can emit hostile text: handled by 8.6 and sanitising.
* A malicious page cannot reach the backend without the gateway's signature; a page in the dashboard
  can call the proxy path, but `permissions.api` limits it to this app's own namespace [V: SDK check;
  token scoping `[I]` from the trust model doc].
* CSRF to CAO: the backend sends no `Origin` header, so CAO's `OriginCheckMiddleware` does not apply
  to it; the backend is the only writer, and every write passes re-verification.
* Replay of a signed request (stop, ask, answer, start, settings): the v2 nonce cache refuses a
  second use within the 60 s window, and the v1-only form is refused so the nonce cannot be skipped
  by dropping the v2 headers. A stop plan is additionally single-use per session. An attacker who can
  read loopback traffic or the backend's environment can already forge requests; the nonce protects
  against a captured request, not against that.

### 9.5 Path-prefix hosts: out of scope

Kiro Crew's dashboard serves from the origin root; a prefix such as `/proxy/kirocrew/` needs a
dashboard rebuilt with `KIROCREW_BASE_PATH`, a build-time option [V: `remote-and-mobile.md`]. App URLs
are `/apps/<name>/...` under that same root, and the App's own calls go through the SDK's rebased
`fetch`, so an App built for the root does not itself know the prefix. Supporting prefixed hosts is a
property of the host build, not of the App, and CAO's own UI has a separate build-time base. The shelf
therefore documents "root-served Kiro Crew only" and does not test prefixes. Reason: it cannot be
fixed from inside an App, and the workshop host case also needs CAO's `/proxy/cao/` base [I].

## 10. CAO call map (question 3)

`R` = read, `W` = write. "Cached" = not repeated per poll. All calls to `http://127.0.0.1:9889` (setting).

| Feature | CAO call | Fields used | Live or polled; reliability |
|---|---|---|---|
| Session list | R `GET /sessions` | `name`, `id` | polled; list of `cao-` sessions; `status` ignored (V) |
| Tier, status counts, ages | R `GET /sessions/{cao-name}` | per terminal: `id, tmux_window, agent_profile, provider, status, last_active, pending_inbox_count` | polled; `status` is **inferred from the screen**, so it is a hint; `last_active` is the better clock (V) |
| Hierarchy, orphans | R `GET /terminals/{id}` (once per new id, the first terminal of a session too) | `caller_id` | immutable once set, so cached; null for conductor and peers; absent from list rows (V) |
| Conductor | derived | the first terminal, unless it has a caller; then the first terminal without a caller. A first terminal whose caller no longer exists is an orphaned helper and the session has **no conductor** (`conductor_missing`): Ask is off and the card says so | CAO docstring says index 0 is best effort |
| History | R `GET /terminals/{conductor}/inbox/messages?limit=100&order=desc` | `sender_id, created_at, status`, body only on demand | polled slowly; newest 100 only; ids not names (V) |
| Inbox rows of one helper | same with `&sender_id=` | `message, created_at, status` | on demand |
| Waiting prompt tail | R `GET /terminals/{id}`, R `GET /terminals/{id}/output?mode=full` | `status`, `provider`, `output` | on demand; provider-specific text; UI treats as text |
| Ask conductor | W `POST /terminals/{c}/inbox/messages?sender_id=cao-shelf&message=`, then R `GET /terminals/{c}/inbox/messages?limit=20&order=desc` | response `message_id`; the row's `status` | delivers when idle or completed; `pending` otherwise (V). The page says which (`delivery`), never just "Sent" |
| Answer helper | W `POST /terminals/{id}/input?message=` after a status read | `success` | typed input; provider decides meaning; verify afterward by the next poll (V on mock) |
| Start | R `GET /agents/profiles`, W `POST /sessions?agent_profile&provider&session_name&working_directory` + JSON `{initial_message}` | Terminal `session_name`, `id` | synchronous up to provider init (30 s timeout); the page picks one idempotency key per opened Start dialog and sends it again on every retry; the backend passes it to CAO and answers a repeat of the same request with the first launch result, so a retry cannot double-launch. The backend never invents a key |
| Stop | R `GET /sessions/{n}` then W `DELETE /sessions/cao-{n}` | `{"success","deleted","errors"}` | admin scope when auth is on; a 409 means cleanup deferred |
| Version | R `GET /health` | `version`, `terminal_backend` | once per 60 s |

The MCP-tool names in the concept map as: `list_sessions` = `GET /sessions`; `get_session_info` =
`GET /sessions/{n}`; `send_session_message` = inbox `POST`; `launch_session` = `POST /sessions`;
`shutdown_session` = `GET` then `DELETE` [V: `ops_mcp_server/server.py`]. The App calls REST directly
because a Kiro Crew App backend is not an MCP client and the ops server only wraps these routes.

Provider differences (answering): `claude_code`, `codex` and others that block orchestrated input make
the inbox route unsuitable; `/input` is correct for all. What the text must be ("y", "1", "o") is
provider-specific. v1 sends exactly what the user chose or typed; suggested buttons exist only for
shapes the backend recognises (section 11, `prompts.py`). If AG-UI is enabled, `POST /agui/v1/run`
`resume[]` does per-provider translation; using it is Phase 3 stretch, detected by `GET
/agui/v1/capabilities` [I].

### Request budget (see section 15 for the table)

Per refresh: 1 `GET /sessions` + one `GET /sessions/{n}` per due session; cold start adds one
`GET /terminals/{id}` per terminal (read once, then cached; at most 100 per tick) and 0 to 4 inbox
reads per tick. "All history" reads at most 4 inboxes and stops after 8 s of CAO time per call.

## 11. Backend design notes

* Files: see section 14. `tiering.py` and `history.py` are pure (no I/O, no clock; `now` is a
  parameter). They are the unit under test and port cleanly.
* The clock comes from `time.time` injected in `poller`; tests pass a fixed `now`
  (`fixtures/cao/*/clock.json`).
* Thread model: one poller thread, a `ThreadingHTTPServer` for requests, a single lock around the
  snapshot swap. Request handlers never call CAO except for writes and on-demand reads, each with a
  10 s read timeout (30 s for start) so a slow CAO cannot starve the server.
* Python 3.11 compatible (CAO's CI runs 3.11 and 3.12; Kiro Crew brings 3.12).
* `prompts.py`: a table of recognised prompt shapes. v1 ships the `mock_cli` shape and the structure;
  real-provider shapes are added only from recordings (Phase 3, task A9). Unknown text yields no
  suggestions, never a guess.
* Config at `data/config.json` (0600). `GET/PUT /api/config` edits it; `PUT` validates `cao_url`
  (loopback http only) and ranges. There is no env override at runtime inside Kiro Crew (env is fixed
  by the gateway [V]); the tests use `CAO_SHELF_CAO_URL`, honoured only when `KIROCREW_SPAWNED` is unset.
* Failure behaviour: any exception in a poll tick is caught, counted, and turns into `cao.error`;
  the thread never dies. Unknown JSON shapes become `cao_incompatible`, not a crash.
* Reload safety: `GET /health` is cheap and dependency-free (the gateway drops the backend if it
  stops answering) [V: manifest-reference].

## 12. Packaging and install in Kiro Crew (question 1, "one command")

1. **From a clone (works at Phase 1, [V] for the local-dir path):**
   ```
   git clone https://github.com/awslabs/cli-agent-orchestrator && cd cli-agent-orchestrator
   examples/kiro-crew-cao-shelf/install.sh
   ```
   `install.sh` (workstream C) checks `kirocrew` is on PATH, builds the bundle if `ui/bundle/index.mjs`
   is missing or older than `ui/src`, runs `kirocrew app install <dir>`, prints the trust step
   ("Settings, Security, Third-party apps, or `kirocrew config set agent.apps_allow_third_party true`
   which trusts every third-party app") and runs `kirocrew app enable`. It never sets the blanket
   grant unless `--trust-all` is passed.
2. **Team registry (Phase 4):** a one-line `registries` entry plus an `app-registry.json` row with
   `subdirectory: "examples/kiro-crew-cao-shelf"`. The registry install builds in that
   subdirectory, so the folder keeps a root `package.json` whose `build` script builds the UI [I].
3. **Official catalog:** open an App Store listing request issue (maintainer-curated) once the app has
   a stable tag [V: `publishing-guide.md` s10]. This is the true "one click". It needs a decision on
   whether the listing may point into a monorepo subdirectory (decision 3).

Built bundle: output goes to `ui/bundle/index.mjs`, **not** `dist/`, because the CAO `.gitignore` has
`dist/` and `build/` [V: `git check-ignore`]. The bundle is committed (small, unminified, reviewable)
and CI rebuilds it and fails on drift (workstream C), so a clone installs without Node. Alternative
(no committed bundle) is decision 2.

Install must not need network after the clone: the backend has no dependencies; only `install.sh`'s
build step needs Node 22.12+ and npm, and it is skipped when a fresh bundle is present.

Uninstall: `kirocrew app uninstall cao-shelf` keeps `data/` by default [V: publishing guide s9];
document how to delete the ledger.

## 13. Configuration

| Setting | Default | Where | Notes |
|---|---|---|---|
| `cao_url` | `http://127.0.0.1:9889` | `data/config.json`, Settings panel in the page | loopback http only |
| `poll_seconds` | 5 (3 to 60) | same | tick of the poller and of the page |
| `warm_hours` | 1 (0.25 to 24) | same | below this, idle is "warm" |
| `stale_hours` | 48 (2 to 720) | same | at or above, idle is STALE |
| `persist_history` | true | same | ledger on or off |
| Tier toggles | Needs, Live, Parked on; Stale off | browser `localStorage` | the only thing the shelf remembers in the browser |
| Trust | off | Kiro Crew `agent.apps_allow_third_party` or per-app grant | not ours to set |
| CAO side | none required | | MCP Apps, AG-UI, events: none needed |

CAO's own `CAO_HOME_DIR` is irrelevant to the App (it talks REST); it matters only to the e2e rig,
which sets a scratch one. If CAO runs with `CAO_ALLOWED_HOSTS` that excludes `127.0.0.1`, calls fail
with 400 from `TrustedHostMiddleware`; the default allows it [V: `constants.py:608`].

## 14. Folder layout and repository fit (question 8)

```
examples/kiro-crew-cao-shelf/
  README.md                  (D) what, install, trust, troubleshooting
  PROPOSAL.md SPIKE.md api-contract.md   (this proposal; verified-behaviour appendix; contract)
  app.json                   (C)
  package.json               (C) root build script for registry installs
  install.sh                 (C)
  assets/icon.png            (C) 512x512 opaque
  backend/server.py          (A)
  backend/cao_shelf/{__init__,http,auth,cao,tiering,history,actions,poller,ledger,config,prompts,text,validation,errors}.py   (A)
  ui/package.json ui/vite.config.ts ui/tsconfig.json ui/vitest.config.ts   (B)
  ui/src/{App.tsx,api.ts,types.ts,sdk.d.ts,styles.ts,components/*.tsx,hooks/*.ts}   (B)
  ui/test/*.test.tsx ui/test/sdk-stub/*   (B)
  ui/bundle/index.mjs        (B builds, C verifies drift)
  schemas/*.json api-contract.md   (shared; changed only with both A and B)
  fixtures/{cao,expected,_gen}/   (A owns generator; shared data)
  tests/test_cao_shelf_*.py    (A) unit and contract tests
  e2e/{rig,tests}/                 (C) fake CAO, scratch rig, Playwright
  design/                          (reference; do not edit)
```

How CAO CI treats examples [V: `.github/workflows/ci.yml`, `pyproject.toml`]:

* `ruff check src/ test/ examples/` runs on **all** examples (rules F401, F821, F822, F841). Our Python
  must pass. No change needed.
* `black --check` and `isort --check-only` run on an **explicit list** of example dirs. Add ours (a
  one-line edit to `ci.yml`, outside this folder, for the owner to approve).
* `pytest` runs `test/ examples/workflow/tests/ examples/skills/tests/` explicitly with
  `-m "not e2e"`. Add `examples/kiro-crew-cao-shelf/tests/` (same edit). Python 3.11 and 3.12 legs.
  Test file names are unique-prefixed `test_cao_shelf_*` to avoid basename clashes (repo has no
  `__init__.py` in example test dirs).
* mypy covers `src/` only; we still run `mypy --strict backend` locally and in our own script.
* Coverage gate (`--cov-fail-under=80`) measures `src/cli_agent_orchestrator` only, so it is not affected.
* `scripts/validate_markdown_links.py` checks every maintained `.md`: relative links in our docs must
  resolve and heading fragments must match. Workstream D runs it.
* A Node job for `ui/` (typecheck, vitest, build, drift) is added to `ci.yml` modelled on the "CAO
  MCP Apps" job. The Playwright e2e is `-m e2e`, off in default CI, with an optional manual workflow.
* License headers: examples carry none; `app.json` has `"license": "Apache-2.0"`. Follow the repo.
* Docs: README in the example (conventions: title, one-paragraph purpose, setup, usage, expected
  failures, as in `examples/ops-mcp/README.md`), plus one page, now `docs/kiro-crew.md`, linked
  from the root `README.md`, `docs/control-planes.md`, `docs/mcp-apps.md`, `docs/web-ui.md` and
  `docs/kiro-cli.md`. The docusaurus sidebar is not edited: it lists only the site's own
  `docusaurus/docs/` pages. The `ci.yml` change is a separate follow-up; until it lands, run
  `make check` in this folder.

## 15. Performance budget (question 3)

Cadence: tick 5 s; a session is read each tick when hot, every 15 s when parked, every 60 s when
stale (api-contract.md). Requests per second to CAO in steady state:

| Fleet | Shelf (this design) | Shelf if every session were read every tick | CAO web Home today |
|---|---|---|---|
| Quiet day, 12 sessions (0 hot, 3 parked, 9 stale) | (1 + 3/3 + 9/12) / 5 s = **0.55 /s** | 13 / 5 = 2.6 /s | 33 / 10 s = 3.3 /s (live read) |
| Busy day, 12 sessions (2 hot, 1 parked, 9 stale) | (1 + 2 + 1/3 + 9/12) / 5 = **0.82 /s** | 2.6 /s | 3.3 /s |
| 50 sessions (2 hot, 8 parked, 40 stale) | (1 + 2 + 8/3 + 40/12) / 5 = **1.8 /s** | 51 / 5 = 10.2 /s | about 1 + 50 + terminals per 10 s |

Cold start: 1 + N session reads + one `caller_id` read per terminal (20 for the 12-session
fixture) + inbox reads at 4 per tick. About 21 requests plus 12 inbox reads over 3 ticks for 12
sessions. Steady state adds 0 caller reads.

Browser to backend: one `GET /api/shelf?since=` per 5 s per visible tab, about 13 KB for 12 sessions
and about 55 KB for 50 when changed, a 40-byte stub when not. Hidden tab: none. The poller itself
stops 30 s after the last request.

Backend memory: well under 5 MB (snapshot, caller cache, ledger). Targets checked in tests: tick
time against the fake CAO under 100 ms for 50 sessions; no more than the table's request counts
(a counting fake asserts it).

Staleness the user can see: a parked session may be up to 15 s old and a stale one 60 s; each card
carries the poll class and the page shows "checked <n> s ago" in the details. A session that leaves
idle is noticed within its class interval; NEEDS YOU is therefore up to 15 s late for a parked session
that suddenly asks (stated, accepted; the `poll_seconds` and class intervals are settings).

## 16. Testing strategy (question 7)

| Layer | Tool | What | Where |
|---|---|---|---|
| Unit, pure logic | pytest | tiering boundaries (47.99 h vs 48.00 h, warm at 1 h), role and orphan rules, "unread session must not create orphans", counts, ordering, history rebuild, 100-row window flag, external senders, ledger merge. Inputs are `fixtures/cao/*`, expected `fixtures/expected/*` | `tests/test_cao_shelf_tiering.py`, `..._history.py` |
| Contract | pytest + fake CAO (`e2e/rig/fake_cao.py`, stdlib server replaying fixtures) + the real backend on a loopback port with a test secret | every endpoint of api-contract.md against `schemas/` (`jsonschema`), every error code, CAO down/timeout/401/odd shape, request counting against the budget, signature rejection, the stop safety rules (not in plan, changed, working, `all` rejected), answer never uses the inbox route, no body in logs | `tests/test_cao_shelf_contract.py` |
| Front-end component | vitest + @testing-library/react + jsdom, SDK replaced by `ui/test/sdk-stub` | US-1 to US-10 ACs against the two fixture shelves: sections and counts, honest line, chips cap "+N", orphan chip, escaping of hostile strings, dialog rules (unticked, disabled red button, focus trap, Escape), live region, narrow class names, reduced motion | `ui/test/*.test.tsx` |
| Type and lint | tsc, eslint (`react/no-danger`), ruff, black, isort, mypy | | |
| Bundle check | node script | bundle has no `dangerouslySetInnerHTML`, no `fetch(` to an absolute URL, no "running" status string, size under 120 KB | workstream B/C |
| End to end | Playwright (python), real `cao-server`, isolated Kiro Crew | below | `e2e/tests/` |

**E2E rig (all of it proven in the spike)**
1. Scratch CAO: `CAO_HOME_DIR` in a temp dir under the project's scratch root (never `/tmp`), `CAO_API_PORT=9990`
   (picked free at runtime), `--terminal tmux` with its own `TMUX_TMPDIR`, `mock_cli` on `PATH`,
   `CAO_MOCK_CLI_SCRIPTED_PROMPTS=1`. Seed with REST: `POST /sessions?agent_profile=developer&provider=mock_cli&session_name=payments-fix`,
   helpers with `POST /sessions/{n}/terminals?...&caller_id=<conductor>`, an approval with input
   `__mock_approval__ push to origin?` [V: all ran]. A real orphan: create a helper, then `DELETE
   /terminals/<caller>`; a real closed helper: helper posts an inbox row (`sender_id=<helper id>`) then
   `DELETE /terminals/<helper>` [V].
2. Isolated Kiro Crew 0.12.2 in `HOME=<scratch>`, `KIROCREW_PORT` pinned, `--no-open --no-tunnel`,
   config seeded (`apps_allow_third_party`, onboarding and privacy flags, beacon off), app installed
   with `kirocrew app install` and enabled, backend `cao_url` set through `PUT /api/config` to the
   scratch CAO. Token from `kirocrew token`.
3. Playwright (chromium from `PLAYWRIGHT_BROWSERS_PATH`): journeys J1 busy shelf with a real waiting
   `mock_cli` (needs-you, answer `y`, status leaves waiting), J2 ask conductor and see processing then
   completed then idle, J3 history of a closed helper (real), J4 orphan chip, J5 start a session
   through the dialog, J6 stop one session, J7 cleanup dialog over back-dated sessions (see note),
   J8 CAO killed mid-run shows the banner, J9 narrow viewport
   screenshots, J10 dark and light theme screenshots, J11 `window.fetch` spy proves the page makes no
   request except to `/apps/cao-shelf/api/`.
   *Note on J7:* waiting 48 h is impossible and `stale_hours` has a minimum of 2, so the rig rewrites
   `terminals.last_active` in the scratch CAO's SQLite file (under its own `CAO_HOME_DIR`, with the
   server stopped or the row updated in one statement) to back-date chosen rows. This is a test-only
   action in the rig, never in the App. [V: table `terminals`, column `last_active` in `clients/database.py:196,269`;
   the exact stored text format must be confirmed in task C6.]
4. Teardown stops only processes whose pid files the rig wrote and deletes only its scratch dirs.
5. Cheap providers: `mock_cli` gives conductor, helper, `waiting_user_answer`, `completed` and `idle`.
   `processing` is visible only when the test sends `__mock_sleep_5`. Not covered by mock: provider
   prompt shapes, `error` from a real provider (`__mock_error__` gives `error`). Real provider recordings are a separate manual step (Phase 3).

Fixtures: generated by `fixtures/_gen/make_fixtures.py`; shapes recorded from the scratch CAO and the
live 2.5.4 key sets; all values invented (`payments-fix`, `coder-1`, `tester`, ids derived from a
hash). `quiet` and `busy` match the shared fleet: 12 sessions, 20 terminals, 20 closed helpers; busy has
4 processing, 1 waiting, 15 idle. Quiet: 0 needs, 0 live, 3 parked, 9 stale.

## 17. Rollout in phases

**Phase 0, spike checks (about 2 days; some done, see SPIKE.md).** Remaining checks:
P0.1 push a branch and install through a registry entry (clone + build in `subdirectory`);
P0.2 verify v2 nonce HMAC with a stdlib verifier against the 0.12.2 gateway (done: required since the review, the e2e journeys pass through the real gateway);
P0.3 `useNavBadge` and `useNotify` signatures; whether a panel tab renders and `useAppInfo().active`;
P0.4 per-app trust grant path (no blanket flag);
P0.5 whether MCP Apps in 0.12.2 can persist state (re-test G5);
P0.6 ask CAO maintainers whether `caller_id` and inbox counts would be added to the MCP Apps snapshot;
P0.7 a real `claude_code` approval prompt recording (what text, what answer works) on a machine with a login;
P0.8 `permissions.api` approval flow on update.
Exit: decisions 1 to 3 answered; tasks A, B, C unblocked.

**Phase 1, read-only shelf (about 1.5 weeks).** `GET /api/shelf`, tiers, cards, chips, honest line,
quiet/busy states, CAO-down states, toggles, badge, config, install script, contract and unit tests,
component tests, one e2e (J1 minus answer, J8). No writes to CAO exist in the codebase at this phase
(a test asserts the CAO client has no write methods yet).

**Phase 2, history and orphans (about 1 week).** `caller_id` reads and cache, orphan/peer chips, in-flight
line, history endpoints, drawer, All history, ledger. e2e J3, J4.

**Phase 3, steering (about 1.5 weeks).** Ask, answer (with prompt endpoint), start, stop and cleanup.
Writes arrive in this order: ask, answer, start, stop, cleanup. Each ships with its safety tests first.
e2e J1 (answer), J2, J5, J6, J7. Real provider prompt recordings feed `prompts.py`.

**Phase 4 (optional).** Panel tab, registry/catalog listing, AG-UI approve route, `/crew shelf` text
command, port tiering to CAO's MCP Apps snapshot if the maintainers agree, events-based names.

## 18. Decisions the owner must make

1. **Go or fold in?** Build the custom App now, or first propose the snapshot changes to
   `ui://cao/dashboard` (section 4.1). Recommended: build, and open the upstream question in parallel.
2. **Commit the built bundle?** Recommended yes (`ui/bundle/index.mjs`, drift-checked). Alternative:
   no bundle in git, build on install.
3. **Distribution target.** Clone and `install.sh` only, a team registry, or an official catalog
   listing (needs the maintainers to accept a monorepo subdirectory source).
4. **Trust wording.** Ship docs that recommend the per-app grant and mention the blanket flag as a
   shortcut, or the reverse.
5. **Ledger default.** On (survives the 100-row window and the 14-day sweep, stores ids and times) or
   off (stores nothing). Recommended on.
6. **Auth-enabled CAO.** Unsupported in v1. Reading a token from the user would mean storing a secret.
   Confirm that is acceptable.
7. **Edits outside the folder.** `ci.yml` (black/isort/pytest/Node job), `docs/` page and sidebar,
   `docs/control-planes.md` link.
8. **Panel tab in v1?** Recommended no.

## 19. Risks and open questions

| Risk | Effect | Mitigation | Could it stop the project? |
|---|---|---|---|
| Third-party trust is a blocker for some users | Cannot enable without a blanket or per-app grant | Document; per-app grant; clear README | Only for locked-down fleets |
| Kiro Crew changes App Kit between versions | Breaks the page or the proxy contract | `minKiroCrewVersion`, only documented SDK parts, e2e on pinned version, one CI job per Kiro Crew minor | No |
| Registry/catalog needs a standalone repo | "One command" via registry blocked | `install.sh` from clone works regardless | Only for the one-click goal |
| `status` is screen-inferred and can disagree with reality (#645) | Wrong NEEDS YOU or LIVE | Say so in the details table; show `last_active`; never claim "running"; answer endpoint re-checks | No |
| A provider's approval text is unknown | No quick buttons | Typed answer always available | No |
| Inbox window is 100 rows and rows are swept at 14 days | History incomplete | `complete` flag; optional ledger | No |
| Main vs 2.5.4 API differences | `/sessions/{n}/terminals` lacks status on 2.5.4 | Use `GET /sessions/{n}` | No |
| Sandbox is not a boundary on this host | Backend could write anywhere | Own rules plus a test | No |
| Accidental stop of a real session | Data loss | Section 8.3 and api-contract stop rules, a closed schema, re-verification, tests with a counting fake CAO | The design is meant to remove this |
| Concept promises names for closed helpers | Disappointment | Corrected in 4.3 | No |

What stays unverified, and the first check that settles it, are the Phase 0 items P0.1 to P0.8 and the
"[I]" tags above.

## 20. Appendix: repository facts used

* Kiro Crew docs read at v0.12.2: `docs/app-kit/{README,getting-started,manifest-reference,api-reference,publishing-guide}.md`,
  `docs/architecture/app-platform-trust-model.md`, `website/docs/{page-layout,theming-contract,frontend-conventions}.md`,
  `docs/guides/remote-and-mobile.md`, source `src/kiro_crew/apps/routes_proxy.py`,
  `apps/registry_pipeline/install_clone.py`, built-ins `ops_mission_control`, `dev_fleet` (as worked
  examples; note built-ins use in-gateway hooks or module entry points, a third-party App uses a file
  entry point as here).
* CAO read at `31393711`: `api/main.py` routes and middleware, `services/session_service.py`,
  `services/terminal_service.py`, `clients/database.py`, `models/terminal.py`, `ops_mcp_server/server.py`,
  `mcp_server/server.py` (`answer_user_prompt`), `constants.py`, `security/auth.py`, `docs/{api,agui,mcp-apps,mock-cli-provider}.md`,
  `cao_mcp_apps/src/dashboard/Dashboard.tsx`, `.github/workflows/ci.yml`, `pyproject.toml`.
* Issues: awslabs/cli-agent-orchestrator #580, #581, #582, #645.

## 21. Remote control (version 0.2.0)

Version 0.2.0 adds remote control: from a card or the Start dialog, CAO Shelf creates a Kiro Crew
chat on its own `cao-remote-control` agent, which forwards each message you type there to the
session's conductor through CAO's `cao-ops-mcp` (four tools only) and shows the reply. The
behaviour and its limits are in the [README](README.md#remote-control), and the relay texts are in
the [remote control contract](api-contract.md#remote-control).

What changes against the sections above:

* Section 9: the backend now reads one injected secret, the App secret, to get an app token for
  its own relay chats; it stores and logs neither. Relayed text passes through the chat's model
  provider and the Kiro Crew transcript; `data/relays.json` keeps no text.
* Section 10: the backend also calls its own Kiro Crew gateway on loopback (`/api/chat`,
  `/api/chat/*`), and the agent's `cao-ops-mcp-server` reads and writes CAO for relayed messages.
* Section 12: the manifest gains `agents` and `permissions.sessionApproval`, so installing 0.2.0
  asks for consent again in the dashboard. It declares no `mcpServers`: the ops server is declared
  only in the relay agent's own file, so it never joins the default Kiro Crew agent, and
  `install.sh` writes its resolved command and CAO address there.
* The target terminal and reply freshness are held by the agent's instructions, not by code (a
  deliberate choice). A pinned, send-and-wait mode of `cao-ops-mcp` is the upstream route to
  hold them in code later.
