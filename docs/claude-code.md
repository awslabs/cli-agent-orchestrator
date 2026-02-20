# Claude Code Provider

## Overview

The Claude Code provider enables CLI Agent Orchestrator (CAO) to work with **Claude Code** (Anthropic's CLI) through your Anthropic API key or Claude subscription, allowing you to orchestrate multiple Claude-based agents.

## Quick Start

### Prerequisites

1. **Anthropic API Key** or **Claude Subscription**: Authentication for Claude Code
2. **Claude Code CLI**: Install the CLI tool
3. **tmux**: Required for terminal management

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Authenticate
claude setup-token
```

### Using Claude Code Provider with CAO

```bash
# Start the CAO server
cao-server

# Launch a Claude Code-backed session
cao launch --agents developer --provider claude_code
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=claude_code&agent_profile=developer"
```

## Features

### Status Detection

The Claude Code provider detects terminal states by analyzing output patterns:

- **IDLE**: Terminal shows `>` or `❯` prompt, ready for input
- **PROCESSING**: Spinner characters visible (`✶`, `✢`, `✽`, `✻`, `·`, `✳`) with ellipsis and status text
- **WAITING_USER_ANSWER**: Claude showing numbered selection options with `❯` cursor
- **COMPLETED**: Response marker `⏺` present + idle prompt visible
- **ERROR**: No recognizable output state

Status detection checks patterns in priority order: PROCESSING → WAITING_USER_ANSWER → COMPLETED → IDLE → ERROR.

### Message Extraction

The provider extracts the last assistant response by finding the `⏺` response marker:

1. Find all `⏺` markers in the output
2. Take the last one (final response)
3. Extract text until the next `>` prompt or separator line (`────────`)
4. Strip ANSI codes from the result

### Permission Bypass

CAO launches Claude Code with `--dangerously-skip-permissions` to bypass:
- **Workspace trust dialog**: The "Yes, I trust this folder" prompt that appears for new directories
- **Tool permission prompts**: Approval dialogs for file edits, command execution, etc.

This is safe because CAO already confirms workspace trust during `cao launch` ("Do you trust all the actions in this folder?") or via `--yolo` flag. Without this flag, worker agents spawned via handoff/assign would block on the trust dialog with no way to accept it interactively.

A fallback `_handle_trust_prompt()` method also monitors for the trust dialog and sends Enter to accept it, in case the flag doesn't cover all scenarios.

## Configuration

### Agent Profile Integration

When launched with an agent profile (e.g., `--agents code_supervisor`), CAO:

1. Loads the profile from the agent store
2. Extracts the system prompt from the Markdown content
3. Passes it via `--append-system-prompt` (newlines escaped to `\n` for tmux compatibility)
4. Injects MCP servers via `--mcp-config` JSON if the profile defines `mcpServers`

### Launch Command

The provider builds the command via `_build_claude_command()`:

```
claude --dangerously-skip-permissions [--append-system-prompt "..."] [--mcp-config "..."]
```

## Implementation Notes

- **Prompt patterns**: `IDLE_PROMPT_PATTERN` matches both old `>` and new `❯` prompt styles, including non-breaking space (`\xa0`)
- **ANSI handling**: All pattern matching strips ANSI codes first via `ANSI_CODE_PATTERN`
- **Processing detection**: `PROCESSING_PATTERN` matches both old format (`✽ Cooking… (esc to interrupt)`) and new Claude Code 2.x format (`✽ Cooking… (6s · ↓ 174 tokens · thinking)`)
- **Trust prompt exclusion**: `TRUST_PROMPT_PATTERN` ("Yes, I trust this folder") is excluded from `WAITING_USER_ANSWER` detection to avoid false positives during initialization
- **Shell escaping**: Uses `shlex.join()` for safe command construction with multiline prompts
- **Exit command**: `/exit` via `POST /terminals/{terminal_id}/exit`

### Status Values

- `TerminalStatus.IDLE`: Ready for input
- `TerminalStatus.PROCESSING`: Working on task
- `TerminalStatus.WAITING_USER_ANSWER`: Waiting for user input
- `TerminalStatus.COMPLETED`: Task finished
- `TerminalStatus.ERROR`: Error occurred

## End-to-End Testing

The E2E test suite validates handoff, assign, and send_message flows for Claude Code.

### Running Claude Code E2E Tests

```bash
# Start CAO server
uv run cao-server

# Run all Claude Code E2E tests
uv run pytest -m e2e test/e2e/ -v -k claude_code

# Run specific test types
uv run pytest -m e2e test/e2e/test_handoff.py -v -k claude_code
uv run pytest -m e2e test/e2e/test_assign.py -v -k claude_code
uv run pytest -m e2e test/e2e/test_send_message.py -v -k claude_code
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k ClaudeCode -o "addopts="
```

## Troubleshooting

### Common Issues

1. **Trust Dialog Blocking**:
   - Claude Code should launch with `--dangerously-skip-permissions` automatically
   - If the trust dialog still appears, check that the provider code includes the flag

2. **Processing Detection Failure**:
   - Verify Claude Code CLI version (`claude --version`)
   - Newer versions may use different spinner formats — check `PROCESSING_PATTERN`

3. **Authentication Issues**:
   ```bash
   claude setup-token
   # Or set ANTHROPIC_API_KEY environment variable
   ```

4. **Status Stuck on ERROR**:
   - Attach to tmux session and check terminal output
   - Verify Claude Code starts correctly in a regular terminal first
