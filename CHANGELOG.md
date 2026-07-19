# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `cao profile find <query>` CLI verb and `find_profiles` MCP tool for keyword/BM25 profile discovery over metadata (name, description, tags, capabilities); metadata-only, never exposes prompt bodies (#340)
- Optional `capabilities` and `tags` arrays in the agent profile frontmatter schema (#340)

### Fixed

- self-healing pipe-pane liveness watchdog for silently-stalled FIFO forwarding (fixes #388) (#397), including detection of a stall that settles into a new static frame before the next poll and of a pipe that never delivers a single byte from terminal creation (cold start, harness-control#93) — see `CAO_PIPE_LIVENESS_COLD_START_GRACE_S` / `CAO_PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS` in `docs/configuration.md`
- web: attach web terminals through the configured backend so herdr-backed terminals no longer fail to attach (#417)
- honor profile frontmatter `provider:` during install (flag > frontmatter > default) (#414)
- handoff workers now inherit the supervisor's working directory server-side in run_agent_step (#423)
### Security

- clear three `py/path-injection` CodeQL alerts (code-scanning alerts #166/#167/#168) in `workflow_spec_service` by colocating the path-containment `SafeAccessCheck` with each filesystem sink. `_safe_spec_path` resolved + contained a spec path and then *returned* it, but CodeQL's `str.startswith` barrier is flow-sensitive and function-local, so the "contained" state was dropped at the call boundary and the caller's `open()` / `os.path.isfile()` sink still saw an unchecked path. The read/probe now happen inside guarded helpers (`_read_contained_spec_bytes`, `_contained_spec_file`) where a single positive `startswith(base + os.sep)` guard dominates the sink. Containment semantics are unchanged (a spec whose realpath escapes its validated base still raises `ValueError`); the byte-cap, single-read TOCTOU guarantee, and never-raise `validate_only` contract are all preserved
- clear the `py/clear-text-storage-sensitive-data` CodeQL false positive (code-scanning alert #142) by renaming the local `secret` fixture variable in two `memory_service` secret-gate tests to `gated_content`. CodeQL's name-based heuristic classified the variable named `secret` as a sensitive-data source and traced it into the memory-wiki write (an intentionally plaintext, by-design markdown sink). The literal is the canonical AWS documentation example key, not a real credential; the value and all assertions are unchanged, so the federated secret-gate rejection and global-scope allow paths are still exercised exactly. No production behavior change

## [2.3.0] - 2026-07-12

### Added

- add reconciliation sweep for orphaned PENDING messages (#266)

- add provider support (#272)

- add script-tier workflows: a `.py` workflow spec is now runnable via `cao workflow run` (and the `workflow_run`/`workflow_cancel` MCP tools), with the same `resume`/`cancel`/`status` support as YAML workflows — tier is detected automatically from the file extension (#312)
- add optional `skills` field to `AgentProfile` to scope the per-agent skill catalog via an fnmatch allowlist; runtime-prompt providers only, `load_skill` resolution unchanged (#351)

- **AG-UI typed-event stream** — new `/agui/v1/stream` Server-Sent Events endpoint that maps CAO's normalized fleet events to [AG-UI](https://github.com/ag-ui-protocol/ag-ui) typed events (`RUN_*`, `STEP_*`, `TEXT_MESSAGE_CONTENT`, `TOOL_CALL_START`, `STATE_SNAPSHOT`, `STATE_DELTA`, `GENERATIVE_UI`, `RUN_ERROR`), so any AG-UI-compatible client renders CAO with no custom adapter. Default-off via `CAO_AGUI_ENABLED`; supports `?since=` history replay and, when auth is enabled, a `?access_token=` query-parameter JWT for browser `EventSource` clients. Message bodies are never carried (metadata-only by construction).

- **Generative UI** — agents author allow-listed UI components (approval cards, choice prompts, diff summaries, progress/metrics, agent cards) via the `emit_ui` MCP tool / `POST /agui/v1/emit_ui`. Intents are validated **server-side** against a frozen allow-list (no arbitrary markup) and rendered uniformly across heterogeneous providers. See [docs/agui.md](docs/agui.md#generative-ui).

- **OpenTelemetry GenAI instrumentation** — opt-in, shipped as the `[otel]` optional extra (`pip install cli-agent-orchestrator[otel]`); the base install degrades to no-ops. The inter-agent dispatch seam (`send_message` / `handoff` / `assign`) emits a GenAI `execute_tool` span and a `cao.orchestration.dispatches` counter over OTLP, and propagates W3C trace context (`traceparent`) into plugin events. GenAI `invoke_agent` / `chat` span helpers ship for instrumenting agent- and model-level calls. See [docs/otel-deployment.md](docs/otel-deployment.md).

- **Native multi-agent workflow spec** — a trusted-author YAML workflow grammar with authoring/validation endpoints, a run-engine seam, and `workflow_run` / `workflow_return` / `workflow_cancel` MCP tools (#312).

- **`mock_cli` provider** — a credentials-free mock agent for deterministic CI of orchestration logic without real CLI binaries or secrets. See [docs/mock-cli-provider.md](docs/mock-cli-provider.md).

- add Antigravity CLI (`agy`) provider — Google's terminal-native coding agent and the successor to the Gemini CLI after the free "Login with Google" path was retired (#323)

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


