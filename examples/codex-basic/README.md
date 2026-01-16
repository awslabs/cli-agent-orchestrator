# Codex CLI Basic Usage Example

This example demonstrates how to use the **Codex CLI provider** for basic AI agent orchestration tasks with your ChatGPT subscription.

## What You'll Learn

- **ChatGPT Integration**: Use your existing ChatGPT subscription with CAO
- **Basic Agent Creation**: Create Codex agents for different development tasks
- **Status Detection**: Understand how CAO detects Codex CLI states
- **Message Extraction**: Extract responses from Codex agents
- **Multi-Agent Coordination**: Simple supervisor-worker patterns

## Prerequisites

1. **ChatGPT Subscription**: Active subscription required
2. **Codex CLI**: Install and authenticate:
   ```bash
   pip install codex-cli
   codex auth login
   codex auth status  # Should show authenticated
   ```

3. **CLI Agent Orchestrator**: Installed and running
   ```bash
   cao-server  # Run in one terminal
   ```

## Quick Start Example

### 1. Create a Simple Codex Agent

```bash
# Start cao-server in one terminal
cao-server

# In another terminal, create a Codex session
cao launch --agents codex_developer --provider codex
```

### 2. Send Your First Task

```bash
# In the tmux window, paste your prompt at the Codex prompt.

# Optional: print the CAO terminal id (useful for API automation / MCP)
echo "$CAO_TERMINAL_ID"
```

Optional automation from another terminal (send input + get extracted last message):

```bash
python3 - <<'PY'
import time

import requests

terminal_id = "<terminal-id>"

requests.post(
    f"http://localhost:9889/terminals/{terminal_id}/input",
    params={"message": "Write a Python function to validate email addresses using regex"},
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

## Agent Profiles

The example includes pre-configured agent profiles for different Codex-based tasks:

### 1. Codex Developer (`codex_developer.md`)
- General programming and development tasks
- Code writing, debugging, refactoring
- Language: Python, JavaScript, TypeScript

### 2. Codex Reviewer (`codex_reviewer.md`)
- Code review and security analysis
- Best practices and optimization
- Testing and quality assurance

### 3. Codex Documenter (`codex_documenter.md`)
- Technical writing and documentation
- README files, API docs, tutorials
- Clear, structured communication

## Setup

No CAO-side agent installation is required for the `codex` provider. The `--agents` value is used for naming/metadata only.

## Usage Examples

### Example 1: Single Agent Code Generation

```bash
# Launch a Codex developer
cao launch --agents codex_developer --provider codex

# In the agent terminal:
"Write a Python function that:
1. Takes a list of URLs
2. Downloads each URL content
3. Extracts all email addresses
4. Returns a unique list of emails

Include proper error handling and docstring."
```

**Expected Output:**
- Agent will think and process (PROCESSING state)
- Write the Python function with proper structure
- Return completed code (COMPLETED state)

### Example 2: Code Review Workflow

```bash
# Launch a Codex reviewer
cao launch --agents codex_reviewer --provider codex
```

Paste this prompt into the Codex CLI:

```text
Please review this Python code for security issues:

    import subprocess

    def execute_command(user_input):
        command = f"ls {user_input}"
        return subprocess.run(command, shell=True, capture_output=True)

Focus on:
1. Security vulnerabilities
2. Input validation
3. Safe coding practices
4. Potential improvements
```

### Example 3: Documentation Generation

```bash
# Launch a Codex documenter
cao launch --agents codex_documenter --provider codex

# Request documentation:
"Create comprehensive README documentation for a Python package that:
- Has functions for data processing
- Includes installation instructions
- Provides usage examples
- Documents API reference
- Includes contribution guidelines

Make it professional and developer-friendly."
```

## Understanding Codex Provider Behavior

### Status Detection

The Codex provider automatically detects these states:

1. **PROCESSING**: Codex is thinking or working
   ```
   [Agent is thinking...]
   ```

2. **WAITING_USER_ANSWER**: Waiting for confirmation
   ```
   Approve this action? (y/n)
   ```

3. **COMPLETED**: Task finished with response
   ```
   ❯  # Ready for next command
   ```

4. **ERROR**: Error occurred
   ```
   Error: Invalid input provided
   ```

### Message Extraction

CAO automatically extracts the last assistant response:

```python
# CAO extracts this part
def validate_email(email):
    """Validate email address using regex."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

# This is the response returned by GET /terminals/{terminal_id}/output?mode=last
```

## Advanced Examples

### Example 4: Sequential Code Development

```bash
# Launch developer
cao launch --agents codex_developer --provider codex

# Multi-step task:
"1. First, write a Python class for User with fields: id, name, email, created_at
2. Then, add methods for validation and serialization
3. Finally, write unit tests for the User class

Proceed step by step and show me each part."
```

### Example 5: Code Refactoring

```bash
# Launch reviewer then developer
cao launch --agents codex_reviewer --provider codex
```

Paste this prompt into the Codex CLI:

```text
Review this legacy code and suggest refactoring improvements:

    def process_data(data):
        result = []
        for i in range(len(data)):
            if data[i] > 0:
                result.append(data[i] * 2)
            else:
                result.append(0)
        return result

Identify:
1. Code smells and anti-patterns
2. Performance improvements
3. Pythonic alternatives
4. Better naming and structure

Then provide a refactored version with explanations.
```

## Troubleshooting

### Common Issues

1. **Authentication Failed**:
   ```bash
   codex auth logout
   codex auth login
   ```

2. **Agent Not Responding**:
   - Check `tmux list-sessions` for session status
   - Verify ChatGPT subscription is active
   - Check network connectivity

3. **Status Detection Issues**:
   - Agent might be in unexpected state
   - Check terminal output manually: `tmux attach -t <session-name>`
   - Verify Codex CLI version compatibility

## Performance Tips

1. **Clear Tasks**: Be specific about what you want
2. **Step by Step**: Break complex tasks into smaller steps
3. **Context Management**: Provide relevant context upfront
4. **Validation**: Ask for explanations of complex logic

## Next Steps

After mastering the basics, explore:

- **Multi-Agent Patterns**: See `examples/assign/` for step-by-step patterns you can adapt to Codex
- **Workflow Integration**: Combine with other providers
- **Custom Agent Profiles**: Create specialized Codex agents
- **MCP Integration**: Use Codex agents in MCP workflows

## Support

For issues:
- Check [troubleshooting section](#troubleshooting)
- Review [main documentation](../../docs/codex-cli.md)
- Report issues on [GitHub](https://github.com/awslabs/cli-agent-orchestrator/issues)

## Contributing

To contribute Codex examples:
1. Fork the repository
2. Create new example in `examples/codex-*/`
3. Follow the established pattern
4. Update documentation
5. Submit a pull request