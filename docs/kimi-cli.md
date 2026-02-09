# Kimi CLI Provider

## Overview

The Kimi CLI provider enables CAO to work with [Kimi Code CLI](https://kimi.com/code), Moonshot AI's coding agent CLI tool. Kimi CLI runs as an interactive TUI using prompt_toolkit.

## Prerequisites

- **Kimi CLI**: Install via `brew install kimi-cli` or `uv tool install kimi-cli`
- **Authentication**: Run `kimi login` (OAuth-based)
- **tmux 3.3+**

Verify installation:

```bash
kimi --version
```

## Quick Start

```bash
# Authenticate
kimi login

# Launch with CAO
cao launch --agents code_supervisor --provider kimi_cli
```

## Status Detection

The provider detects Kimi CLI states by analyzing tmux terminal output:

| Status | Pattern | Description |
|--------|---------|-------------|
| **IDLE** | `username@dirname💫` or `username@dirname✨` at bottom | Prompt visible, ready for input |
| **PROCESSING** | No prompt at bottom | Response is streaming |
| **COMPLETED** | Prompt at bottom + latching flag (user input detected) | Task finished |
| **ERROR** | `Error:`, `APIError:`, `ConnectionError:` patterns | Error detected |

### Prompt Symbols

- **💫** (dizzy): Thinking mode enabled (default behavior)
- **✨** (sparkle): Thinking mode disabled (`--no-thinking` flag)

The provider matches both symbols using the pattern `\w+@[\w.-]+[✨💫]`.

## Message Extraction

Response extraction from terminal output:

1. Find the last user input box (bordered with `╭─` / `╰─`)
2. Collect all content between the box end and the next prompt
3. Filter out thinking bullets (gray ANSI-styled `•` lines)
4. Return the cleaned response text

### Thinking vs Response Bullets

Both thinking and response lines use the `•` (bullet) prefix. The provider distinguishes them using ANSI color codes in the raw terminal output:

- **Thinking**: `\x1b[38;5;244m•` (gray color 244 + italic)
- **Response**: Plain `•` without ANSI color prefix

## Agent Profiles

Agent profiles are **optional** for Kimi CLI. If provided, the provider:

1. Creates a temporary YAML agent file that extends Kimi's built-in `default` agent
2. Writes the system prompt as a separate markdown file
3. Passes the agent file via `--agent-file`

### Agent File Format

```yaml
version: 1
agent:
  extend: default
  system_prompt_path: ./system.md
```

Temp files are automatically cleaned up when the provider's `cleanup()` method is called.

## MCP Server Configuration

MCP servers from agent profiles are passed via `--mcp-config` as a JSON string:

```bash
kimi --yolo --mcp-config '{"server-name": {"command": "npx", "args": ["-y", "cao-mcp-server"]}}'
```

### MCP Tool Call Timeout

Kimi CLI defaults to a 60-second MCP tool call timeout (`tool_call_timeout_ms=60000` in `~/.kimi/config.toml`). This is too short for `handoff` operations, which create a worker terminal, wait for completion, and extract output — routinely exceeding 60 seconds.

The provider automatically modifies `~/.kimi/config.toml` to set `tool_call_timeout_ms=600000` when MCP servers are configured, increasing the timeout to 600 seconds (10 minutes) to match CAO's default handoff timeout. The original value is restored during `cleanup()`. This is the same direct-config-write pattern used by the Gemini CLI provider (`~/.gemini/settings.json`).

**Why not `--config` flag?** Kimi CLI's `--config` flag causes it to bypass the default config file (`~/.kimi/config.toml`), which breaks OAuth authentication — the CLI shows "model: not set" and `/login` refuses to work. Modifying the config file directly avoids this issue.

Without this override, the supervisor Kimi CLI agent receives a `ToolError("Timeout while calling MCP tool handoff")` after 60 seconds, even though the worker is still processing.

### CAO_TERMINAL_ID Forwarding

Kimi CLI does not automatically forward parent shell environment variables to MCP subprocesses. The provider explicitly injects `CAO_TERMINAL_ID` into the `env` field of each MCP server config so that tools like `handoff` and `assign` can create new agent windows in the same tmux session (instead of creating separate sessions). Existing `env` entries are preserved, and an existing `CAO_TERMINAL_ID` value is never overwritten.

## Command Flags

| Flag | Purpose |
|------|---------|
| `--yolo` | Auto-approve all tool action confirmations |
| `--agent-file FILE` | Custom agent YAML file |
| `--mcp-config TEXT` | MCP server configuration (JSON, repeatable) |
| `--work-dir DIR` | Set working directory |
| `--no-thinking` | Disable thinking mode (changes prompt to ✨) |

## Implementation Notes

### Provider Lifecycle

1. **Initialize**: Set MCP timeout in `~/.kimi/config.toml` (if MCP servers) → wait for shell → send `kimi --yolo` → wait for IDLE (up to 60s)
2. **Status Detection**: Check bottom 50 lines for idle prompt pattern (end-of-line anchored)
3. **Message Extraction**: Line-based approach mapping raw and clean output for thinking filtering
4. **Exit**: Send `/exit` command
5. **Cleanup**: Remove temp agent files, restore MCP timeout in config.toml, reset state

### Terminal Output Format

```
╭────────────────────────────────────────────────────────╮
│ Welcome to Kimi Code CLI!                              │
╰────────────────────────────────────────────────────────╯
user@project💫 create a function
╭────────────────────────────────────────────────────────╮
│ create a function                                      │
╰────────────────────────────────────────────────────────╯
• [thinking] Let me create the function...
• Here is the function:

def greet(name):
    return f"Hello, {name}!"

user@project💫
```

## E2E Testing

```bash
# Run all Kimi CLI E2E tests
uv run pytest -m e2e test/e2e/ -v -k kimi_cli

# Run specific test type
uv run pytest -m e2e test/e2e/test_handoff.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_assign.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_send_message.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k KimiCli -o "addopts="
```

Prerequisites for E2E tests:
- CAO server running (`cao-server`)
- `kimi` CLI authenticated (`kimi login`)
- Agent profiles installed (`cao install developer`)

## Troubleshooting

### Kimi CLI not detected

```bash
# Verify kimi is on PATH (command is `kimi`, not `kimi-cli`)
which kimi
kimi --version
```

### Authentication issues

```bash
# Re-authenticate
kimi login
```

### Initialization timeout

If Kimi CLI takes too long to start, check:
- Network connectivity (Kimi requires API access)
- Authentication status (`kimi login`)
- The provider waits up to 60 seconds for initialization

### Status bar not detected

The provider checks the bottom 50 lines for the idle prompt (`IDLE_PROMPT_TAIL_LINES = 50`). This accounts for Kimi's TUI padding lines between the prompt and the status bar, which varies with terminal height (e.g., a 46-row terminal has ~32 empty padding lines). If Kimi's TUI layout changes significantly, this constant may need adjustment.
