# Gemini CLI Provider

## Overview

The Gemini CLI provider enables CAO to work with [Gemini CLI](https://github.com/google-gemini/gemini-cli), Google's coding agent CLI tool. Gemini CLI runs as an interactive Ink-based TUI (not alternate screen mode) that keeps scrollback history in tmux.

## Prerequisites

- **Gemini CLI**: Install via `npm install -g @google/gemini-cli` or `npx @google/gemini-cli`
- **Authentication**: Run `gemini` and follow the OAuth flow, or set `GEMINI_API_KEY`
- **tmux 3.3+**

Verify installation:

```bash
gemini --version
```

## Quick Start

```bash
# Launch with CAO
cao launch --agents code_supervisor --provider gemini_cli
```

## Status Detection

The provider detects Gemini CLI states by analyzing tmux terminal output:

| Status | Pattern | Description |
|--------|---------|-------------|
| **IDLE** | `*   Type your message` at bottom | Input box visible, ready for input |
| **PROCESSING** | No idle prompt at bottom | Response is streaming |
| **COMPLETED** | Idle prompt + user query (`>` prefix) + response (`✦` prefix) | Task finished |
| **ERROR** | `Error:`, `APIError:`, `ConnectionError:`, `Traceback` patterns | Error detected |

### Input Box Structure

Gemini CLI uses an Ink-based input box with block character borders:

```
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
 *   Type your message or @path/to/file
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
```

## Message Extraction

Response extraction from terminal output:

1. Find the last user query (line with `>` prefix inside query box)
2. Collect all content between the query and the next idle prompt
3. Filter out TUI chrome: input box borders (`▀▄`), status bar, YOLO indicator, model indicator
4. Return the cleaned response text

### Response Format

Gemini CLI uses the `✦` (U+2726, four-pointed star) prefix for assistant responses:

```
✦ Here is the implementation:

def greet(name):
    return f"Hello, {name}!"
```

Tool calls appear in rounded-corner boxes:

```
╭──────────────────────────────╮
│ ✓  ReadFile test.txt          │
╰──────────────────────────────╯
```

## Agent Profiles

Agent profiles are **optional** for Gemini CLI. When an agent profile is provided:

1. **System prompt**: Injected via two mechanisms:
   - **Primary**: The `-i` (prompt-interactive) flag sends the system prompt as the first user message. Gemini strongly adopts the role from `-i`, making it effective for supervisor orchestration.
   - **Supplementary**: Written to a `GEMINI.md` file in the working directory for persistent project-level context. If an existing `GEMINI.md` is present, it is backed up to `GEMINI.md.cao_backup` and restored during cleanup.

   Note: `GEMINI.md` alone is insufficient — the model treats it as weak background context and does not adopt supervisor roles. The `-i` flag is required for reliable system prompt injection.
2. **MCP servers**: Registered via `gemini mcp add` before launching (see below).

## MCP Server Configuration

MCP servers from agent profiles are registered using `gemini mcp add --scope user` commands chained before the main `gemini` command:

```bash
gemini mcp add cao-mcp-server --scope user -e CAO_TERMINAL_ID=abc12345 npx -y cao-mcp-server && \
gemini --yolo --sandbox false -i "You are the analysis_supervisor..."
```

The `--scope user` flag writes MCP config to user-level settings instead of project-level settings. This is required because `gemini mcp add` refuses to write project-level settings in the home directory (returns "Please use --scope user to edit settings in the home directory").

### CAO_TERMINAL_ID Forwarding

Gemini CLI forwards `CAO_TERMINAL_ID` to MCP subprocesses via the `-e` flag on `gemini mcp add`, which sets environment variables in the MCP server process. This ensures tools like `handoff` and `assign` create new agent windows in the same tmux session.

### MCP Server Cleanup

When the provider's `cleanup()` method is called, it sends `gemini mcp remove --scope user <name>` for each MCP server that was added during initialization.

## Command Flags

| Flag | Purpose |
|------|---------|
| `--yolo` | Auto-approve all tool action confirmations |
| `--sandbox false` | Disable sandbox mode (required for file system access) |

## Implementation Notes

### Provider Lifecycle

1. **Initialize**: Wait for shell → warm-up echo (verify shell ready) → send command → wait for IDLE (up to 60s)
2. **Status Detection**: Check bottom 50 lines for idle prompt pattern (`IDLE_PROMPT_TAIL_LINES = 50`)
3. **Message Extraction**: Line-based approach filtering TUI chrome
4. **Exit**: Send `C-d` (Ctrl+D)
5. **Cleanup**: Remove MCP servers, reset state

### Terminal Output Format

```
 ███ GEMINI BANNER
                                                  YOLO mode (ctrl + y to toggle)
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
 > say hello
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  Responding with gemini-3-flash-preview
✦ Hello! How can I help you today?

▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
 *   Type your message or @path/to/file
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
 .../project (main)   no sandbox   Auto (Gemini 3) /model | 199.2 MB
```

### IDLE_PROMPT_TAIL_LINES

Set to 50. Gemini's Ink-based TUI can add padding lines between the input box and the status bar at the bottom. On tall terminals (e.g., 150x46), the prompt may be far from the last line. 50 lines covers terminals up to ~60 rows.

## E2E Testing

```bash
# Run all Gemini CLI E2E tests
uv run pytest test/e2e/ -v -k Gemini -o "addopts="

# Run specific test type
uv run pytest test/e2e/test_handoff.py -v -k Gemini -o "addopts="
uv run pytest test/e2e/test_assign.py -v -k Gemini -o "addopts="
uv run pytest test/e2e/test_send_message.py -v -k Gemini -o "addopts="
uv run pytest test/e2e/test_supervisor_orchestration.py -v -k Gemini -o "addopts="
```

Prerequisites for E2E tests:
- CAO server running (`cao-server`)
- `gemini` CLI authenticated
- Agent profiles installed (`cao install developer`, `cao install examples/assign/analysis_supervisor.md`)

## Troubleshooting

### Gemini CLI not detected

```bash
# Verify gemini is on PATH
which gemini
gemini --version
```

### Initialization timeout

If Gemini CLI takes too long to start, check:
- Network connectivity (Gemini requires API access)
- Authentication status (re-run `gemini` to authenticate)
- MCP server registration: `gemini mcp add` needs `--scope user` when the working directory is the home directory; without it the command fails silently and `gemini` never launches
- Shell environment: the provider sends a warm-up `echo` command and waits for the marker before launching `gemini`, ensuring PATH/nvm/homebrew are loaded
- The provider waits up to 60 seconds for initialization

### Status detection not working on tall terminals

The provider checks the bottom 50 lines for the idle prompt (`IDLE_PROMPT_TAIL_LINES = 50`). This accounts for Gemini's Ink TUI padding lines between the input box and the status bar, which varies with terminal height. If Gemini's TUI layout changes significantly, this constant may need adjustment.
