# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
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


