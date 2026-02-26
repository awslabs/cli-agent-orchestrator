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

The Kiro CLI provider supports two modes for status detection:

#### 1. Hook-Based Status Tracking (Recommended)

Hook-based status tracking provides improved performance and reliability:

- Status updates sent directly to CAO API via hooks
- No continuous tmux polling required
- Faster and more reliable status detection
- Better scalability with many concurrent agents

Enable hooks during installation using the `--use-hooks` flag:

```bash
# Install with hooks enabled for automatic status updates
cao install my-agent.md --use-hooks
cao launch --provider kiro_cli --agent my-agent
```

```bash
# Install without hooks (default) - status checks via tmux polling only
cao install my-agent.md
cao launch --provider kiro_cli --agent my-agent
```

##### Hook Variables

The following environment variables are available in hook commands:

- `$CAO_TERMINAL_ID`: Terminal identifier (8-character hex string, e.g., "abc12345")
- `$CAO_SESSION_NAME`: Tmux session name
- `$CAO_AGENT_PROFILE`: Agent profile name

##### Hook Implementation

Hooks use `curl` to make HTTP requests to the CAO API with smart failure handling and retries:

```bash
# Example hook command (auto-injected by --use-hooks)
[ -z "$CAO_TERMINAL_ID" ] || \
  curl -sf --max-time 2 --retry 3 --retry-delay 1 --retry-max-time 10 \
    -X POST "http://localhost:9889/terminals/$CAO_TERMINAL_ID/status?new_status=idle"
```

**Behavior:**
- **If `CAO_TERMINAL_ID` is NOT set**: Hook succeeds immediately (agent usable outside CAO)
- **If `CAO_TERMINAL_ID` IS set**: Hook fails if curl fails after retries (ensures hooks work correctly in CAO)

This design allows the same agent definition to work both inside CAO (with hooks) and standalone (without hooks).

**Retry Strategy:**
- **3 retry attempts** on transient failures (network errors, timeouts, 5xx server errors)
- **Exponential backoff**: 1s, 2s, 4s between retries
- **10 second max total time** across all attempts
- **No retry on 4xx errors** (like 422 validation errors - these are permanent failures)

**Security Features:**
- Terminal ID validated at API level (8-char hex pattern)
- URL properly quoted to prevent injection
- Silent mode (`-s`) and fail fast (`-f`)
- 2 second timeout per request

##### Hook Failure Handling

**When `CAO_TERMINAL_ID` is not set** (standalone usage):
- Hook succeeds immediately without making API call
- Agent works normally without CAO integration

**When `CAO_TERMINAL_ID` is set** (CAO usage):
- Hook retries up to 3 times with exponential backoff (1s, 2s, 4s)
- Hook fails if all retries exhausted (network error, API unavailable, timeout, etc.)
- Kiro CLI will show the hook failure after all retries
- This is intentional - if hooks are expected to work, failures should be visible

**Important**: Once hooks start working, if they fail later (e.g., persistent network issue after retries), status may become stale. This is acceptable as the alternative (continuous tmux polling) creates resource bottlenecks.

**Failure scenarios when `CAO_TERMINAL_ID` is set**:
- **curl not installed**: Hook fails immediately ❌
- **Transient network error**: Retries 3 times, may succeed ✅
- **Persistent network error**: Hook fails after retries ❌
- **API unavailable**: Retries 3 times, fails if API doesn't come back ❌
- **Invalid terminal ID**: API returns 422, no retry (permanent error) ❌
- **Timeout**: Retries up to 3 times, fails if all timeout ❌

##### Debugging Hooks

Check if hooks are working:

```bash
# View CAO server logs
tail -f ~/.cao/logs/server.log

# Test hook manually
export CAO_TERMINAL_ID=abc12345
curl -sf --max-time 2 -X POST \
  "http://localhost:9889/terminals/$CAO_TERMINAL_ID/status?new_status=idle"

# Check terminal status via API
curl http://localhost:9889/terminals/abc12345
```

To disable hook-based status for debugging (when hooks are installed):

```bash
export CAO_KIRO_USE_HOOK_STATUS=false
cao launch --provider kiro_cli --agent my-agent
```

See the [Kiro Hooks Example](../examples/kiro-hooks/README.md) for detailed information.

#### 2. Tmux Polling (Fallback)

When hook-based status is unavailable or disabled, detects terminal states by analyzing ANSI-stripped output:

- **IDLE**: Agent prompt visible (`[profile_name] >` pattern), no response content
- **PROCESSING**: No idle prompt found in output (agent is generating response)
- **COMPLETED**: Green arrow (`>`) response marker present + idle prompt after it
- **WAITING_USER_ANSWER**: Permission prompt visible (`Allow this action? [y/n/t]:`)
- **ERROR**: Known error indicators present (e.g., "Kiro is having trouble responding right now")

Status detection priority: no prompt → PROCESSING → ERROR → WAITING_USER_ANSWER → COMPLETED → IDLE.

### Status Values

CAO uses the following status values to track terminal state:

| Status | Meaning | When Set |
|--------|---------|----------|
| `idle` | Agent is ready for input | After agent starts, after response completes |
| `processing` | Agent is generating response | After user submits prompt |
| `completed` | Response finished, prompt visible | After agent completes response (polling only) |
| `waiting_user_answer` | Agent waiting for permission | When permission prompt detected (polling only) |
| `error` | Agent encountered an error | When error indicators detected (polling only) |

**Note**: Hook-based status only sets `idle` and `processing`. Other statuses are detected via tmux polling.

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
