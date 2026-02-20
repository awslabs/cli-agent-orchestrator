# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - Unreleased

### Fixed

- Fix Claude Code provider failing to launch due to tmux `send-keys` corrupting single quotes in long commands; use `literal=True` for text chunks in `tmux.py`
- Add missing `wait_for_shell` call to Claude Code provider `initialize()` to match other providers
- Update Claude Code `IDLE_PROMPT_PATTERN` to match both `>` and `❯` prompt styles
- Add `_handle_trust_prompt()` to Claude Code provider to auto-accept the workspace trust dialog when opened in a new/untrusted directory; exclude trust prompt from `WAITING_USER_ANSWER` detection
- Fix Claude Code worker agents blocking on workspace trust prompt during handoff/assign: add `--dangerously-skip-permissions` flag to bypass trust dialog since CAO already confirms workspace trust during `cao launch`
- Fix Claude Code `PROCESSING_PATTERN` not matching newer Claude Code 2.x spinner format: broaden pattern to match both `(esc to interrupt)` and `(Ns · ↓ tokens · thinking)` formats
- Fix tmux `paste-buffer -p` Enter key being swallowed by Claude Code 2.x TUI: add 0.3s delay between bracketed paste and Enter submission
- Fix Claude Code MCP servers not receiving `CAO_TERMINAL_ID` env var: inject it explicitly via `--mcp-config` env field since Claude Code doesn't forward parent shell env vars to MCP subprocesses

### Added

- Workspace trust confirmation prompt in `launch.py` before starting providers: asks "Do you trust all the actions in this folder?" since providers are granted full permissions (read, write, execute) in the working directory; supports `--yolo` flag to skip
- Provider documentation: `docs/claude-code.md` covering status detection, message extraction, configuration, implementation notes, and troubleshooting
- Working directory documentation: `docs/working-directory.md` covering `CAO_ENABLE_WORKING_DIRECTORY` feature

### Changed

- Bump `fastmcp` from 2.12.2 to 2.14.0
- Add `mcp>=1.23.0` as explicit dependency
- Relax `libtmux` pin from `==0.51.0` to `>=0.51.0`
- Change default provider from `q_cli` to `kiro_cli`
- Exclude e2e tests from default pytest runs (`-m 'not e2e'`)

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


