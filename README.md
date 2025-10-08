# CLI Agent Orchestrator

A lightweight orchestration system for managing multiple AI agent sessions in tmux terminals. Enables Multi-agent collaboration via MCP server.

For project structure and architecture details, see [CODEBASE.md](CODEBASE.md).

## Installation

1. Install [tmux](https://github.com/tmux/tmux/wiki/Installing)

2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)

3. Install CLI Agent Orchestrator:
```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main --upgrade
```

## Quick Start

### Installing Agents

CAO supports installing agents from multiple sources:

**1. Install built-in agents (bundled with CAO):**
```bash
cao install code_supervisor
cao install developer
cao install reviewer
```

**2. Install from a local file:**
```bash
cao install ./my-custom-agent.md
cao install /absolute/path/to/agent.md
```

**3. Install from a URL:**
```bash
cao install https://example.com/agents/custom-agent.md
```

When installing from a file or URL, the agent is saved to your local agent store (`~/.aws/cli-agent-orchestrator/agent-store/`) and can be referenced by name in future installations.

For details on creating custom agent profiles, see [docs/agent-profile.md](docs/agent-profile.md).

### Launching Agents

Start the cao server:
```bash
cao-server
```

In another terminal, launch a terminal with an agent profile:
```bash
cao launch --agents code_supervisor
```

Shutdown sessions:
```bash
# Shutdown all cao sessions
cao shutdown --all

# Shutdown specific session
cao shutdown --session cao-my-session
```

### Working with tmux Sessions

All agent sessions run in tmux. Useful commands:

```bash
# List all sessions
tmux list-sessions

# Attach to a session
tmux attach -t <session-name>

# Detach from session (inside tmux)
Ctrl+b, then d

# Delete a session
cao shutdown --session <session-name>
```

## Flows - Scheduled Agent Sessions

Flows allow you to schedule agent sessions to run automatically based on cron expressions.

### Prerequisites

Install the agent profile you want to use:
```bash
cao install developer
```

### Quick Start

The example flow asks a simple world trivia question every morning at 7:30 AM.

```bash
# 1. Start the cao server
cao-server

# 2. In another terminal, add a flow
cao flow add examples/flow/morning-trivia.md

# 3. List flows to see schedule and status
cao flow list

# 4. Manually run a flow (optional - for testing)
cao flow run morning-trivia

# 5. View flow execution (after it runs)
tmux list-sessions
tmux attach -t <session-name>

# 6. Cleanup session when done
cao shutdown --session <session-name>
```

**IMPORTANT:** The `cao-server` must be running for flows to execute on schedule.

### Example 1: Simple Scheduled Task

A flow that runs at regular intervals with a static prompt (no script needed):

**File: `daily-standup.md`**
```yaml
---
name: daily-standup
schedule: "0 9 * * 1-5"  # 9am weekdays
agent_profile: developer
---

Review yesterday's commits and create a standup summary.
```

### Example 2: Conditional Execution with Health Check

A flow that monitors a service and only executes when there's an issue:

**File: `monitor-service.md`**
```yaml
---
name: monitor-service
schedule: "*/5 * * * *"  # Every 5 minutes
agent_profile: developer
script: ./health-check.sh
---

The service at [[url]] is down (status: [[status_code]]).
Please investigate and triage the issue:
1. Check recent deployments
2. Review error logs
3. Identify root cause
4. Suggest remediation steps
```

**Script: `health-check.sh`**
```bash
#!/bin/bash
URL="https://api.example.com/health"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL")

if [ "$STATUS" != "200" ]; then
  # Service is down - execute flow
  echo "{\"execute\": true, \"output\": {\"url\": \"$URL\", \"status_code\": \"$STATUS\"}}"
else
  # Service is healthy - skip execution
  echo "{\"execute\": false, \"output\": {}}"
fi
```

### Flow Commands

```bash
# Add a flow
cao flow add daily-standup.md

# List all flows (shows schedule, next run time, enabled status)
cao flow list

# Enable/disable a flow
cao flow enable daily-standup
cao flow disable daily-standup

# Manually run a flow (ignores schedule)
cao flow run daily-standup

# Remove a flow
cao flow remove daily-standup
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.

