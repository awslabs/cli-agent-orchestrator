# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Enable/disable an agent-profile directory without removing it, so its profiles leave the active set while the path stays listed (#280, #281).

- add optional `skills` field to `AgentProfile` to scope the per-agent skill catalog via an fnmatch allowlist; runtime-prompt providers only, `load_skill` resolution unchanged (#351)

- add Antigravity CLI (`agy`) provider — Google's terminal-native coding agent and the successor to the Gemini CLI after the free "Login with Google" path was retired (#323)

- add built-in Hermes provider support through profile-configured `hermesProfile` wrappers

- add OKF memory export/import — `cao memory export`/`cao memory import` CLI commands plus a read-scoped `GET /memory/export` API endpoint streaming a scope as a tar.gz bundle (#345)
- add `examples/fleet` — a cross-node fleet coordinator that manages many CAO nodes from one place: one-command node bootstrap, a `fleet` control helper (list/show/exec against any node), and an AI conductor wired to one `cao-ops-mcp-server` per node. Purely additive under `examples/`; each node stays a stateless client of the existing `cao-server` API (#349)
- add `examples/fleet/panel` — a web control panel + live console for the fleet: a stateless FastAPI app that fans out to every node's `cao-server` REST API and serves a browser SPA (a wall of live agent screens + a focused console). Isolates per-node failures, degrades `/screen` → `/output` for older nodes, and adds opt-in shared-token auth (`CAO_PANEL_TOKEN`) for off-loopback use (#366)

### Changed

- rename `cao flow` → `cao schedule` to avoid confusion with the new `cao workflow` feature. `cao flow` remains as a hidden deprecated alias that prints a warning to stderr; flow files, `~/.cao/flows`, stored schedules, and the `/flows` REST API are unchanged, and the web UI only updates its CLI hint string (#378)

### Deprecated

- `cao flow` — use `cao schedule` instead; the alias will be removed in a future release (#378)

### Fixed

- claude_code: a backgrounded task ("✻ Waiting for N dynamic workflow(s) to finish") no longer reads as COMPLETED — the wait line has no spinner ellipsis (invisible to every PROCESSING check) while the printed response + idle ❯ box look like a finished turn, and it even matched the lenient completion pattern; both the raw-buffer and pyte screen paths now report PROCESSING until a newer response/completion summary appears, so dashboards and wait_until_terminal_status no longer see a mid-run terminal as done (#392)

- fifo: non-blocking FIFO reader loop and event-loop-safe session teardown — reader threads can no longer be stranded in a blocking FIFO `open()` by a stop/reopen race, and `DELETE /sessions` runs teardown in a worker thread, so repeated create/delete cycles can no longer wedge cao-server (#382)
- kiro-cli 2.11 compatibility:
  - bracketed paste now sends 2 Enters with a 1s submit delay so pasted task text is actually submitted (kiro 2.11's TUI swallows the single Enter used by older versions, leaving the message unsent)
  - `TUI_PROCESSING_PATTERN` matches both `"Kiro is working"` (pre-2.11) and `"Thinking..."` (2.11+)
  - Check 6 (no-Credits completion path) now requires a full bordered response box (two separators + ≥2 content lines) instead of any idle-prompt match after `input_received`; kiro 2.11 keeps the `"ask a question or describe a task"` placeholder in the raw buffer at all times, so the previous logic tore workers down within seconds of receiving a task
  - always launch `kiro-cli chat` with `--trust-all-tools`, not just in yolo mode. kiro 2.11 introduced a `"subagent requires approval"` interactive prompt that fires on every MCP tool call spawning a subagent (e.g. `cao-mcp-server` `assign`/`handoff`); with no human at the terminal in headless orchestration, the supervisor deadlocked on the dialog. CAO still enforces tool scoping at its own layers (profile `allowedTools` + MCP allowlist), so bypassing kiro's UI-level per-invocation confirm is safe. Also broadened `TUI_PERMISSION_PATTERN` to detect the new `Yes / Trust / No` layout and the `"subagent requires approval"` header
- status monitor: `send_input` now uses `clear_rolling_buffer` (byte-only) instead of `reset_buffer` so the sticky-latch arm set by `notify_input_sent` survives. Prevents the IDLE→PROCESSING transition from being latch-blocked when kiro 2.11's TUI immediately renders a partial idle frame after `send_input` (regression seen in `test_supervisor_assign_and_handoff`: supervisor completed real work but status stayed IDLE for the whole turn)
- fifo reader: coalesce chunks arriving within a 50ms window into one publish. Kiro's TUI spinner animates ~10 fps and each frame is a separate FIFO write — without coalescing that flooded the shared async queue and dropped worker state transitions along with the animation noise, breaking assign and handoff (supervisor never saw the worker's completion). Coalescing reduces publish rate ~20x during bursts and keeps the queue drained
- event_bus: rate-limit "queue full" drop reporting to at most one line per topic per second (first drop still logs immediately so back-pressure is not silently swallowed). Under a real dual-worker output burst the previous per-drop ERROR log accumulated 42,000+ lines in ~20 minutes, which itself contributed to event-loop starvation. Also downgraded the message from ERROR to WARNING — a dropped event is a soft signal, not a fatal condition. The per-topic drop-state maps are now bounded: since topics embed terminal IDs, a long-running server that churns through many short-lived terminals would otherwise accumulate a dead entry per terminal forever — stale entries (idle past a TTL) are evicted once the map grows past a cap
- log_writer: drain the event-bus queue in batches of up to 256 events and group same-file writes so each unique log file is opened at most once per batch. The prior "one asyncio.to_thread(write) per event" pattern capped throughput at ~one file-write per event-loop tick, which was slower than the FIFO reader's publish rate under two concurrent evaluators streaming multi-KB frames. Ordering per terminal is preserved (chunks are concatenated in drain order before the write)
- event loop: offload blocking tmux subprocess I/O off the asyncio event loop — the most operator-relevant fix here. `StatusMonitor` chunk processing (which shells out to `tmux` per output burst for tmux-backed providers) and the blocking `terminal_service` calls in the API handlers (`send_input`, `get_output`, `exit_terminal_cli`, `delete_terminal`, `send_special_key`, `get_terminal`) and in `run_agent_step` (handoff) now run via `asyncio.to_thread`. Under concurrent worker output these were forking `tmux` directly on the loop, freezing the whole server — `/health` and the supervisor's follow-up `assign` calls stranded until clients timed out. Diagnosed via lldb (loop thread parked in `__fork`/`subprocess_fork_exec`). Same hazard class as #382, previously fixed only for `DELETE /sessions`. Spawned detection/deferred-init tasks are held in strong-reference sets (asyncio only weak-refs `create_task` results)
- codex: `extract_last_message_from_script` now strips ALL terminal escape sequences (`strip_terminal_escapes`) instead of only SGR colour codes. codex's TUI emits cursor-move/erase CSI sequences heavily; the SGR-only strip left them in, so `get_output(mode=last)` returned escape garbage instead of the response
- poll_until_done (`cao launch` / `cao session send`, sync mode): now returns on a STABLE ready state — COMPLETED immediately (unchanged), or IDLE after the agent has been observed working and idle then persists for a short window. Previously required COMPLETED only, so a kiro agent that finished a turn at IDLE with no Credits marker would hang until the 300s timeout. This semantics change affects every provider, not just kiro. Only PROCESSING/WAITING_USER_ANSWER now flip the "observed working" flag — UNKNOWN does not, since a terminal can report UNKNOWN before it starts (no output yet / provider not registered / deferred init) and counting it would let a following stable idle return early with empty output. The status GET also carries a per-request timeout so a stalled server cannot block past the outer timeout budget
- status monitor: `_arm_quiesce_timer` now cancels any outstanding quiescence timer for a terminal before arming the new one. Several output chunks arriving in quick succession queued multiple `_arm` closures; the later one overwrote the stored `TimerHandle` while leaving the earlier timer live, so two timers fired and a stale one firing mid-burst caused early/duplicate quiescence detections and status flaps. One outstanding timer per terminal now, always the latest
- terminal_service: deferred-init failure path logs with `exc_info=True` (preserving the traceback) and formats the exception with `{e!r}` in both the log line and the supervisor-facing inbox message, so provider-supplied error text can't inject newlines/control characters
- api: `POST /sessions/{name}/terminals` now rejects an `initial_message` / `initial_message_orchestration_type` body with 400 when `defer_init=false` — that payload is only delivered on the deferred-init path, so silently dropping it previously surfaced as a hard-to-diagnose "worker never received task". Deliberate 4xx responses (this guard and the invalid-orchestration-type check) also propagate as-is instead of being masked as 500

### Security

- memory: validate every user-derived path component (`key`, `scope`, `scope_id`) as a single safe path segment and confine assembled wiki/index paths under the memory base directory via `os.path.realpath` + an explicit containment guard, closing the 11 CodeQL `py/path-injection` alerts in `memory_service.py`. Added shared helpers `validate_path_component` / `safe_join_under_base` in `utils/path_validation.py`. The remaining `py/clear-text-storage-sensitive-data` alert is assessed as a false positive (the memory wiki is intentionally plaintext markdown; the flagged value is a topic `key` slug, not a credential) and documented in-code for won't-fix dismissal.

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
