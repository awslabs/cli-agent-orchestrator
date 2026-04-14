---
name: cao-provider
description: Create a new CLI agent provider for CAO (CLI Agent Orchestrator). Use this skill whenever the user wants to add support for a new CLI-based AI agent (e.g., a new coding assistant CLI), integrate a new provider, or scaffold a provider implementation. Also use when the user asks about the provider architecture, what files to modify, or how providers work in CAO.
---

# CAO Provider Creator

Guide for creating a new CLI agent provider for CLI Agent Orchestrator. A "provider" is an adapter that lets CAO interact with a specific CLI-based AI agent through tmux.

## What You're Building

A provider translates between CAO's unified interface and a specific CLI tool's terminal output. It needs to:

1. **Launch** the CLI tool in a tmux window with the right flags
2. **Detect status** by parsing terminal output (IDLE, PROCESSING, COMPLETED, ERROR, WAITING_USER_ANSWER)
3. **Extract responses** from the terminal buffer after the agent finishes
4. **Clean up** when the terminal is deleted

## Before You Start

Gather this information about the target CLI:

- What command launches it? (e.g., `claude`, `kiro-cli chat`, `codex`)
- What does the idle prompt look like? (e.g., `> `, `❯ `, `ask a question`)
- What does the processing state look like? (e.g., spinner characters, "Thinking...")
- How are responses formatted? (e.g., preceded by `⏺`, inside a box, plain text)
- Does it support `--dangerously-skip-permissions` or similar flags?
- Does it have a REPL mode or is it single-shot?
- How does it handle MCP servers? (CLI flags, config file, agent JSON)
- Does it use alt-screen (full-screen TUI) or scrollback (inline output)? This fundamentally changes status detection logic — see lesson #16
- What's the exit command? (`/exit`, `/quit`, Ctrl+C)

## Step-by-Step Implementation

### Step 1: Add to ProviderType enum

File: `src/cli_agent_orchestrator/models/provider.py`

```python
class ProviderType(str, Enum):
    # ... existing providers ...
    NEW_CLI = "new_cli"
```

The value string is used everywhere — in API requests, database, config. Use snake_case.

### Step 2: Create the provider class

File: `src/cli_agent_orchestrator/providers/new_cli.py`

Read `references/provider-template.md` for the full annotated template. The key sections:

**Regex patterns** — Define at module level, not inside methods. You need patterns for:
- ANSI code stripping (reuse `r"\x1b\[[0-9;]*m"`)
- Idle prompt detection (what the prompt looks like when waiting for input)
- Processing detection (spinners, "Thinking...", progress indicators)
- Response markers (how agent responses start — e.g., `⏺` for Claude Code)
- Permission/confirmation prompts (if the CLI asks Y/n questions)

**Status detection priority** — The order in `get_status()` matters. Read `references/lessons-learnt.md` for the critical "stale buffer" lesson. The recommended pattern:

```
1. Strip ANSI codes from terminal output
2. Check WAITING_USER_ANSWER first (permission prompts need immediate attention)
3. Check COMPLETED (response marker + idle prompt both present in recent lines)
4. Check IDLE (just idle prompt, no response marker)
5. Check PROCESSING (spinner/thinking indicator in recent lines only)
6. Default to ERROR
```

**Message extraction** — Find the last response boundary in the terminal output and extract everything between it and the next prompt. Always strip ANSI codes from the final extracted text.

### Step 3: Register in ProviderManager

File: `src/cli_agent_orchestrator/providers/manager.py`

Add the import and elif branch:

```python
from cli_agent_orchestrator.providers.new_cli import NewCliProvider

# In create_provider():
elif provider_type == ProviderType.NEW_CLI.value:
    provider = NewCliProvider(
        terminal_id, tmux_session, tmux_window, agent_profile, allowed_tools
    )
```

### Step 4: Add to PROVIDERS_REQUIRING_WORKSPACE_ACCESS

File: `src/cli_agent_orchestrator/cli/commands/launch.py`

If the provider executes code or accesses the filesystem, add it:

```python
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {
    # ... existing ...
    "new_cli",
}
```

### Step 5: Tool restriction enforcement

There are three approaches depending on the CLI's capabilities. Read `docs/tool-restrictions.md` for full context.

**Hard enforcement via CLI flags** (e.g., Claude Code, Copilot CLI): Add the provider to `TOOL_MAPPING` in `src/cli_agent_orchestrator/utils/tool_mapping.py` to translate CAO vocabulary to native tool names.

