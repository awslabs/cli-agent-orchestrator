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
# Create a Codex CLI session
cao create codex developer

# Or using HTTP API
curl -X POST "http://localhost:9889/sessions" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "codex",
    "agent_profile": "developer"
  }'
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

### Agent Profile

Create a custom agent profile for Codex:

```yaml
# agent_profile.yaml
name: codex-developer
profile: developer
skills: [python, javascript, typescript, testing, documentation]
workspace: codex-workspace

# Codex CLI specific settings
codex:
  model: "gpt-4"
  timeout: 300
  auto_approve: false
```

### Provider Options

Configure Codex provider behavior:

```javascript
{
  "provider": "codex",
  "options": {
    "model": "gpt-4",
    "timeout": 300,
    "auto_approve": false,
    "confirmation_prompt": "Approve this action? (y/n)"
  }
}
```

## Workflows

### 1. Single Agent Tasks

```bash
# Create a Codex session for code review
cao create codex code-reviewer

# Send code review task
cao send <terminal-id> "Please review this Python code for security issues"

# Wait for completion and get response
cao get-output <terminal-id>
```

### 2. Supervisor-Worker Pattern

Create a supervisor agent that coordinates multiple Codex workers:

```python
# supervisor.py
def coordinate_refactoring_task():
    # Create supervisor agent
    supervisor = cao.create("codex", "supervisor")

    # Create worker agents
    frontend_worker = cao.create("codex", "frontend-developer")
    backend_worker = cao.create("codex", "backend-developer")
    tester_worker = cao.create("codex", "qa-tester")

    # Coordinate work
    supervisor.send("Plan refactoring of user authentication system")

    # Delegate tasks to workers
    frontend_worker.send("Update frontend auth components")
    backend_worker.send("Refactor backend auth services")
    tester_worker.send("Create tests for auth flow")

    # Collect results
    results = []
    for worker in [frontend_worker, backend_worker, tester_worker]:
        results.append(worker.get_output())

    # Final review
    final_review = supervisor.send(f"Review refactoring results: {results}")
    return final_review
```

### 3. Code Review Workflow

```bash
# Step 1: Create reviewer agent
reviewer_id=$(cao create codex code-reviewer)

# Step 2: Send code for review
cao send $reviewer_id "Review this pull request for security and performance issues"

# Step 3: Get detailed review
review_output=$(cao get-output $reviewer_id)

# Step 4: Create fixer agent
fixer_id=$(cao create codex developer)

# Step 5: Send review to fixer
cao send $fixer_id "Fix the issues identified in this review: $review_output"

# Step 6: Get fixes
fixes=$(cao get-output $fixer_id)

echo "Review completed and fixes applied"
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

2. **Timeout Issues**:
   - Increase timeout in provider configuration
   - Check network connectivity
   - Verify ChatGPT subscription status

3. **Status Detection Problems**:
   - Check terminal history for unexpected prompts
   - Verify Codex CLI version compatibility
   - Review custom prompt patterns

### Debug Mode

Enable debug logging for troubleshooting:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Create Codex provider with debug
provider = CodexProvider(
    terminal_id="debug-session",
    debug=True
)
```

## API Reference

### Provider Methods

```python
# Create Codex provider
provider = CodexProvider(
    terminal_id="session-id",
    debug=False
)

# Get terminal status
status = provider.get_status()
# Returns: TerminalStatus enum

# Get last message
message = provider.get_last_message()
# Returns: str (assistant response)

# Send input
provider.send_input("Your prompt here")
```

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

### 3. Error Handling

Always check for errors and handle them appropriately:
```python
try:
    status = provider.get_status()
    if status == TerminalStatus.ERROR:
        error_output = provider.get_output()
        logger.error(f"Codex task failed: {error_output}")
        return False
except Exception as e:
    logger.error(f"Provider error: {e}")
    return False
```

### 4. Resource Management

Clean up terminals after tasks complete:
```python
# Clean up completed sessions
def cleanup_session(terminal_id):
    provider = CodexProvider(terminal_id)
    try:
        provider.send_input("/exit")
        provider.delete_terminal()
    except Exception as e:
        logger.warning(f"Failed to cleanup session {terminal_id}: {e}")
```

## Examples

See the `examples/` directory for complete workflow examples:
- `examples/codex-basic/` - Basic Codex usage
- `examples/codex-supervisor/` - Supervisor-worker pattern
- `examples/codex-code-review/` - Automated code review workflow

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
- Documentation: [Codex CLI Provider Docs](https://github.com/awslabs/cli-agent-orchestrator/docs/codex-cli.md)