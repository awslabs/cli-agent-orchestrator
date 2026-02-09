# Ralph Fix Plan - <Provider> Provider Verification

## BLOCKING — Real E2E Tests (run FIRST, before anything else)

These require a running cao-server. Start it, run E2E, stop it. If any E2E test fails, fix the provider before proceeding to other checks.

- [ ] Start cao-server: `uv run cao-server &` and verify: `curl -s http://localhost:9889/health`
- [ ] Run E2E handoff for new provider: `uv run pytest -m e2e test/e2e/test_handoff.py -v -k <Provider> --tb=long`
- [ ] Run E2E assign for new provider: `uv run pytest -m e2e test/e2e/test_assign.py -v -k <Provider> --tb=long`
- [ ] Run E2E send_message for new provider: `uv run pytest -m e2e test/e2e/test_send_message.py -v -k <Provider> --tb=long`
- [ ] Stop cao-server: `kill $(pgrep -f cao-server)`
- [ ] All 5 E2E tests pass for <provider> (2 handoff + 2 assign + 1 send_message)

## High Priority
- [ ] Verify all documentation consistency (IDLE_PROMPT_TAIL_LINES value matches code in docs/<provider-kebab>.md, test counts match actual pytest output, no stale values)
- [ ] Review <provider>.py code comments for completeness per verification checklist
- [ ] Security audit: verify shlex.join() used for ALL CLI argument building in <provider>.py (no f-string interpolation), no secrets, temp file cleanup

## Medium Priority
- [ ] Review <provider>.py against kimi_cli.py for any missed patterns (cross-check all lessons-learnt against the new provider)
- [ ] Verify <provider>.py forwards CAO_TERMINAL_ID to MCP subprocesses (if applicable)
- [ ] Verify <provider>.py exit_cli() return value works correctly through tmux send_keys (text commands sent literally, key sequences like C-d sent non-literally)
- [ ] Check that CODEBASE.md architecture diagram formatting is correct (no broken ASCII art)
- [ ] Verify .codex/skills/, .gemini/skills/, .kimi/skills/ contain correct copies of build-cao-provider skill

## Low Priority
- [ ] Review test fixture files for realism (do they match actual <Provider> CLI output at multiple terminal sizes?)
- [ ] Test <provider>.py regex patterns programmatically against its fixture files (verify patterns match expected fixtures)
- [ ] Check if any edge cases in <provider>.py status detection are untested (false ERROR from response mentioning "Error:", multi-turn scrollback with old responses)
- [ ] Verify every numeric claim in docs matches actual counts (run pytest --collect-only, count test methods, check all doc files)

## Pre-Requisites (verify these were completed correctly)
<!-- These should be done in Phases 1-4 BEFORE running Ralph.
     Ralph's job is to VERIFY correctness, not build.
     Mark as checked after verifying each one is correct. -->
- [ ] Provider enum added (<PROVIDER> = "<provider>")
- [ ] Provider implementation (<provider>.py)
- [ ] Registration in manager.py and launch.py
- [ ] Unit tests (100% coverage target)
- [ ] Test fixture files
- [ ] E2E test classes (handoff, assign, send_message)
- [ ] CI workflow (test-<provider>-provider.yml)
- [ ] Provider documentation (docs/<provider-kebab>.md)
- [ ] README.md updated (provider table + launch example)
- [ ] CHANGELOG.md updated
- [ ] CODEBASE.md updated (architecture diagram + directory structure)
- [ ] DEVELOPMENT.md updated (test count, commands, prerequisites, structure, docs)
- [ ] docs/api.md updated
- [ ] test/providers/README.md updated (full test section + CI table + metrics)
- [ ] Architecture mmd + PNG re-rendered
- [ ] base.py docstring updated
- [ ] E2E conftest.py require_<provider> fixture added
- [ ] Full test suite passing
- [ ] black, isort, mypy clean
- [ ] pip-audit clean

## Notes
- Replace <provider> with the actual provider name (e.g., gemini_cli)
- Replace <Provider> with the display name (e.g., Gemini CLI)
- Replace <provider-kebab> with the kebab-case name (e.g., gemini-cli)
- Add provider-specific notes here (TUI type, MCP mechanism, exit command, etc.)

## Learnings
<!-- Ralph will append learnings from each loop here -->