**Hard enforcement via agent JSON** (e.g., Kiro CLI, Q CLI): The CLI reads `allowedTools` from the agent profile. No `TOOL_MAPPING` entry needed — CAO passes vocabulary directly.

**Soft enforcement via system prompt** (e.g., Kimi CLI, Codex): No native restriction mechanism. CAO prepends restriction instructions to the system prompt. No `TOOL_MAPPING` entry needed.

Only add a `TOOL_MAPPING` entry if the CLI has its own native tool names that differ from CAO's vocabulary.

### Step 6: Handle startup prompts

Many CLIs show cascading prompts on first launch (workspace trust, permission bypass, terms acceptance). Handle these in `initialize()` or a dedicated `_handle_startup_prompts()` method using a polling loop — not a single check. See `references/lessons-learnt.md` #17 for the stabilization loop pattern. Also consider shell warm-up (#14) and TERM variable compatibility (#15).

### Step 7: Write unit tests

File: `test/providers/test_new_cli_unit.py`

Read `references/test-guide.md` for the full test structure. Minimum coverage:

1. **Initialization** — successful start, shell timeout, CLI timeout, agent profiles
2. **Status detection** — IDLE, PROCESSING, COMPLETED, WAITING_USER_ANSWER, ERROR, empty output
3. **Message extraction** — successful extraction, edge cases, error handling
4. **Regex patterns** — verify each pattern matches expected terminal output
5. **Edge cases** — ANSI codes, Unicode, long outputs, multiple responses

Use `unittest.mock.patch` to mock `tmux_client`. Create fixture files in `test/providers/fixtures/`.

### Step 8: Write e2e tests

Add test classes to existing e2e test files and a fixture in `test/e2e/conftest.py`. Read `references/test-guide.md` for the full list of e2e test classes to add.

### Step 9: Validate with assign + handoff orchestration

This is the canonical multi-agent e2e test. It exercises assign (non-blocking), handoff (blocking), send_message (async inbox), and status detection under concurrent load. Use the `examples/assign/` profiles:

```bash
cao install examples/assign/data_analyst.md
cao install examples/assign/report_generator.md
cao install examples/assign/analysis_supervisor.md
cao launch --agents analysis_supervisor --provider new_cli --auto-approve
```

**Test flow:** Supervisor assigns 3x data_analyst workers in parallel + handoff 1x report_generator (blocking) → analysts send_message results back to supervisor → supervisor combines template + results into final report.

If any step fails, investigate:
- **Assign fails:** Status detection not recognizing IDLE after analyst finishes, or per-directory lock conflict (see lesson #19)
- **Handoff times out:** COMPLETED not detected — check stale buffer (lesson #1) or alt-screen detection (lesson #16)
- **send_message not delivered:** Supervisor not reaching IDLE state, blocking message delivery — check startup prompt loop (lesson #17)
- **Concurrent failures:** Race conditions in shared config files (lesson #19) or TERM env issues (lesson #15)

See `test/e2e/test_assign.py` for the automated version. Reference: https://github.com/awslabs/cli-agent-orchestrator/tree/feature/kimi-cli/examples/assign

### Step 10: Documentation

Create `docs/new-cli.md` with prerequisites, launch examples, agent profile format, known limitations, and troubleshooting. Update `README.md` provider table and `CHANGELOG.md`.

## File Checklist

When your provider is complete, verify you've touched all these files:

- [ ] `src/cli_agent_orchestrator/models/provider.py` — ProviderType enum
- [ ] `src/cli_agent_orchestrator/providers/new_cli.py` — Provider class
- [ ] `src/cli_agent_orchestrator/providers/manager.py` — Import + elif branch
- [ ] `src/cli_agent_orchestrator/cli/commands/launch.py` — PROVIDERS_REQUIRING_WORKSPACE_ACCESS
- [ ] `src/cli_agent_orchestrator/utils/tool_mapping.py` — TOOL_MAPPING (only if CLI needs translation)
- [ ] `test/providers/test_new_cli_unit.py` — Unit tests
- [ ] `test/providers/fixtures/new_cli_*.txt` — Test fixtures
- [ ] `test/e2e/conftest.py` — require_new_cli fixture
- [ ] `test/e2e/test_*.py` — E2E test classes
- [ ] `docs/new-cli.md` — Provider documentation
- [ ] `README.md` — Provider table
- [ ] `CHANGELOG.md` — New provider entry
