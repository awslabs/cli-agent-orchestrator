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

The E2E test suite validates handoff, assign, and send_message flows for every supported provider. To run the Cursor CLI E2E tests:

```bash
# Start CAO server
uv run cao-server

# Run all E2E tests filtered to the cursor_cli provider
uv run pytest -m e2e test/e2e/ -v -k cursor_cli -o "addopts="

# Run a specific flow
uv run pytest -m e2e test/e2e/test_handoff.py -v -k cursor_cli -o "addopts="
```

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

## References

- [Cursor CLI Overview](https://cursor.com/docs/cli/overview)
- [Cursor CLI Parameters](https://cursor.com/docs/cli/reference/parameters)
- [Issue #264: Add support for Cursor CLI as a provider](https://github.com/awslabs/cli-agent-orchestrator/issues/264)
