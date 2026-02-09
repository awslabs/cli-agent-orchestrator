# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [1.1.0] - Unreleased

### Fixed

- Fix Kimi CLI handoff timing out because `get_status()` never returned COMPLETED for long responses: the original logic required both user input box (`╭─`) and response bullets (`^•\s`) in the 200-line tmux capture, but long responses push the input box out of range and structured output (tables, numbered lists) has no `•` bullets; replaced with a latching `_has_received_input` flag that detects the user input box during PROCESSING (when it's still visible) and persists through completion
- Fix Kimi CLI output extraction failing for long responses (>200 lines) where the user input box scrolled out of capture: added `_extract_without_input_box()` fallback that extracts all content before the idle prompt, filtering out status bar and welcome banner lines
- Fix inbox message delivery failing for TUI-based providers (Kimi CLI, Gemini CLI): inbox service passed `tail_lines=5` to `get_status()` but TUI providers need 50+ lines to find the idle prompt; messages stayed PENDING forever because the supervisor was never detected as IDLE
- Fix inbox watchdog log tail check (`_has_idle_pattern`) using only 5 lines, which missed the idle prompt for full-screen TUI providers where the prompt sits mid-screen with 30+ padding lines below; increased to 100 lines so the watchdog reliably triggers delivery when the terminal goes IDLE
- Fix shell command injection risk in Q CLI and Kiro CLI providers: replace f-string command interpolation with `shlex.join()` for safe shell escaping of `agent_profile` values, consistent with other providers
- Fix Gemini CLI `exit_cli()` returning `C-d` (tmux key sequence) but being sent as literal text through `send_input()`: add `send_special_key()` to `TmuxClient` and `terminal_service`, update `exit_terminal` endpoint to detect key sequences (`C-` or `M-` prefix) and route through non-literal tmux key sending
- Fix Claude Code provider not forwarding `CAO_TERMINAL_ID` to MCP server subprocesses: inject `CAO_TERMINAL_ID` into MCP server `env` config, matching Kimi CLI and Codex providers
- Fix `constants.py` docstring missing Kimi CLI and Gemini CLI from provider list
- Fix Claude Code provider failing to launch due to tmux `send-keys` corrupting single quotes in long commands; resolved by main branch's paste-buffer approach (`load-buffer` + `paste-buffer -p`)
- Add missing `wait_for_shell` call to Claude Code provider `initialize()` to match other providers
- Update Claude Code `IDLE_PROMPT_PATTERN` to match both `>` and `❯` prompt styles
- Add `_handle_trust_prompt()` to Claude Code provider to auto-accept the workspace trust dialog when opened in a new/untrusted directory; exclude trust prompt from `WAITING_USER_ANSWER` detection
- Fix Codex provider failing to launch in tmux: add warm-up `echo ready` command before starting codex to prevent immediate exit in fresh sessions
- Fix Codex idle prompt detection for `--no-alt-screen` mode: replace `\Z`-anchored regex with bottom-N-lines approach (`IDLE_PROMPT_TAIL_LINES = 5`) since inline mode keeps scrollback history
- Fix Codex trust prompt `›` falsely matching idle prompt pattern by checking trust prompt before idle prompt in `get_status()`
- Fix Codex status detection not recognizing real interactive output format: update `ASSISTANT_PREFIX_PATTERN` to match `•` bullet responses and `USER_PREFIX_PATTERN` to match `›` user input prompts, enabling `get_status()` to return `COMPLETED` for real Codex output (previously always returned `IDLE`, causing handoff/assign to time out)
- Fix `USER_PREFIX_PATTERN` crossing newline boundaries: use `[^\S\n]` (horizontal whitespace) instead of `\s` to prevent `› \n  ?` from matching as user input
- Add `IDLE_PROMPT_STRICT_PATTERN` for extraction: matches only empty prompt lines (`› ` without text) to distinguish idle prompts from user input lines
- Rewrite `extract_last_message_from_script()` to use user-message-based extraction as primary approach (works for both label and bullet formats) with assistant-marker fallback
- Fix Codex MCP `tool_timeout_sec` not taking effect: change value from `600` (TOML integer) to `600.0` (TOML float) because Codex deserializes via `Option<f64>` and silently rejects integers, falling back to the 60s default
- Fix handoff worker agents not returning results: prepend `[CAO Handoff]` context to the message in `_handoff_impl()` so the worker agent knows this is a blocking handoff and should output results directly instead of attempting to call `send_message` back to the supervisor (which fails because the worker doesn't have the supervisor's terminal ID)
- Fix Codex TUI footer causing false IDLE during handoff: `› Summarize recent commits` in the TUI status bar matched `USER_PREFIX_PATTERN` as a user message, preventing COMPLETED detection; now detects TUI footer (`? for shortcuts` / `context left`) and excludes bottom lines from user-message matching
- Fix Codex TUI progress spinner causing false COMPLETED: `• Working (0s • esc to interrupt)` matched `ASSISTANT_PREFIX_PATTERN` while TUI `›` hint matched idle prompt; added `TUI_PROGRESS_PATTERN` check to return PROCESSING when spinner is active
- Fix Codex output extraction returning TUI chrome: apply same TUI footer detection to `extract_last_message_from_script()` and use `cutoff_pos` as extraction boundary when no strict idle prompt found
- Fix Codex extraction of multi-line user messages: find first `•` assistant marker after user message instead of skipping one line, correctly handling wrapped `[CAO Handoff]` prefix text
- Fix Claude Code worker agents blocking on workspace trust prompt during handoff/assign: add `--dangerously-skip-permissions` flag to bypass trust dialog since CAO already confirms workspace trust during `cao launch`
- Fix Claude Code `PROCESSING_PATTERN` not matching newer Claude Code 2.x spinner format: broaden pattern to match both `(esc to interrupt)` and `(Ns · ↓ tokens · thinking)` formats
- Fix Kimi CLI worker agents created as separate sessions instead of windows: forward `CAO_TERMINAL_ID` to MCP server subprocess via `env` field in `--mcp-config` JSON, matching Codex provider's `env_vars` approach
- Fix Kimi CLI initialization timeout on tall terminals (46+ rows): increase `IDLE_PROMPT_TAIL_LINES` from 10 to 50 to account for TUI padding lines between idle prompt and status bar
- Fix Gemini CLI `_build_gemini_command()` using `--` separator and `export` for MCP server registration: replace with `-e` flag for `CAO_TERMINAL_ID` forwarding and positional command argument without `--`
- Fix Gemini CLI initialization timeout when working directory is the home directory: replace `gemini mcp add` commands with direct `~/.gemini/settings.json` writes, eliminating Node.js subprocess overhead (~2-3s per MCP server)
- Fix Gemini CLI slow assign/handoff (~15s vs ~1s for other providers): MCP server registration now writes directly to `~/.gemini/settings.json` instead of chaining `gemini mcp add --scope user` commands that each spawned a Node.js process
- Fix Gemini CLI failing to launch in fresh tmux sessions: add warm-up `echo CAO_SHELL_READY` command with marker-based polling (15s timeout) before sending the `gemini` command, ensuring the shell environment (PATH, nvm, homebrew) is fully loaded
- Fix all providers' `send_input()` using `tmux send_keys(literal=True)` which sends characters individually, allowing TUI hotkeys (e.g., Gemini CLI's `!` shell mode toggle) to intercept user messages; replace with `send_keys_via_paste()` using `tmux set-buffer` + `paste-buffer -p` (bracketed paste mode) to bypass per-character hotkey handling
- Fix Gemini CLI supervisor agents not receiving system prompt: inject agent profile system prompt via `GEMINI.md` file in the working directory (Gemini CLI reads this for project-level instructions); backs up existing `GEMINI.md` and restores during cleanup

