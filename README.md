# CLI Agent Orchestrator

A lightweight orchestration system for managing multiple AI agent sessions in tmux terminals. Enables Multi-agent collaboration via MCP server.

For project structure and architecture details, see [CODEBASE.md](CODEBASE.md).

## Installation

1. Install tmux (version 3.3 or higher required)

```bash
bash <(curl -s https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/refs/heads/main/tmux-install.sh)
```

2. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

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

## MCP Server Tools and Orchestration Modes

CAO provides a local HTTP server that processes orchestration requests. CLI agents can interact with this server through MCP tools to coordinate multi-agent workflows.

### How It Works

Each agent terminal is assigned a unique `CAO_TERMINAL_ID` environment variable. The server uses this ID to:
- Route messages between agents
- Track terminal status (IDLE, BUSY, COMPLETED, ERROR)
- Manage terminal-to-terminal communication via inbox
- Coordinate orchestration operations

When an agent calls an MCP tool, the server identifies the caller by their `CAO_TERMINAL_ID` and orchestrates accordingly.

### Orchestration Modes

CAO supports three orchestration patterns:

**1. Handoff** - Transfer control to another agent and wait for completion
- Creates a new terminal with the specified agent profile
- Sends the task message and waits for the agent to finish
- Returns the agent's output to the caller
- Automatically exits the agent after completion
- Use when you need **synchronous** task execution with results

Example: Sequential code review workflow
```
Supervisor → handoff(developer, "Implement login feature") → waits
                                                              ↓
                                                    Developer completes
                                                              ↓
Supervisor ← receives code ← Developer exits
          ↓
          → handoff(reviewer, "Review login code") → waits
                                                      ↓
                                              Reviewer completes
                                                      ↓
Supervisor ← receives review ← Reviewer exits
```

**2. Delegate** - Spawn an agent to work independently (async)
- Creates a new terminal with the specified agent profile
- Sends the task message with callback instructions
- Returns immediately with the terminal ID
- Agent continues working in the background
- Delegated agent sends results back to supervisor via `send_message` when complete
- Messages are queued for delivery if the supervisor is busy (common in parallel workflows)
- Use for **asynchronous** task execution or fire-and-forget operations

Example: Parallel test execution
```
Supervisor → delegate(tester, "Run unit tests") → continues immediately
          → delegate(tester, "Run integration tests") → continues immediately
          → delegate(tester, "Run e2e tests") → continues immediately
                                                        ↓
Supervisor ← send_message("Unit tests passed") ← Tester 1
          ← send_message("Integration tests passed") ← Tester 2
          ← send_message("E2E tests passed") ← Tester 3
```

**3. Send Message** - Communicate with an existing agent
- Sends a message to a specific terminal's inbox
- Messages are queued and delivered when the terminal is idle
- Enables ongoing collaboration between agents
- Common for **swarm** operations where multiple agents coordinate dynamically
- Use for iterative feedback or multi-turn conversations

Example: Multi-role feature development
```
PM → send_message(developer_id, "Build payment API per spec")
Developer → send_message(pm_id, "Clarify refund flow?")
PM → send_message(developer_id, "Refunds go to original payment method")
Developer → send_message(reviewer_id, "Ready for review")
Reviewer → send_message(developer_id, "Add error handling for timeouts")
Developer → send_message(reviewer_id, "Updated")
Reviewer → send_message(pm_id, "Payment API approved")
```

### Custom Orchestration

The `cao-server` runs on `http://localhost:9889` by default and exposes REST APIs for session management, terminal control, and messaging. The CLI commands (`cao launch`, `cao shutdown`) and MCP server tools (`handoff`, `delegate`, `send_message`) are just examples of how these APIs can be packaged together.

You can combine the three orchestration modes above into custom workflows, or create entirely new orchestration patterns using the underlying APIs to fit your specific needs.

For complete API documentation, see [docs/api.md](docs/api.md).

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

