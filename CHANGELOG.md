# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MiniMax Code (`mcode`) provider with per-terminal authentication and profile
  isolation, model and MCP configuration, multi-turn TUI orchestration,
  supervisor/worker E2E coverage, and provider documentation (#624)
- Oh My Pi (`omp`) provider with additive native configuration, profile MCP extension wiring, lifecycle detection, and supervisor/worker orchestration support (#559)
- Add the official xAI Grok Build CLI as the `grok_cli` provider, including
  isolated per-terminal MCP configuration, native hard tool restrictions,
  multi-turn TUI support, orchestration e2e coverage, and provider docs.

### Fixed

- tmux listing parse failures are retried once and reported as a distinct condition instead of surfacing as a bare `ValueError` that reads like "session not found" one layer up. libtmux 0.53.1+ zips `parse_output`'s fields with `strict=True`, so any short row (a pane or session vanishing mid-listing, or trailing fields tmux omits) raised `ValueError: zip() argument 2 is shorter than argument 1` — which propagated through `server.sessions`/`window.panes`, blocked launches outright, and left the pipe-liveness watchdog unable to tell a genuinely-gone session from a transient parse failure. Adds `TmuxLookupError` and routes the listing reads in `clients/tmux.py` through a single retry-and-classify wrapper; a failed `create_session` no longer leaves an orphaned tmux session that blocks relaunching the same name. Also caps `libtmux<0.53.1`, the last release that zips non-strict (caom-anv)
- Codex handoff extraction now skips native TUI activity cells without relying on an English verb allowlist, including when the model's reply starts with prose (#545)
- `list_sessions` ownership metadata now persists the effective canonical launch directory, stays stable after pane `cd`, and purges stale terminal rows before same-name session relaunches so reused sessions report the new directory/profile (#497)
- make session teardown atomic so tmux and the terminal registry can no longer diverge (#498). `delete_session` previously trusted a pre-loop liveness reading and an unverified `kill_session` result, so a session could survive while its registry rows were deleted (orphaned tmux session) or vice versa (ghost rows that later misattributed a reused session name). Now: session creation and session teardown are mutually exclusive per session *name* (a concurrent launch can no longer interleave with a teardown of the same name), `kill_session` returns True only once the session is confirmed gone, and registry rows are deleted only *after* that confirmation. **Error-contract change:** a teardown whose tmux kill cannot be confirmed now raises (surfaced as HTTP 500 on `DELETE /sessions/{name}`) instead of reporting success — the registry rows are left intact and the operation is safe to re-run, which reconciles the survivor. Backend authors: `TerminalBackend.kill_session` must not return True for a merely-dispatched kill

## [2.4.1] - 2026-08-04

### Fixed

- force fast-uri 3.1.5 in aidlc-portfolio examples (#551) (#552)


### Other

- bump cryptography from 48.0.1 to 50.0.0 (#548)

- bump fast-uri from 3.1.4 to 3.1.5 in /docusaurus (#549)

- bump postcss from 8.5.21 to 8.5.25 in /docusaurus (#550)

## [2.4.0] - 2026-08-04

### Added

- add Phase 0 Kiro engine selection (`v2` default; `kas` capability-probed and rejected before terminal allocation)
- Read-only profile HTTP routes for capability search, template discovery and schema retrieval, template-config validation, and rendered previews (#523)
- `CAO_HOME_DIR` environment variable to relocate CAO's entire data directory outside `~/.aws` (#467)
- **Self-learning loop** (opt-in, off by default; see `docs/self-learning.md`):
  - Phase 1 — outcome capture: `memory.learning_enabled` setting (`CAO_MEMORY_LEARNING_ENABLED`), `workflow_outcomes` table, `report_outcome`/`list_outcomes`/`store_lesson` MCP tools (the latter targets a named worker profile's agent scope so retrospective lessons reach the worker), scope-gated `POST/GET /outcomes` endpoints, and a built-in `retrospector` agent profile that distills session outcomes into worker-scoped memory lessons
  - Phase 2 — instruction promotion: `memory.instruction_promotion_enabled` setting (`CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED`; promotion ⊂ learning ⊂ memory), itemized-delta editing of a delimited `## Learned Patterns` block in profile files, `cao memory promote <agent>` CLI (dry-run by default, `--apply` to mutate), recall-count promotion gate, content-free audit logging
  - `cao-learning` shipped skill teaching supervisors/workers the outcome-reporting and lesson-storage habits
  - Validated by a 20-package controlled A/B experiment: +11 mean points on work items with headroom (6/6 wins, sign test p = 0.016), neutral at the ceiling — `docs/self-learning-validation.md`
- `cao profile find <query>` CLI verb and `find_profiles` MCP tool for keyword/BM25 profile discovery over metadata (name, description, tags, capabilities); metadata-only, never exposes prompt bodies (#340)
- Optional `capabilities` and `tags` arrays in the agent profile frontmatter schema (#340)
- **Durable workflow run journal** — a workflow run's execution history now survives a server restart and is inspectable after the fact (#504):
  - append-only `workflow_run_event` table with a per-run `seq` as the sole ordering authority; a swallowed append leaves a hole the read path DECLARES as a `GapMarker` (interior and trailing) rather than renumbering it away, so "nothing happened" is distinguishable from "an event was lost"
  - read routes over the durable journal, all answered with no in-memory `run_registry` dependency: `GET /workflows/runs/{id}` (enriched inspect), `GET .../events` (ordered timeline), `GET .../compare?against=` (per-step comparison), `GET .../diagnostics` (troubleshooting bundle). All four require a `cao:read`/`cao:write`/`cao:admin` scope when auth is enabled
  - SSE live-follow content-negotiated onto the same `.../events` path (`Accept: text/event-stream` or `?stream=true`): durable replay from a cursor, exact `Last-Event-ID` reconnect, and a terminal-state guard that closes an already-ended run instead of hanging the follower
  - `GET /terminals/{id}/output/range` for byte-exact reads of a terminal's append-only log
  - `DELETE /workflows/runs/{id}` (write/admin scope) removing a run and all its retained data, plus an age + run-count retention sweep at startup — `0` on either bound DISABLES that bound
  - web: a run list / detail surface with event-timeline playback (transport + ARIA scrubber), declared-gap markers, and a terminal pane synced to the selected event
  - output capture is OFF by default (metadata only); when enabled, retained text is size-capped and funnelled through the existing `audit_log` sanitizer. Four new `memory` settings — see [Configuration](docs/configuration.md#memory-memory)
- **Asynchronous workflow-run lifecycle** — runs are now submittable without holding a connection open for their whole duration (#505):
  - `POST /workflows/runs:submit` acks `202 {run_id, state, links}` the instant the run is durably journaled, then drives it in a background task. The blocking `POST /workflows/runs` is retained and byte-compatible. `GET /workflows/runs` lists journaled runs newest-first (`?state=`, `?limit=`), and `GET /workflows/runs/{run_id}/result` returns the full retained result for a detached, in-flight, or post-restart run
  - four CLI verbs: `cao workflow runs` (list recorded runs), `wait` (follow an already-submitted run), `result` (full detail), `events` (live SSE progress); `run` now submits-and-follows by default, with `--detach` to submit and exit and `--wait` for the retained blocking path
  - six MCP tools: `workflow_start`, `workflow_status`, `workflow_result`, `workflow_list`, `workflow_wait`, `workflow_events`. Note `workflow_list` lists **runs**, whereas the CLI's `list` lists **specs** — see the CLI ↔ MCP name mapping in `docs/workflows.md`
  - `GET /workflows/runs` and `GET /workflows/runs/{run_id}/result` require a read scope (`cao:read`, `cao:write`, or `cao:admin`) when auth is enabled

### Changed

- **BREAKING** — `cao workflow run --json` output shape (#505). It previously echoed the complete `WorkflowRunResult` (`run_id`, `workflow_name`, `state`, `steps[]`, timestamps, `kind`); because the default path now submits-and-follows rather than blocking, it emits only the stable terminal object `{"run_id": ..., "state": ...}`. A non-TTY plain `run` emits the same JSON. **Scripts reading `steps[]` or `workflow_name` off `run --json` must change** — use `cao workflow result <id> --json` for full detail, or `cao workflow status <id> --json` for a mid-run snapshot. `run --wait --json` is unaffected and still returns the complete `WorkflowRunResult`. Exit codes are unchanged across TTY, non-TTY, and `--json`
- **BREAKING** — the run-level `output` field is no longer returned by `GET /workflows/runs/{run_id}/result` or by the `workflow_result` / `workflow_wait` MCP tools (#505). Run-level output is not journaled, so the field was structurally always `null` on those journal-assembled surfaces; it is dropped rather than advertised. Per-step outputs are unaffected (`steps[].output`), and the blocking `run --wait` path still returns run-level output for script-tier runs
- memory + stub providers (#348 B2) (#416)

- API routes + OKF/Obsidian/GraphML sinks (#348 B3) (#424)

- add read_session_output tool for typed worker-output readback (#422)

- Sigma renderer + web graph view + projection cache (#348 B4) (#442)

- script-tier run surface + author shim — U5+U6 (#312) (#399)

- runtime inputs + cao-workflow authoring skill (#420) (#450)

- AG-UI protocol adapter + generative UI — PR #387 Phase-A (reconciled) (#436)

- add source-aware `cao update` command (#26) (#445)

- add profile discovery via cao profile find + find_prof… (#438)

- validate Markdown links repository-wide (#474)

- secret scanning + leak-response runbook (#457) (#477)

- AG-UI Phase 2 — L2 construct library (#458) (#485)

- modernize integration for herdr 0.7.x (broadcast events, api snapshot, native --env) (#502)

- add agent profile routing (#486)

- add an explicit model override to handoff/assign (#501)

- make CAO_HOME_DIR env-overridable (#467)

- pass model and initial message when launching sessions (#513)

- opt-in self-learning loop — outcome capture, retrospection, instruction promotion (#514) (#515)

- add read-only profile search, template and preview endpoints (#523)

- add explicit v2/KAS engine selection (#470)

- add AI-DLC portfolio orchestration example (#521)

- typed memory relationship store (#511) (#524)


### Changed

- extract local-store persistence into profile_store (#543)


### Documentation

- add Simplified Chinese README (#439)

- add tags and capabilities to aws example profiles (#484)

- improve README and documentation navigation (#472)

- reconcile historical implementation records (#499)

- sync provider lists/tables with all 9 registered providers (#490)

- add Docusaurus documentation site and interactive courses (#531)

- link the documentation site from both READMEs (#536)


### Fixed

- workflow: the background drive's FAILED backstop no longer overwrites an already-settled run (#505). It fired unconditionally on any exception, so a drive that raised *after* the engine journaled COMPLETED/CANCELLED — during post-settlement bookkeeping — rewrote that terminal state to FAILED, making the durable record misreport the run's outcome. The write is now a conditional `UPDATE ... WHERE state = 'running'` in the journal DAL (atomic, so no concurrent settle can interleave), covering both the `Exception` and `CancelledError` arms; a run that raises *before* settling still lands FAILED as before, so no run is left orphaned in `running`
- workflow: `cao workflow events` closes its streamed SSE response on every exit path (#505). The follower breaks out of its loop on a terminal frame and abandons the generator on each reconnect, so without an explicit close the socket survived until garbage collection and a long follow with repeated reconnects accumulated live file descriptors. The equivalent MCP tool was already hardened
- workflow: a caller-supplied `run_id` that loses a concurrent-submit race now returns `409` instead of `500` (#505). The uniqueness pre-check and the durable insert are not one atomic operation, so both submits can pass the pre-check; the loser's `IntegrityError` is now mapped to the same `409` the serialized case reports
- profile store writes are now atomic and inter-process safe. Both store writes were previously bare `write_text` calls, so a concurrent `cao profile` write and a server-side write could interleave or leave a partial file. Adds `locked_atomic_write` to `utils/atomic_file.py` as the blind-write sibling of `locked_atomic_rewrite` (#492): it shares the same lock, temp file, fsync, mode preservation and `os.replace`, but skips the read, so a corrupt or non-UTF-8 file in the agent store can still be replaced by the install that would have repaired it instead of failing with `UnicodeDecodeError` (#543)
- self-healing pipe-pane liveness watchdog for silently-stalled FIFO forwarding (fixes #388) (#397), including detection of a stall that settles into a new static frame before the next poll and of a pipe that never delivers a single byte from terminal creation (cold start, harness-control#93) — see `CAO_PIPE_LIVENESS_COLD_START_GRACE_S` / `CAO_PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS` in `docs/configuration.md`
- web: attach web terminals through the configured backend so herdr-backed terminals no longer fail to attach (#417)
- honor profile frontmatter `provider:` during install (flag > frontmatter > default) (#414)
- deliver messages with `tmux paste-buffer -p` on tmux >= 3.7, which sanitizes pasted buffers through vis(3) and rendered the previously hand-crafted `ESC [200~`/`ESC [201~` markers as literal `^[[200~` garbage in the receiving TUI; tmux < 3.7 keeps the hand-crafted wrap so TUIs that never enable DECSET 2004 (e.g. kiro-cli) still receive multi-line messages as a single input (#413)
- handoff workers now inherit the supervisor's working directory server-side in run_agent_step (#423)
- sync devcontainer feature version with pyproject.toml on release (#419)

- TOML-escape MCP command/args/env in -c overrides (#404)

- roll back DB terminal row when create_terminal fails (#421)

- stop logging full send_keys payloads at INFO (#427)

- enable Jinja2 autoescape in agent-profile scaffolding (#429)

- make Jinja2 autoescape safety net real + lock it with a test (#429 follow-up) (#434)

- stop bump_version clobbering mypy python_version (#435)

- clear CodeQL clear-text-storage false positive in memory tests (#142) (#440)

- honor frontmatter provider with flag > frontmatter > default precedence (fixes #414) (#431)

- attach terminals through configured backend (#417)

- self-healing pipe-pane liveness watchdog (fixes #388) (#397)

- allow containerized/wrapped provider agents to initialize (#400) (#428)

- gate first paste on real input readiness (settle check) (#441)

- validate MCP server names and env keys in -c override paths (#426)

- kill orphaned window on init failure for new_session=False (harness-control#186) (#446)

- isolate wait_until_input_ready in claude_code init-timeout tests (#452)

- repair metadata projections after database replacement (#449)

- scrub personal PII from provider fixtures + add recurrence guard (#456)

- inline select_autoescape so bandit B701 recognizes the autoescape config (#462)

- inherit supervisor working directory server-side in run_agent_step (#423)

- colocate CodeQL path-injection guards with filesystem sinks (#166/#167/#168) (#461)

- exclude own-line effort footer from response-marker detection (#466)

- content-based staleness guard prevents stale COMPLETED (#407) (#480)

- detect trust prompt v2 + block delivery into dead terminals (#482)

- verify deferred-init worker started and re-submit dropped input (#479)

- oversize pyte screen to 400x200 so taller attached terminals reach IDLE (#478)

- bottom-anchor dialog detection + plan-approval dismissal (#405) (#481)

- gate live integration tests + classify trust-all-tools dialog (#483)

- suppress and dismiss startup update-available dialog (#488)

- add autouse fixture to strip leaked CAO env vars (#489)

- suppress pytest warnings summary in pre-push hook to avoid BlockingIOError (#491)

- validate CAO terminal ID before API requests (#475)

- use paste-buffer -p instead of hand-crafted bracketed-paste markers (fixes #413) (#430)

- prevent deferred-init retry loop from re-pasting into working OpenCode workers (#496)

- bound graph lint projection (#507)

- skip bracketed-paste wrap when the pane is a bare shell (#500)

- mock cleanup-nudge lookup in assign tests to stop live-server leakage (#508)

- submit orchestrated/flow tasks reliably on Gemini 3.x agy (#517)

- make the state-detection rolling buffer size configurable, raise default to 32KB (#425)

- detect v0.145 idle composer at startup (#527)

- reset backend registry singleton between tests (#522) (#528)

- inter-process-safe atomic read-modify-write for memory/skill files (caom-47e) (#492)

- upgrade postcss to >=8.5.18 in web (#535)

- validate Origin on terminal WebSocket to block cross-site WebSocket hijacking (CWE-1385) (#533)

- convert kimi/antigravity/copilot startup-prompt handling to async (#509)


### Other

- bump mcp from 1.26.0 to 1.28.1 (#455)

- bump postcss from 8.5.16 to 8.5.25 in /cao_mcp_apps (#530)

- pin paste submission overrides (#544)

- release v2.4.0

## [2.3.0] - 2026-07-12

### Added

- add reconciliation sweep for orphaned PENDING messages (#266)

- add provider support (#272)

- add herdr terminal backend with event-driven inbox delivery (#271)

- bundle built-in memory plugins for Claude Code, Kiro, and Codex (#269)

- Web UI support for the memory system (#290)

- Phase 3 — LLM wiki compile, cross-references, lint, audit log, scoring (#285)

- add Cursor CLI as a first-class provider (#296)

- pyte rendered-screen status detection (closes #287) (#293)

- gate network egress behind a web_fetch tool category (#311)

- discover skills from extra_skill_dirs (mirror extra_agent_dirs) (#277)

- pass per-agent config overrides via codexConfig (#278)

- add optional Session Name field to the Spawn Agent dialog (#279)

- worker status/output tools + orchestration worker profiles (#324)

- spec grammar + run_agent_step substrate (#312 Bolt 1) (#320)

- authoring, persistence & structured returns (#312 Bolt 2) (#326)

- add Antigravity CLI (agy) provider (#323)

- wiki self-healing — `cao memory heal` (Phase 4 U1) (#306)

- sandboxed host-rendered fleet UI (SEP-1865) + capabil… (#332)

- cross-project federation — FEDERATED scope (Phase 4 U3) (#314)

- canonical-source fidelity + host-delegated dogfooding (#347)

- orchestration run engine (#312 Bolt 3 / N5) (#329)

- rename cao flow → cao schedule with deprecated alias (#380)

- cross-node fleet coordinator (bootstrap + AI conductor) (#365)

- scope the per-agent skill catalog via a profile allowlist (#351)

- Open Knowledge Format (OKF) export/import (#345) (#384)

- durable run journal + resume (#312 N6) (#372)

- script-tier journal extension (#312 C3/U3) (#391)

- script linter + run-step env guard (#312 B2: U1+U2) (#394)

- fleet web panel + live console (#366)

- enable/disable an agent-profile directory (closes #280, #281) (#368)

- script-tier execution engine — U4 runner (#312) (#396)

- GraphView contract + provider/sink registries (#348, B1) (#402)


### Documentation

- add per-scope store samples, on-disk comparison, SQLite architecture diagram (#355)

- add AWS cloud-ops agent examples with config (#377)

- fleet coordinator guide (docs/fleet_instructions.md) (#367)

- draft CHANGELOG for v2.3.0 (#418)


### Fixed

- stop TestPyPI squats breaking the release smoke test (#270)

- handle v0.136+ TUI footer and skip MCP tool-call markers … (#274)

- mark messages DELIVERED before send_input to stop double delivery (#265)

- address CodeQL command-injection and URL-sanitization … (#288)

- structural callback routing for worker agents (#284) (#289)

- auto-detect server backend + herdr reconcile fixes (#309)

- detect TUI idle state without falling back to --legacy-ui (#330)

- harden Claude and OpenCode status detection (#327)

- stop echoed system prompt from short-circuiting trust dialog (#319)

- allow permissionMode to override yolo in claude_code provider (#322)

- adopt vite 8 / vitest 4 and restore the 90% coverage floor (#346)

- also deny Claude Code's renamed subagent tool (Agent) (#350)

- accept workspace-trust dialog so init doesn't hang (#364)

- dismiss startup upgrade-reminder dialog so init doesn't hang (#363)

- read herdr native status in all providers (#359) (#361)

- add --version/-V option (#354) (#379)

- dismiss startup feedback survey so init doesn't block (#371)

- non-blocking reader loop + event-loop-safe teardown (fixes #382) (#383)

- fix: unblock multi-agent orchestration on kiro-cli 2.11 — event-loop deadlock, serial/timed-out assign, and provider output/status detection (#390)

- background task ("✻ Waiting for N workflows") no longer reads as COMPLETED (fixes #392) (#393)

- validate user-derived path components to close CodeQL path-injection alerts (#401)

- launch bundled cao-mcp-server without a per-launch network fetch (#403)


### Other

- Potential fix for code scanning alert no. 66: Uncontrolled command line (#275)

- bump starlette from 0.49.1 to 1.0.1 (#276)

- Event-driven architecture: rebase onto main + green the suite (continues #115) (#273)

- bump esbuild, @vitejs/plugin-react and vite in /web (#295)

- bump pyjwt from 2.12.0 to 2.13.0 (#301)

- bump python-multipart from 0.0.27 to 0.0.31 (#302)

- bump cryptography from 46.0.7 to 48.0.1 (#303)

- bump starlette from 1.0.1 to 1.3.1 (#304)

- bump form-data from 4.0.5 to 4.0.6 in /web (#305)

- Add configurable server timeouts and file-based Claude Code prompt delivery (#318)

- fix kiro/q integration tests (mock_db signature + event-loop starvation) (#333)

- bump happy-dom from 15.11.7 to 20.10.6 in /cao_mcp_apps (#341)

- Remove Amazon Q CLI and Gemini CLI providers (#353)

- quickly remove some comments (#370)

- Unify CAO configuration into a single source of truth (#357) (#381)

- bump ws from 8.20.0 to 8.21.0 in /web (#398)

- [Feat] cao profile — profile lifecycle management (#395)

- release v2.3.0

## [2.2.0] - 2026-06-02

### Added

- Add Opencode provider label to Web UI (#217)

- add install with pypi in README.md (#214)

- Build an MCP server for cao operations (#166)

- shell command tracking, flow recycling fixes, and inbox delivery reliability (#230)

- auto-delete handoff terminals with snapshot-based restore (#233)

- enhance DashboardHome with filtering, sorting, grouping, and session deletion (#200)

- persistent agent memory system (Phase 1) — foundation (#245)

- forward env vars to supervisor and child agents (#259)

- SQLite metadata, BM25 fallback, context-manager injection (#254)

- auto-derive CORS origins from cao-server --host/--port (#261)

- Official devcontainer feature for CAO (#260)

- eager inbox delivery for providers that buffer input during processing (#251)

- Phase 2.5 hardening (#262)


### Documentation

- add external tool integration guide for CAO skills (#241)

- fix web UI build instructions and add 404 troubleshooting (#252)

- add Hermes Agent as worked example (#253)


### Fixed

- detect TUI Initializing... to prevent false IDLE (#211) (#215)

- start panes at 220x50 to avoid kiro-cli SIGWINCH input death (#216) (#218)

- Add a poller to opencode CLI inbox delivery to drain s… (#210)

- resolve profile.provider in create_session() (#198)

- wait for idle before tmux attach on non-headless launch (#220) (#221)

- fix mcp worker provider resolution (#224)

- harden agent-profile install against SSRF and path inje… (#226)

- isolate GEMINI.md per terminal in a dedicated workspace (#227)

- guard agent-name path lookups against traversal (#228)

- fix ops mcp profile provider resolution (#229)

- fix handoff hang for Q Developer Pro — Credits marker not emitted in TUI mode (#238)

- filter environment to prevent 'command too long' errors (#246)

- default TERM to xterm-256color for tmux PTY attach (#256)

- make network allowlists configurable via env vars (#255)

- resolve profile.provider regardless of yolo/allowed-tools branch (#257)

- reject send_message when receiver_id equals sender (#24) (#263)


### Other

- [Docs]Reorganize README, split detail into topic docs, and add control-plane overview (#225)

- bump python-multipart from 0.0.26 to 0.0.27 (#232)

- bump urllib3 from 2.6.3 to 2.7.0 (#234)

- bump authlib from 1.6.11 to 1.6.12 (#236)

- bump idna from 3.10 to 3.15 (#247)

- Add optional permission_mode field to AgentProfile for claude_code provider (#244)

- Add optional codexProfile field to AgentProfile for codex provider (#250)

- Fix/codeql 66 tmux name validation (#258)

- bump vitest from 3.2.4 to 4.1.0 in /web (#267)

- Fix/resolve provider explicit override (#268)

## [2.1.1] - 2026-04-28

### Added

- Add OpenCode CLI provider support (#193)

- add PyPI publish workflow and update pyproject.toml (#123)


### Fixed

- honour profile.provider when --provider flag is not given (#196)

- eliminate PROCESSING false-positives from compaction and /exit (#199)

- honor --yolo and profile.model at launch (#201)

- recognise Copilot v1.0.31+ status bar and breadcrumb as footer lines for idle detection (#184)

- fix the cliff github api timeout with env GITHUB_TOKEN for git cliff to pickup. Add retry mechanism in script (#212)


### Other

- Feat/publish cao to pypi (#209)

- bump postcss from 8.5.8 to 8.5.12 in /web (#208)

- switch to deploy key to bypass commit to main (#213)

- release v2.1.1

## [2.1.0] - 2026-04-22

### Added

- Add support for skills (#145)

- Build support for external plugins (#172)

- add cao session command, HTTP API refactor, and kiro-cli fixes (#187)


### Documentation

- add managed skills to README, restore developer.md orch… (#170)

- cut 2.1.0 release notes (#195)

- correct 2.1.0 entry — remove unmerged feature, fix refs (#197)


### Fixed

- Bundle built WebUI assets within Python wheel (#169)

- prevent stale processing spinners from blocking inbox delivery (#104) (#106)

- structural PROCESSING detection immune to ❯ position race (#177)

- read GEMINI.md for Gemini skill catalog injection assertion (#180)

- gracefully handle missing agent profiles in CAO store (#186)

- handle Kiro CLI 2.0 Credits-before-separator layout (#188)

- honor profile.model at terminal creation (#189)

- position-aware 'Kiro is working' check prevents stale PROCESSING blocking handoffs (#185)

- prevent false-positive IDLE on shell prompt during startup (#190)

- only kill sessions this call created on cleanup (#191)


### Other

- bump pytest from 8.4.2 to 9.0.3 (#173)

- bump python-multipart from 0.0.22 to 0.0.26 (#175)

- bump authlib from 1.6.9 to 1.6.11 (#178)

- bump python-dotenv from 1.1.1 to 1.2.2 (#194)

## [2.0.2] - 2026-04-10

### Added

- Support agent-profile environment variable injection and loading (#156)

- add cao-provider skill for new CLI agent providers (#154)

- add full TUI mode support with --legacy-ui fallback (#159) (#163)


### Fixed

- improve Web UI terminal scroll and paste reliability (#162)


### Other

- Fix/providers endpoint missing entries (#158)

- bump vite from 6.4.1 to 6.4.2 in /web (#160)

- bump cryptography from 46.0.6 to 46.0.7 (#165)

## [2.0.1] - 2026-04-03

### Added

- add allowedTools — universal tool restriction across … (#125)


### Fixed

- add --legacy-ui flag for new Kiro CLI TUI compatibility (#138)

- add new TUI fallback patterns + fix #137 exception handling  (#140)

- replace WAITING_USER_ANSWER regex to prevent stale scrollback false positives (#142)

- honor child allowedTools=["*"] instead of inheriting parent restrictions (#141) (#144)

- clarify prompt, add --auto-approve, document TOOL_MAPPING (#146)


### Other

- bump cryptography from 46.0.5 to 46.0.6 (#135)

- bump pygments from 2.19.2 to 2.20.0 (#136)

- bump fastmcp from 2.14.5 to 3.2.0 (#139)

## [2.0.0] - 2026-03-26

### Added

- add Gemini CLI provider (#102)

- Support provider override in agent profiles for cross-provider workflows (#101)

- add Kimi CLI provider (#113)

- add copilot_cli provider (#82)

- add Web UI dashboard with configurable agent directories (#108)

- auto-inject sender terminal ID in assign and send_message (#98)


### Documentation

- add cross-provider example profiles and fix missing gemini_cli in README (#109)


### Fixed

- accept IDLE or COMPLETED during terminal init (#111)

- add extraction retry for TUI-based providers (Gemini CLI) (#117)

- add CodeQL SafeAccessCheck guard for path injection (#121)

- add DNS rebinding protection via Host header validation (#124)

- pin trivy-action to SHA instead of mutable master ref (#126)

- handle bypass permissions prompt on startup (#119) (#120)

- bump vite 5→6.4.1 and vitest 2→3.2.4 to fix esbuild vulner… (#129)


### Other

- Fixes the `400 Bad Request` error when launching agents in directories outside `~/`, such as `/Volumes/workplace` on macOS.  (#110)

- bump black from 25.9.0 to 26.3.1 (#114)

- bump pyjwt from 2.11.0 to 2.12.0 (#118)

- bump authlib from 1.6.7 to 1.6.9 (#122)

- bump requests from 2.32.5 to 2.33.0 (#130)

- Docs/update readme and changelog (#132)

- Docs/update readme and changelog (#133)

## [1.1.1] - 2026-03-09

### Fixed

- Fix regex to catch Claude Code Processing spinner (#92)

- Update failing Q CLI unit tests due to working directory validation (#94)

- Update Codex TUI footer detection for v0.111.0 (#99)


### Other

- bump authlib from 1.6.6 to 1.6.7 (#97)

## [1.1.0] - 2026-02-27

### Added

- add --dangerously-skip-permissions, --yolo flag, tmux paste fix, and dep upgrades (#76)

- rewrite Codex provider, framework improvements, security fix, and docs (#77)

- add CLI commands, shell safety fixes, agent profiles, and docs (#83)


### Fixed

- detect active permission prompts using line-based counting (#71)


### Other

- bump cryptography from 46.0.1 to 46.0.5 (#72)

- add comprehensive unit tests, E2E tests, and CI workflows (#81)

## [1.0.3] - 2026-02-09

### Fixed

- Synchronize status detection with response completion (#62)

- update IDLE_PROMPT_PATTERN_LOG to match actual kiro-cli ANSI output (#65)

- prevent permission prompt pattern from matching stale prompts (#69)


### Other

- replace chunked send_keys with paste-buffer for instant delivery (#67)

## [1.0.2] - 2026-02-05

### Added

- add dynamic working directory inheritance for spawned agents (#47)


### Fixed

- Handle CLI prompts with trailing text (#61)

## [1.0.1] - 2026-02-02

### Fixed

- release workflow version parsing (#60)


### Other

- bump authlib from 1.6.4 to 1.6.6 (#51)

- bump urllib3 from 2.5.0 to 2.6.3 (#52)

- Remove unused constants and enum values (#45)

- bump starlette from 0.48.0 to 0.49.1 (#53)

- bump werkzeug from 3.1.1 to 3.1.5 (#55)

- bump python-multipart from 0.0.20 to 0.0.22 (#58)

- Escape newlines in Claude Code multiline system prompts (#59)

## [1.0.0] - 2026-01-23

### Added

- async delegate (#3)

- add badge to deepwiki for weekly auto-refresh (#13)

- add Codex CLI provider (#39)

- add changelog and automated release workflow (#50)


### Changed

- rename 'delegate' to 'assign' throughout codebase (#10)


### Fixed

- Handle percentage in agent prompt pattern (#4)

- resolve code formatting issues in upstream main (#40)


### Other

- Initial commit

- Initial Launch (#1)

- Inbox Service (#2)

- tmux install script (#5)

- update README: orchestration modes (#6)

- Update README.md (#7)

- Update issue templates (#8)

- Document update with Mermaid process diagram (#9)

- Adding examples for assign (async parallel) (#11)

- update idle prompt pattern for Q CLI to use consistent color codes (#15)

- Add comprehensive test suite for Q CLI provider (#16)

- Add code formatting and type checking with Black, isort, and mypy (#20)

- Make Q CLI Prompt Pattern Matching ANSI color-agnostic (#18)

- Add explicit permissions to workflow

- Kiro CLI provider (#25)

- Add GET endpoint for inbox messages with status filtering (#30)

- Adding git to the install dependencies message (#28)

- Bump to v0.51.0, update method name (#31)

- accept optional U+03BB (λ) after % in kiro and q CLIs (#44)
