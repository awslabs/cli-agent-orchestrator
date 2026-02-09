# Kiro CLI Provider

## Overview

The Kiro CLI provider enables CLI Agent Orchestrator (CAO) to work with **Kiro CLI**, an AI-powered coding assistant that operates through agent-based conversations with customizable profiles.

## Quick Start

### Prerequisites

1. **AWS Credentials**: Kiro CLI authenticates via AWS
2. **Kiro CLI**: Install the CLI tool
3. **tmux**: Required for terminal management

```bash
# Install Kiro CLI
npm install -g @anthropic-ai/kiro-cli

# Verify authentication
kiro-cli --version
```

### Using Kiro CLI Provider with CAO

```bash
# Start the CAO server
cao-server

# Launch a Kiro CLI-backed session (agent profile is required)
cao launch --agents developer --provider kiro_cli
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=kiro_cli&agent_profile=developer"
```

**Note**: Kiro CLI requires an agent profile — it cannot be launched without one.

## Features

### Status Detection

The Kiro CLI provider detects terminal states by analyzing ANSI-stripped output:

- **IDLE**: Agent prompt visible (`[profile_name] >` pattern), no response content
- **PROCESSING**: No idle prompt found in output (agent is generating response)
- **COMPLETED**: Green arrow (`>`) response marker present + idle prompt after it
- **WAITING_USER_ANSWER**: Permission prompt visible (`Allow this action? [y/n/t]:`)
- **ERROR**: Known error indicators present (e.g., "Kiro is having trouble responding right now")

Status detection priority: no prompt → PROCESSING → ERROR → WAITING_USER_ANSWER → COMPLETED → IDLE.

### Dynamic Prompt Pattern

The idle prompt pattern is built dynamically from the agent profile name:

```
[developer] >          # Basic prompt
[developer] !>         # Prompt with pending changes
[developer] 50% >      # Prompt with progress indicator
[developer] λ >        # Prompt with lambda symbol
[developer] 50% λ >    # Combined progress and lambda
```

Pattern: `\[{agent_profile}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*`

### Message Extraction

The provider extracts the last assistant response using the green arrow indicator:

1. Strip ANSI codes from output
2. Find all green arrow (`>`) markers (response start)
3. Take the last one
4. Find the next idle prompt after it (response end)
5. Extract and clean text between them (strip ANSI, escape sequences, control characters)

### Permission Prompts

Kiro CLI shows `Allow this action? [y/n/t]:` prompts for sensitive operations (file edits, command execution). The provider detects these as `WAITING_USER_ANSWER` status. Unlike Claude Code, Kiro CLI does not have a trust folder dialog.

## Configuration

### Agent Profile (Required)

Kiro CLI always requires an agent profile. CAO passes it via:

```
kiro-cli chat --agent {profile_name}
```

The profile name determines the prompt pattern used for status detection. Built-in profiles include `developer` and `reviewer`.

### Launch Command

The provider constructs a simple command:

```
kiro-cli chat --agent developer
```

No additional flags are needed for tmux compatibility.

## Implementation Notes

- **ANSI stripping**: All pattern matching operates on ANSI-stripped output for reliability
- **Green arrow pattern**: `^>\s*` matches the start of agent responses (after ANSI stripping)
- **Generic prompt pattern**: `\x1b\[38;5;13m>\s*\x1b\[39m\s*$` matches the purple-colored prompt in raw output (used for log monitoring)
- **Error detection**: Checks for known error strings like "Kiro is having trouble responding right now"
- **Multi-format cleanup**: Extraction strips ANSI codes, escape sequences, and control characters
- **Exit command**: `/exit` via `POST /terminals/{terminal_id}/exit`

### Status Values

- `TerminalStatus.IDLE`: Ready for input
- `TerminalStatus.PROCESSING`: Working on task
- `TerminalStatus.WAITING_USER_ANSWER`: Waiting for permission confirmation
- `TerminalStatus.COMPLETED`: Task finished
- `TerminalStatus.ERROR`: Error occurred

## End-to-End Testing

The E2E test suite validates handoff, assign, and send_message flows for Kiro CLI.

### Running Kiro CLI E2E Tests

```bash
# Start CAO server
uv run cao-server

# Run all Kiro CLI E2E tests
uv run pytest -m e2e test/e2e/ -v -k kiro_cli

# Run specific test types
uv run pytest -m e2e test/e2e/test_handoff.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_assign.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_send_message.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k KiroCli -o "addopts="
```

## Troubleshooting

### Common Issues

1. **"Agent profile required" Error**:
   - Kiro CLI cannot be launched without an agent profile
   - Always specify `--agents` when launching: `cao launch --agents developer --provider kiro_cli`

2. **Permission Prompts Blocking**:
   - Kiro CLI shows `[y/n/t]` prompts for operations
   - The provider detects these as `WAITING_USER_ANSWER`
   - In multi-agent flows, the supervisor or user must handle these

3. **Authentication Issues**:
   ```bash
   # Verify AWS credentials
   aws sts get-caller-identity
   # Set credentials via environment
   export AWS_ACCESS_KEY_ID=...
   export AWS_SECRET_ACCESS_KEY=...
   export AWS_DEFAULT_REGION=...
   ```

4. **Prompt Pattern Not Matching**:
   - The prompt pattern is built from the agent profile name
   - Custom profiles must use the standard `[name] >` prompt format
   - Check with: `kiro-cli chat --agent your_profile`
