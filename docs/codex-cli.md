# Codex CLI Provider

## Overview

The Codex CLI provider enables CLI Agent Orchestrator (CAO) to work with **ChatGPT/Codex CLI** through your ChatGPT subscription, allowing you to orchestrate multiple Codex-based agents without migrating everything to API-based agents.

## Quick Start

### Prerequisites

1. **ChatGPT Subscription**: You need an active ChatGPT subscription
2. **Codex CLI**: Install and configure Codex CLI tool
3. **Authentication**: Authenticate Codex CLI with your ChatGPT account

```bash
# Install Codex CLI
pip install codex-cli

# Authenticate with ChatGPT
codex auth login
```

### Using Codex Provider with CAO

Create a terminal using the Codex provider:

```bash
# Start the CAO server in one terminal
cao-server

# In another terminal, launch a Codex-backed CAO session
cao launch --agents codex_developer --provider codex
```

You can also create a session via HTTP API (query parameters):

```bash
curl -X POST "http://localhost:9889/sessions?provider=codex&agent_profile=codex_developer"
```

## Features

### Status Detection

The Codex provider automatically detects terminal states:

- **IDLE**: Terminal is ready for input
- **PROCESSING**: Codex is thinking or working
- **WAITING_USER_ANSWER**: Waiting for user approval/confirmation
- **COMPLETED**: Task finished with assistant response
- **ERROR**: Error occurred during execution

### Message Extraction

The provider automatically extracts the last assistant response from terminal output, making it easy to parse and process results.

## Configuration

CAO's Codex provider currently launches `codex` and relies on your existing Codex CLI configuration/authentication.

- `--provider codex` selects the provider.
- `--agents <name>` is stored as terminal metadata and used for tmux window naming; it does not change Codex behavior.
- Model/timeout/approval settings are configured in Codex CLI itself (outside of CAO).

## Workflows

### 1. Interactive single-agent task

```bash
cao launch --agents codex_developer --provider codex
```

In the tmux window, type your prompt at the Codex prompt.

To get the CAO terminal id (useful for API automation / MCP), run:

```bash
echo "$CAO_TERMINAL_ID"
```

### 2. Automate send/get-output via HTTP API

```bash
python3 - <<'PY'
import time

import requests

terminal_id = "<terminal-id>"

requests.post(
    f"http://localhost:9889/terminals/{terminal_id}/input",
    params={"message": "Please review this Python code for security issues"},
).raise_for_status()

# Poll status until completion
while True:
    status = requests.get(f"http://localhost:9889/terminals/{terminal_id}").json()["status"]
    if status in {"completed", "error", "waiting_user_answer"}:
        break
    time.sleep(1)

resp = requests.get(
    f"http://localhost:9889/terminals/{terminal_id}/output",
    params={"mode": "last"},
)
resp.raise_for_status()
print(resp.json()["output"])
PY
```

## Authentication

### ChatGPT Subscription Setup

1. **Install Codex CLI**:
   ```bash
   pip install codex-cli
   ```

2. **Authenticate**:
   ```bash
   codex auth login
   # Follow browser authentication flow
   ```

3. **Verify Authentication**:
   ```bash
   codex auth status
   ```

### Workspace Setup

Configure your workspace for Codex development:

```bash
# Create workspace directory
mkdir codex-workspace
cd codex-workspace

# Initialize project structure
mkdir -p src tests docs

# Create .codex config file
cat > .codexrc << EOF
{
  "model": "gpt-4",
  "timeout": 300,
  "workspace": "./src"
}
EOF
```

## Troubleshooting

### Common Issues

1. **Authentication Failed**:
   ```bash
   # Re-authenticate
   codex auth logout
   codex auth login
   ```

2. **Timeout / Hanging Tasks**:
   - Confirm `codex` works in a regular shell (`codex`, then exit)
   - Attach to the tmux session and check whether Codex is waiting for input/approval
   - Verify your ChatGPT subscription status and network connectivity

3. **Status Detection Problems**:
   - Check terminal history for unexpected prompts
   - Verify Codex CLI version compatibility
   - Review custom prompt patterns

## Implementation Notes

- Status detection is implemented in `CodexProvider.get_status()` (terminal output parsing).
- Output mode `last` uses `CodexProvider.extract_last_message_from_script()`.
- Exiting a Codex terminal uses `/exit` (`POST /terminals/{terminal_id}/exit`).

### Status Values

- `TerminalStatus.IDLE`: Ready for input
- `TerminalStatus.PROCESSING`: Working on task
- `TerminalStatus.WAITING_USER_ANSWER`: Waiting for user input
- `TerminalStatus.COMPLETED`: Task finished
- `TerminalStatus.ERROR`: Error occurred

## Best Practices

### 1. Agent Naming

Use descriptive names for Codex agents:
- `codex-frontend-dev` - Frontend development
- `codex-security-reviewer` - Security code review
- `codex-api-designer` - API design and documentation

### 2. Task Breakdown

Break complex tasks into smaller, focused prompts:
```python
# Instead of:
"Build a complete web application"

# Use:
"Design the database schema for user authentication"
"Implement the authentication API endpoints"
"Create the login form component"
"Write tests for the authentication flow"
```



## Examples

See the `examples/` directory for a step-by-step walkthrough:
- `examples/codex-basic/` - Basic Codex usage (includes three agent profiles)

## Contributing

To contribute to the Codex provider:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Update documentation
5. Submit a pull request

## Support

For issues and questions:
- GitHub Issues: [cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator/issues)
- Documentation: [Codex CLI Provider Docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/codex-cli.md)