### Added

- E2E assign callback round-trip test (`test_assign_with_callback`) for all providers: verifies full assign flow where worker completes task, result is sent to supervisor's inbox, inbox message delivered (status=DELIVERED), and supervisor processes the callback
- E2E send_message test now verifies inbox message status = DELIVERED (not just stored), proving the inbox delivery pipeline works end-to-end for each provider
- E2E supervisor orchestration test now verifies no inbox messages stuck as PENDING after supervisor completes, catching inbox delivery pipeline failures
- Consolidated `lessons-learned.md` into `lessons-learnt.md` (20 lessons → 14), merging related entries and removing operational notes
- Workspace trust confirmation prompt in `launch.py` before starting providers: asks "Do you trust all the actions in this folder?" since providers are granted full permissions (read, write, execute) in the working directory; supports `--yolo` flag to skip
- Unit tests for `TmuxClient.send_keys` validating paste-buffer delivery (`test/clients/test_tmux_send_keys.py`)
- Claude Code unit tests for `wait_for_shell` lifecycle, shell timeout, `❯` prompt detection, and ANSI-coded output
- Trust prompt handling tests (6 tests) and workspace confirmation tests (4 tests)
- Codex provider agent profile support: inject system prompt via `-c developer_instructions` config override, mirroring Claude Code's `--append-system-prompt` behavior
- Codex provider MCP server support: inject MCP servers from agent profiles via `-c mcp_servers.<name>.<field>=<value>` config overrides (per-session, no global config changes), enabling tools like `handoff` and `send_message` for multi-agent orchestration
- Codex MCP server `CAO_TERMINAL_ID` environment forwarding: automatically adds `env_vars=["CAO_TERMINAL_ID"]` to all MCP server configs so handoff can create new agent windows in the same tmux session
- Codex `_build_codex_command()` method with `shlex.join()` for safe shell escaping and proper quote/backslash/newline handling
- Codex launch flags: `--no-alt-screen` (inline mode for reliable tmux capture) and `--disable shell_snapshot` (prevent SIGTTIN in tmux)
- Codex `_handle_trust_prompt()` to auto-accept workspace trust dialog during initialization
- Codex unit tests: `TestCodexBuildCommand` (10 tests) for command building, agent profile injection, MCP server config, escaping, and error handling
- Codex bullet-format status detection tests: `TestCodexBulletFormatStatusDetection` (7 tests) for COMPLETED, PROCESSING, IDLE, code blocks, error detection, multi-turn, and TUI status bar using real `•` bullet response format
- Codex bullet-format extraction tests: `TestCodexBulletFormatExtraction` (5 tests) for single-line, multi-line, code block, multi-turn, and no-trailing-prompt extraction from `•` bullet format
- Codex TUI spinner status detection tests: `test_get_status_processing_tui_spinner`, `test_get_status_processing_tui_thinking_spinner`, `test_get_status_processing_dynamic_spinner_text` (3 tests) verifying PROCESSING is returned when TUI progress spinner is active
- Handoff message context tests: `TestHandoffMessageContext` (6 tests) in `test/mcp_server/test_handoff.py` verifying `[CAO Handoff]` prefix is prepended only for Codex provider, includes supervisor terminal ID, and preserves the original message
- Multi-agent communication protocol section added to `developer.md` and `reviewer.md` agent profiles explaining handoff vs assign behavior
- End-to-end test suite (`test/e2e/`) with 15 tests covering handoff, assign, and send_message flows across all 3 providers (codex, claude_code, kiro_cli); uses real `data_analyst` and `report_generator` profiles from `examples/assign/`; gated behind `@pytest.mark.e2e` marker, excluded from default `pytest` runs
- Provider documentation: `docs/claude-code.md` and `docs/kiro-cli.md` covering status detection, message extraction, configuration, implementation notes, E2E testing, and troubleshooting
- CI workflow `test-codex-provider.yml` for Codex provider-specific unit tests (path-triggered)
- CI workflow `test-claude-code-provider.yml` for Claude Code provider-specific unit tests (path-triggered)
- Kimi CLI provider (`providers/kimi_cli.py`): full integration with Kimi Code CLI (https://kimi.com/code) including idle/processing/completed/error status detection via TUI prompt patterns (✨/💫), response extraction with thinking-bullet filtering, agent profile support via temp YAML files, MCP server config injection, and temp file cleanup
- Kimi CLI unit tests (`test/providers/test_kimi_cli_unit.py`): 57 tests across 6 test classes covering initialization, status detection, message extraction, command building, patterns, and lifecycle (100% code coverage)
- Kimi CLI test fixtures: 5 fixture files (`kimi_cli_idle_output.txt`, `kimi_cli_completed_output.txt`, `kimi_cli_processing_output.txt`, `kimi_cli_error_output.txt`, `kimi_cli_complex_response.txt`) capturing real terminal output patterns
- Kimi CLI E2E tests: handoff (2 tests), assign (2 tests), and send_message (1 test) in `test/e2e/` gated behind `require_kimi` fixture
- CI workflow `test-kimi-cli-provider.yml` for Kimi CLI provider-specific unit tests (path-triggered)
- Provider documentation: `docs/kimi-cli.md` covering prerequisites, status detection, message extraction, agent profiles, MCP config, and troubleshooting
- Gemini CLI provider (`providers/gemini_cli.py`): full integration with Gemini CLI (https://github.com/google-gemini/gemini-cli) including idle/processing/completed/error status detection via Ink TUI patterns (`*   Type your message` idle prompt, `✦` response prefix, `>` query prefix), response extraction with TUI chrome filtering, MCP server registration via direct `~/.gemini/settings.json` writes, and `CAO_TERMINAL_ID` forwarding via env field
- Gemini CLI unit tests (`test/providers/test_gemini_cli_unit.py`): 57 tests across 6 test classes covering initialization, status detection, message extraction, command building, patterns, and miscellaneous (100% code coverage)
- Gemini CLI test fixtures: 5 fixture files (`gemini_cli_idle_output.txt`, `gemini_cli_completed_output.txt`, `gemini_cli_processing_output.txt`, `gemini_cli_error_output.txt`, `gemini_cli_complex_response.txt`) capturing real terminal output patterns
- Gemini CLI E2E tests: handoff (2 tests), assign (2 tests), and send_message (1 test) in `test/e2e/` gated behind `require_gemini` fixture
- CI workflow `test-gemini-cli-provider.yml` for Gemini CLI provider-specific unit tests (path-triggered)
- Provider documentation: `docs/gemini-cli.md` covering prerequisites, status detection, message extraction, MCP config, and troubleshooting
- `TmuxClient.send_keys_via_paste()` method for sending text via bracketed paste mode (`tmux set-buffer` + `paste-buffer -p`), bypassing TUI hotkey interception in Ink-based and prompt_toolkit-based CLIs
- `TmuxClient.send_special_key()` method for sending tmux key sequences (e.g., `C-d`, `C-c`) non-literally, distinct from `send_keys()` which sends text literally
- Supervisor orchestration E2E tests (`test/e2e/test_supervisor_orchestration.py`): 10 tests across all 5 providers (2 per provider) that verify the full supervisor→worker delegation flow via MCP tools (handoff and assign+handoff), using `analysis_supervisor` profile from `examples/assign/`
- Centralized `skills/` directory as single source of truth for AI coding agent skills (`build-cao-provider`, `skill-creator`), with install instructions for Claude Code (`.claude/skills/`), Codex (`.agents/skills/`), Gemini (`.gemini/skills/`), Kimi (`.kimi/skills/`), and Kiro (`.kiro/skills/`)
- `terminal_service.send_special_key()` wrapper function for the new tmux client method
- Exit terminal endpoint key sequence routing: `POST /terminals/{terminal_id}/exit` now detects `C-`/`M-` prefixed exit commands and sends them as tmux key sequences instead of literal text

## [1.0.2] - 2026-01-30

### Fixed

- Handle CLI prompts with trailing text (#61)

### Added

- Dynamic working directory inheritance for spawned agents (#47)

## [1.0.1] - 2026-01-27

### Fixed

- Release workflow version parsing (#60)
- Escape newlines in Claude Code multiline system prompts (#59)

### Security

- Bump python-multipart from 0.0.20 to 0.0.22 (#58)
- Bump werkzeug from 3.1.1 to 3.1.5 (#55)
- Bump starlette from 0.48.0 to 0.49.1 (#53)
- Bump urllib3 from 2.5.0 to 2.6.3 (#52)
- Bump authlib from 1.6.4 to 1.6.6 (#51)

### Other

- Remove unused constants and enum values (#45)

## [1.0.0] - 2026-01-23

### Added

- async delegate (#3)

- add badge to deepwiki for weekly auto-refresh (#13)

- add Codex CLI provider (#39)


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


