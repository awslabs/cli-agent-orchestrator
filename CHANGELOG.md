# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [Unreleased]

### Added

- add Antigravity CLI (`agy`) provider — Google's terminal-native coding agent and the successor to the Gemini CLI after the free "Login with Google" path was retired (#323)

- add built-in Hermes provider support through profile-configured `hermesProfile` wrappers

- add `examples/fleet` — a cross-node fleet coordinator that manages many CAO nodes from one place: one-command node bootstrap, a `fleet` control helper (list/show/exec against any node), and an AI conductor wired to one `cao-ops-mcp-server` per node. Purely additive under `examples/`; each node stays a stateless client of the existing `cao-server` API (#349)

### Changed

- rename `cao flow` → `cao schedule` to avoid confusion with the new `cao workflow` feature. `cao flow` remains as a hidden deprecated alias that prints a warning to stderr; flow files, `~/.cao/flows`, stored schedules, and the `/flows` REST API are unchanged, and the web UI only updates its CLI hint string (#378)

### Deprecated

- `cao flow` — use `cao schedule` instead; the alias will be removed in a future release (#378)

### Fixed

- fifo: non-blocking FIFO reader loop and event-loop-safe session teardown — reader threads can no longer be stranded in a blocking FIFO `open()` by a stop/reopen race, and `DELETE /sessions` runs teardown in a worker thread, so repeated create/delete cycles can no longer wedge cao-server (#382)

## [2.2.0] - 2026-06-04

### Highlights

- **CAO memory** — Agents can now store and recall knowledge across sessions via `memory_store` / `memory_recall` / `memory_forget` MCP tools. Memories are scoped to `global` / `project` / `session` / `agent`, persisted as wiki-style markdown under `~/.aws/cli-agent-orchestrator/memory/`, indexed in SQLite with BM25 fallback retrieval, and auto-injected as `<cao-memory>` context at session start. Ships with CLI commands (`cao memory list/show/delete/clear`), tiered retention, file-lock concurrent-write safety, per-scope caps, and stable project identity via git remote. See [docs/memory.md](docs/memory.md). (#245, #254, #262)

- **External tool integration: OpenClaw & Hermes Agent** — A new external-tool-integration skill lets CAO orchestrate non-CAO CLI agents (OpenClaw, Hermes Agent, etc.) as first-class workers. Hermes Agent is shipped as a worked end-to-end example. See [docs/external-tool-integration.md](docs/external-tool-integration.md). (#241, #253)

### Added

- Build an MCP server for cao operations (#166)

- auto-delete handoff terminals with snapshot-based restore (#233)

- shell command tracking, flow recycling fixes, and inbox delivery reliability (#230)

- eager inbox delivery for providers that buffer input during processing (#251)

- forward `cao launch --env` vars to supervisor and child agents (#259)

- add optional `codexProfile` field to AgentProfile for codex provider (#250)

- add optional `permission_mode` field to AgentProfile for claude_code provider (#244)

- auto-derive CORS origins from `cao-server --host/--port` (#261)

- official devcontainer feature for CAO (#260)

- memory: Phase 2.5 hardening — per-scope caps, ISO-8601 Z round-trip lock, durability + concurrent flock tests, `memory.enabled` short-circuit, stable project identity via git remote (#262)

- enhance Web UI DashboardHome with filtering, sorting, grouping, and session deletion (#200)

- add OpenCode provider label to Web UI (#217)


### Documentation

- reorganize README, split detail into topic docs, and add control-plane overview (#225)

- fix Web UI build instructions and add 404 troubleshooting (#252)

- add install with pypi in README.md (#214)


### Fixed

- codex: detect v0.136+ TUI footer (`model · path` without `N% left`) so handoff/assign workers reliably reach COMPLETED instead of pinning at IDLE

- codex: skip `• Called <tool>(...)` MCP tool-call markers during last-message extraction so skill body text (including `[CAO Handoff]`) no longer leaks into worker output

- ci: stop TestPyPI squats breaking the release smoke test by installing the package with `--no-deps` and resolving deps from PyPI alone (#270)

- kiro_cli: treat MCP-server boot screen as PROCESSING and gate shell-baseline IDLE on `_initialized` to fix paste-into-boot-screen race that dropped the first message after launch (#268)

- mcp: reject `send_message` when `receiver_id` equals sender (#263)

- tmux name validation hardening (CodeQL #66) (#258)

- launch: resolve profile.provider regardless of yolo/allowed-tools branch (#257)

- api: default TERM to xterm-256color for tmux PTY attach (#256)

- api: make network allowlists configurable via env vars (#255)

- tmux: filter environment to prevent 'command too long' errors (#246)

- session-service: resolve profile.provider in `create_session()` (#198)

- fix mcp worker provider resolution (#224)

- fix ops mcp profile provider resolution (#229)

- agent_profiles: guard agent-name path lookups against traversal (#228)

- install: harden agent-profile install against SSRF and path injection (#226)

- gemini_cli: isolate `GEMINI.md` per terminal in a dedicated workspace (#227)

- kiro_cli: fix handoff hang for Q Developer Pro — Credits marker not emitted in TUI mode (#238)

- kiro_cli: detect TUI `Initializing...` to prevent false IDLE (#215)

- tmux: start panes at 220x50 to avoid kiro-cli SIGWINCH input death (#218)

- launch: wait for idle before tmux attach on non-headless launch (#221)

- opencode: add a poller to OpenCode CLI inbox delivery to drain stuck messages (#210)


### Other

- bump idna from 3.10 to 3.15 (#247)

- bump authlib from 1.6.11 to 1.6.12 (#236)

- bump urllib3 from 2.6.3 to 2.7.0 (#234)

- bump python-multipart from 0.0.26 to 0.0.27 (#232)

- bump vitest from 3.2.4 to 4.1.0 in /web (#267)


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
