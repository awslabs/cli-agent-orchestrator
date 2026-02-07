---
name: build-cao-provider
description: Use when building a new CLI agent provider for CAO (e.g., Gemini CLI). Guides the full lifecycle — capturing real terminal output, implementing the provider, unit tests (>90% coverage), E2E tests across all providers, security scans, and updating 10+ documentation files. Encodes lessons learned from building Kimi CLI, Codex, and Claude Code providers.
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Task
argument-hint: "[provider-name]"
---

# Build a New CAO Provider

The provider name is `$ARGUMENTS`. If no argument provided, ask the user.

**Load references as needed** — do not read them all upfront:
- [Lessons Learned](./references/lessons-learned.md) — 8 critical bugs and their fixes (load during Phase 2)
- [Implementation Checklist](./references/implementation-checklist.md) — File-by-file creation guide (load during Phase 2)
- [Verification Checklist](./references/verification-checklist.md) — Testing, security, and documentation checks (load during Phase 5-6)

---

## Phase 1: Capture Real Terminal Output

**Do this FIRST.** Patterns from documentation are unreliable — only trust real captured output.

Capture at **multiple terminal sizes** (80x24 AND 150x46 minimum). TUI padding varies with height — this was a critical miss (see lessons #2).

For each size, capture 4 states: idle, processing, completed, error. These become test fixtures and the source of truth for regex patterns.

## Phase 2: Implement Provider

Load [implementation-checklist.md](./references/implementation-checklist.md) and [lessons-learned.md](./references/lessons-learned.md).

Follow `kimi_cli.py` as reference template. Two non-obvious requirements that caused production bugs:
1. **Forward `CAO_TERMINAL_ID` to MCP env** in `_build_command()` — without this, workers appear as separate tmux sessions invisible to the user (lessons #1)
2. **Set `IDLE_PROMPT_TAIL_LINES >= 50`** — smaller values fail on tall terminals even though E2E tests pass at 80x24 (lessons #2)

## Phase 3: Test

1. **Unit tests** — >90% coverage, include `test_get_status_idle_tall_terminal` from day one
2. **E2E** — handoff, assign, send_message for the new provider
3. **E2E all providers** — `uv run pytest -m e2e test/e2e/ -v` — verify no regressions
4. **Security** — `uv run pip-audit` or `trivy fs --severity HIGH,CRITICAL .`

## Phase 4: Code Quality

```bash
uv run black src/ test/ && uv run isort src/ test/ && uv run mypy src/
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py
```

## Phase 5: Documentation

Load [verification-checklist.md](./references/verification-checklist.md). A new provider touches 10+ files.

After updating all docs, audit every changed file for: no duplicated entries, no stale values (e.g., docs saying "bottom 10 lines" when code says 50), test counts match actual output, provider name consistent everywhere.

## Phase 6: Final Gate

Do NOT commit until every item in [verification-checklist.md](./references/verification-checklist.md) is checked.

---

## Quick Reference

| Provider | Prompt | Response Marker | MCP Config |
|----------|--------|-----------------|------------|
| kimi_cli | `user@dir💫/✨` | `•` bullet | `--mcp-config` JSON |
| claude_code | `>` or `❯` | `───` separator | `--mcp-config` JSON |
| codex | `›` (U+203A) | `•` bullet | `-c mcp_servers.*` TOML |
| kiro_cli | `%` + optional `λ` | Green arrow `❯` | Built-in |
| q_cli | `%` + optional `λ` | Green arrow `❯` | N/A |

```
src/cli_agent_orchestrator/
├── models/provider.py          # ProviderType enum — add new value
├── providers/
│   ├── base.py                 # BaseProvider abstract class
│   ├── manager.py              # Registration — add elif
│   ├── kimi_cli.py             # Best reference: TUI, thinking filter, MCP env
│   ├── claude_code.py          # Reference: trust prompt handling
│   └── codex.py                # Reference: TOML config, bullet format
├── cli/commands/launch.py      # PROVIDERS_REQUIRING_WORKSPACE_ACCESS
└── utils/terminal.py           # wait_for_shell, wait_until_status
```
