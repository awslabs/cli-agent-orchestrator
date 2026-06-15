# Cursor CLI Provider

## Overview

The Cursor CLI provider enables CLI Agent Orchestrator (CAO) to work with the **[Cursor CLI](https://cursor.com/cli)** (primary command: `agent`, historical alias: `cursor-agent`) — Anysphere's terminal-native AI coding assistant. Use it to drive Cursor alongside Claude Code, Kiro CLI, and the other providers already supported by CAO.

The provider implements the [BaseProvider](https://github.com/awslabs/cli-agent-orchestrator) interface, so it inherits support for handoff, assign, and send_message orchestration flows.

## Quick Start

### Prerequisites

1. **Cursor subscription or API key** — required by `agent login`.
2. **Cursor CLI** — install the `agent` (or legacy `cursor-agent`) binary on your `$PATH`.
3. **tmux** — required for terminal management.

```bash
# Install Cursor CLI (see https://cursor.com/cli for the current method)
curl https://cursor.com/install -fsS | bash

# Authenticate
agent login
```

### Using the Cursor CLI Provider with CAO

```bash
# Start the CAO server
cao-server

# Launch a Cursor-backed session
cao launch --agents developer --provider cursor_cli
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=cursor_cli&agent_profile=developer"
```

## Features

### Status Detection

The Cursor CLI provider detects terminal states by analyzing output patterns:

- **IDLE / COMPLETED**: Terminal shows a `❯` (or `>`) REPL prompt, ready for input.
- **PROCESSING**: Spinner characters (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳·`) with ellipsis on a line immediately before the `──────────────────────` separator.
- **WAITING_USER_ANSWER**: TUI selection widget (mode picker, model picker) showing the `↑/↓ to navigate` footer, or an active workspace-trust / tool-permission dialog.
- **UNKNOWN**: No recognizable state.

Status detection checks patterns in priority order: PROCESSING → WAITING_USER_ANSWER → COMPLETED → UNKNOWN.

The PROCESSING check is **structural** — it walks backwards from the last separator looking for a spinner line, so stale spinner text from a previously completed turn does not trigger a false positive (the same approach used by the Claude Code provider).

### Message Extraction

Cursor CLI does not emit a single canonical response marker (unlike Claude Code's `⏺`), so the provider uses the structural **separator + trailing prompt** pattern:

1. Find the last `──────────────────────` separator that precedes a trailing `❯` idle prompt.
2. Find the separator before that one (or the start of the buffer).
3. Extract the content between them and strip ANSI codes.

If no boundary is detected, extraction raises `ValueError("No Cursor CLI response found - no separator / idle prompt boundary detected")`.

### Permission Bypass

By default, CAO launches Cursor CLI with the following flags to skip the interactive dialogs that would otherwise block headless orchestration:

- `--force` — auto-approves every tool call (Bash, file writes, etc.).
- `--trust` — accepts the per-directory workspace trust dialog on first run.
- `--approve-mcps` — pre-approves MCP servers declared on the command line.

These are safe to set because CAO already confirms workspace trust during `cao launch` ("Do you trust all the actions in this folder?") or via `--yolo`. Without them, every worker agent spawned via handoff/assign would block on a trust/permission dialog with no way to accept it interactively.

## Configuration

### Agent Profile Integration

When launched with an agent profile (e.g., `--agents code_supervisor`), CAO:

1. Loads the profile from the agent store (`~/.aws/cli-agent-orchestrator/agent-store`).
2. Extracts the system prompt from the Markdown content.
3. Passes it via `--system-prompt` (newlines escaped to `\n` for tmux compatibility).
4. Injects MCP servers via `--mcp <json>` if the profile defines `mcpServers`.
5. Forwards the `CAO_TERMINAL_ID` env var to each MCP server so they can identify the current terminal for handoff/assign operations.
6. Honors the profile's `model` field by passing `--model <id>` at launch (overridable via the constructor).

### Launch Command

The provider builds the command via `_build_cursor_command()`:

```
agent --force --trust [--model <id>] [--agent <name>] [--system-prompt "..."] [--mcp "{...}" --approve-mcps]
```

The `--print` flag is intentionally **not** passed: CAO drives the interactive REPL so the inbox service can stream follow-up prompts via MCP handoff. Print mode is a one-shot CLI flag that exits after the first response and is therefore incompatible with multi-turn CAO sessions.

### Model Override

The provider forwards a model selection in the following order of precedence:

1. The profile's `model` field (when set on the agent profile).
2. The constructor-provided `model` argument (e.g., from `cao launch --model gpt-5`).
3. No `--model` flag (Cursor uses the user's default model).

## Tool Restrictions

Cursor CLI does not yet expose a `--disallowedTools` (or equivalent) flag for hard tool enforcement, so this provider falls back to **soft enforcement via the system prompt** (see `docs/tool-restrictions.md`):

- When the operator sets an explicit non-wildcard allowlist (`--allowed-tools fs_read,fs_list` or a restricted `role`), the provider prepends the shared `SECURITY_PROMPT` plus a tool list to the agent's system prompt. This is **advisory only** — Cursor may still call any tool. See `skills/cao-provider/references/lessons-learnt.md` #13 for the three enforcement approaches.
- When the operator uses `--yolo` (`allowed_tools=["*"]`) or omits restrictions entirely, no security prompt is added.

If you need strict hard enforcement, prefer a provider that supports `--disallowedTools` (Claude Code, Copilot CLI, Gemini CLI) or the OpenCode CLI frontmatter mechanism.

## End-to-End Testing

The E2E test suite validates the full orchestration matrix (handoff, assign, send_message, allowedTools, supervisor orchestration) for every supported provider. The 11 core e2e tests for Cursor CLI are added under the `TestCursorCli*` test classes in `test/e2e/` and follow the same `_run_*_test()` helpers used by the other providers.

### Prerequisites

1. **Cursor CLI** (`agent` or `cursor-agent`) installed and authenticated.
2. **CAO server** running (`uv run cao-server`).
3. **Agent profiles** installed for the cursor_cli provider (the profiles shipped in `examples/assign/` are provider-agnostic; you can pin them to `cursor_cli` either at install time or via frontmatter `provider: cursor_cli`):

   ```bash
   cao install examples/assign/data_analyst.md --provider cursor_cli
   cao install examples/assign/report_generator.md --provider cursor_cli
   cao install developer --provider cursor_cli  # for handoff / send_message tests
   ```

4. **tmux** available on `$PATH`.

### Running Cursor CLI E2E Tests

The default pytest `addopts` excludes the `e2e` marker, so the `-o "addopts="` override is required to enable them:

```bash
# Start CAO server
uv run cao-server

# All Cursor CLI e2e tests
uv run pytest -m e2e test/e2e/ -v -k cursor_cli -o "addopts="

# Individual flow files
uv run pytest -m e2e test/e2e/test_handoff.py -v -k cursor_cli -o "addopts="
uv run pytest -m e2e test/e2e/test_assign.py -v -k cursor_cli -o "addopts="
uv run pytest -m e2e test/e2e/test_send_message.py -v -k cursor_cli -o "addopts="
uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -k cursor_cli -o "addopts="
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k cursor_cli -o "addopts="
```

### The 11 Core E2E Tests

| # | Test class | What it validates |
|---|------------|-------------------|
| 1 | `TestCursorCliHandoff::test_handoff_simple_function` | Worker creates a Python function, returns extractable output |
| 2 | `TestCursorCliHandoff::test_handoff_second_task` | Same terminal handles a second task with no state leakage |
| 3 | `TestCursorCliAssign::test_assign_data_analyst` | `data_analyst` profile produces statistical analysis on a dataset |
| 4 | `TestCursorCliAssign::test_assign_report_generator` | `report_generator` profile creates a structured report template |
| 5 | `TestCursorCliAssign::test_assign_with_callback` | Worker completes → inbox callback → supervisor receives result |
| 6 | `TestCursorCliSendMessage::test_send_message_to_inbox` | One terminal sends a message to another's inbox; delivery verified |
| 7 | `TestCursorCliAllowedTools::test_restricted_supervisor_cannot_bash` | **Marked `xfail`** — Cursor CLI lacks a native `--disallowedTools` flag; soft enforcement via `SECURITY_PROMPT` is advisory only. Tracked under "Tool Restrictions" above. |
| 8 | `TestCursorCliAllowedTools::test_unrestricted_developer_can_bash` | Developer with `--yolo` (allowedTools=`["*"]`) can execute bash |
| 9 | `TestCursorCliAllowedTools::test_allowed_tools_stored_in_metadata` | `allowedTools` is persisted and returned by `GET /terminals/{id}` |
| 10 | `TestCursorCliSupervisorOrchestration::test_supervisor_handoff` | Supervisor agent autonomously calls the `handoff()` MCP tool to delegate to `report_generator` |
| 11 | `TestCursorCliSupervisorOrchestration::test_supervisor_assign_three_analysts` | **The canonical `examples/assign/` smoke test.** Supervisor parallel-assigns 3x data analysts, sequential-handoffs the report generator, receives all 3 inbox callbacks, and finalizes the report without doing the analysis work itself. The supervisor must NOT complete the jobs itself — the test asserts the final output references delegated results. |

### Manual `examples/assign/` Smoke Test

For a quick interactive validation outside the pytest harness:

```bash
cao install examples/assign/analysis_supervisor.md --provider cursor_cli
cao install examples/assign/data_analyst.md --provider cursor_cli
cao install examples/assign/report_generator.md --provider cursor_cli

cao launch --agents analysis_supervisor --provider cursor_cli
```

Then in the supervisor terminal, paste the example task from `examples/assign/README.md` (3 datasets, calculate mean/median/stdev, generate a report). The supervisor should:

1. Use `assign()` to dispatch 3x data analysts in parallel
2. Use `handoff()` to get a report template from the report generator
3. Finish its turn (no sleep/echo loops — they block inbox delivery)
4. Receive all 3 inbox callbacks and combine template + results into the final report

If the supervisor completes the analysis work itself, the per-directory lock or status detection is broken — see `skills/cao-provider/references/lessons-learnt.md` #19 (per-directory locks) and #16 (alt-screen detection).

## Troubleshooting

### Common Issues

1. **Trust Dialog Blocking**
   - The provider launches Cursor CLI with `--trust` automatically.
   - If the dialog still appears, verify the `agent` version supports `--trust` (`agent --help`).

2. **MCP Approval Dialog Blocking**
   - The provider launches with `--approve-mcps` when the profile declares `mcpServers`.
   - If MCP servers still prompt, check the `--mcp` JSON syntax in the launch command.

3. **Authentication Issues**
   ```bash
   agent login
   # Or set CURSOR_API_KEY environment variable
   ```

4. **Status Stuck on UNKNOWN**
   - Attach to the tmux session (`tmux attach -t <session-name>`) and check terminal output.
   - Verify Cursor CLI starts correctly in a regular terminal first: `agent "hello"`.

5. **`agent` Not Found on `$PATH`**
   - The provider does not prefix the command with an absolute path — install the binary where your shell can find it.
   - On Linux, the recommended install method is `curl https://cursor.com/install -fsS | bash`.

6. **E2E tests skip with "Cursor CLI (agent / cursor-agent) not installed"**
   - Install Cursor CLI and ensure the `agent` (or legacy `cursor-agent`) binary is on `$PATH`.
   - The `require_cursor` fixture auto-skips when the binary is absent; no failure, just no coverage.

## References

- [Cursor CLI Overview](https://cursor.com/docs/cli/overview)
- [Cursor CLI Parameters](https://cursor.com/docs/cli/reference/parameters)
- [Issue #264: Add support for Cursor CLI as a provider](https://github.com/awslabs/cli-agent-orchestrator/issues/264)
