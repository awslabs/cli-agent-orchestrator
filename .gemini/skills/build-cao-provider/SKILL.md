---
name: build-cao-provider
description: Use when building a new CLI agent provider for CAO (e.g., Gemini CLI). Guides the full lifecycle — capturing real terminal output, implementing the provider, unit tests (90%+ coverage), E2E tests across all providers, security scans, and updating 10+ documentation files. Encodes lessons learned from building Kimi CLI, Codex, and Claude Code providers.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Task
---

# Build a New CAO Provider

The provider name is `$ARGUMENTS`. If no argument provided, ask the user.

**Load references as needed** — do not read them all upfront:
- [Lessons Learned](./references/lessons-learned.md) — 9 critical bugs and their fixes (load during Phase 2)
- [Implementation Checklist](./references/implementation-checklist.md) — File-by-file creation guide (load during Phase 2)
- [Verification Checklist](./references/verification-checklist.md) — Testing, security, and documentation checks (load during Phase 6-7)

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

## Phase 5: Ralph Autonomous Verification (Optional but Highly Recommended)

> **Ralph** ([github.com/frankbria/ralph-claude-code](https://github.com/frankbria/ralph-claude-code)) is an autonomous AI development loop that runs Claude Code iteratively with intelligent exit detection. It catches bugs that manual review misses — in the Gemini CLI provider work, Ralph found 3 real bugs in its first loop alone (see lessons #9).

**Important:** Ralph must be run from a **real terminal**, NOT from within Claude Code.

### Setup

Pre-made templates are in `templates/ralph/` — copy them, then customize with your provider name.

```bash
# 1. Initialize Ralph (skip if .ralph/ already exists)
ralph-enable-ci --project-type python

# 2. Copy templates (replace <provider> placeholders after copying)
cp .claude/skills/build-cao-provider/templates/ralph/PROMPT.md .ralph/PROMPT.md
cp .claude/skills/build-cao-provider/templates/ralph/fix_plan.md .ralph/fix_plan.md
cp .claude/skills/build-cao-provider/templates/ralph/AGENT.md .ralph/AGENT.md
cp .claude/skills/build-cao-provider/templates/ralph/ralphrc .ralphrc

# 3. Copy skill specs so Ralph can verify against them
cp .claude/skills/build-cao-provider/references/*.md .ralph/specs/

# 4. Replace placeholders in all .ralph/ files:
#    <provider>       → e.g., gemini_cli
#    <Provider>       → e.g., Gemini CLI
#    <provider-kebab> → e.g., gemini-cli
#    <PROVIDER>       → e.g., GEMINI_CLI
```

The templates include:
- **PROMPT.md** — Directs Ralph to verify (not build), references specs/, includes RALPH_STATUS block
- **fix_plan.md** — Pre-filled with 8 verification tasks (doc consistency, security, cross-provider comparison)
- **AGENT.md** — Build/test/run commands using `uv run`
- **ralphrc** — Project config with python tools, 15min timeout, circuit breaker thresholds

### Run Ralph

```bash
# From a REAL terminal (not Claude Code), in the project root:

# Option 1: Tmux monitor — 3-pane view (loop, live output, status dashboard)
ralph --monitor

# Option 2: Headless — for CI or non-tmux environments
ralph --live

# Option 3: Quick single pass
ralph --max-loops 3
```

### What Ralph Typically Catches

Based on the Gemini CLI provider experience (3 loops):

| Loop | Findings |
|------|----------|
| 1 | Real bugs: wrong package names, wrong Unicode character names in comments, stale test counts in docs |
| 2 | Code quality: verifies comments and security patterns are clean |
| 3 | Cross-provider: checks all lessons-learned against the provider, finds missed patterns |

Ralph's circuit breaker stops automatically after 3 loops with no progress or 5 loops with the same error.

## Phase 6: Documentation

Load [verification-checklist.md](./references/verification-checklist.md). A new provider touches 10+ files.

After updating all docs, audit every changed file for: no duplicated entries, no stale values (e.g., docs saying "bottom 10 lines" when code says 50), test counts match actual output, provider name consistent everywhere.

## Phase 7: Final Gate

Do NOT commit until every item in [verification-checklist.md](./references/verification-checklist.md) is checked.

---

## Quick Reference

| Provider | Prompt | Response Marker | MCP Config |
|----------|--------|-----------------|------------|
| kimi_cli | `user@dir💫/✨` | `•` bullet | `--mcp-config` JSON |
| claude_code | `>` or `❯` | `───` separator | `--mcp-config` JSON |
| codex | `›` (U+203A) | `•` bullet | `-c mcp_servers.*` TOML |
| gemini_cli | `*   Type your message` | `✦` (U+2726) | `gemini mcp add` pre-launch |
| kiro_cli | `%` + optional `λ` | Green arrow `❯` | Built-in |
| q_cli | `%` + optional `λ` | Green arrow `❯` | N/A |

```
src/cli_agent_orchestrator/
├── models/provider.py          # ProviderType enum — add new value
├── providers/
│   ├── base.py                 # BaseProvider abstract class
│   ├── manager.py              # Registration — add elif
│   ├── kimi_cli.py             # Best reference: TUI, thinking filter, MCP env
│   ├── gemini_cli.py           # Reference: Ink TUI, pre-launch MCP, shell env
│   ├── claude_code.py          # Reference: trust prompt handling
│   └── codex.py                # Reference: TOML config, bullet format
├── cli/commands/launch.py      # PROVIDERS_REQUIRING_WORKSPACE_ACCESS
└── utils/terminal.py           # wait_for_shell, wait_until_status
```
