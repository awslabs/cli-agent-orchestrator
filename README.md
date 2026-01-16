# CLI Agent Orchestrator

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/awslabs/cli-agent-orchestrator)

CLI Agent Orchestrator(CAO, pronounced as "kay-oh"), is a lightweight orchestration system for managing multiple AI agent sessions in tmux terminals. Enables Multi-agent collaboration via MCP server.

## Hierarchical Multi-Agent System

CLI Agent Orchestrator (CAO) implements a hierarchical multi-agent system that enables complex problem-solving through specialized division of CLI Developer Agents.

![CAO Architecture](./docs/assets/cao_architecture.png)

### Key Features

* **Hierarchical orchestration** – CAO's supervisor agent coordinates workflow management and task delegation to specialized worker agents. The supervisor maintains overall project context while agents focus on their domains of expertise.
* **Session-based isolation** – Each agent operates in isolated tmux sessions, ensuring proper context separation while enabling seamless communication through Model Context Protocol (MCP) servers. This provides both coordination and parallel processing capabilities.
* **Intelligent task delegation** – CAO automatically routes tasks to appropriate specialists based on project requirements, expertise matching, and workflow dependencies. The system adapts between individual agent work and coordinated team efforts through three orchestration patterns:
    - **Handoff** - Synchronous task transfer with wait-for-completion
    - **Assign** - Asynchronous task spawning for parallel execution  
    - **Send Message** - Direct communication with existing agents
* **Flexible workflow patterns** – CAO supports both sequential coordination for dependent tasks and parallel processing for independent work streams. This allows optimization of both development speed and quality assurance processes.
* **Flow - Scheduled runs** – Automated execution of workflows at specified intervals using cron-like scheduling, enabling routine tasks and monitoring workflows to run unattended.
* **Context preservation** – The supervisor agent provides only necessary context to each worker agent, avoiding context pollution while maintaining workflow coherence.
* **Direct worker interaction and steering** – Users can interact directly with worker agents to provide additional steering, distinguishing from sub-agents features by allowing real-time guidance and course correction.
* **Advanced CLI integration** – CAO agents have full access to advanced features of the developer CLI, such as the [sub-agents](https://docs.claude.com/en/docs/claude-code/sub-agents) feature of Claude Code, [Custom Agent](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-custom-agents.html) of Amazon Q Developer for CLI and so on.

For detailed project structure and architecture, see [CODEBASE.md](CODEBASE.md).

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

**Provider Selection:**

By default, agents are installed for the `q_cli` provider (Amazon Q CLI). You can specify a different provider:

```bash
# Install for Kiro CLI
cao install developer --provider kiro_cli

# Install for Amazon Q CLI (default)
cao install developer --provider q_cli
```

Note: The `claude_code` provider does not require agent installation.

For details on creating custom agent profiles, see [docs/agent-profile.md](docs/agent-profile.md).

## Codex CLI Provider

The **Codex CLI provider** enables CAO to work with **ChatGPT/Codex CLI** through your ChatGPT subscription, allowing you to orchestrate multiple Codex-based agents without migrating to API-based agents.

### Key Benefits

- **ChatGPT Integration**: Use your existing ChatGPT subscription for agent orchestration
- **No Migration Required**: Continue using Codex CLI without switching to API-based agents
- **Multi-Agent Coordination**: Orchestrate multiple Codex agents in supervisor-worker patterns
- **Status Detection**: Automatic detection of processing, waiting, completed, and error states

### Quick Start

```bash
# Start the CAO server (in one terminal)
cao-server

# Install an example Codex agent profile
cao install examples/codex-basic/codex_developer.md --provider codex

# Launch a Codex-backed agent (opens a tmux window)
cao launch --agents codex_developer --provider codex

# In the tmux window, paste your prompt at the Codex prompt.

# Optional: print the CAO terminal id (useful for API automation / MCP)
echo "$CAO_TERMINAL_ID"
```

Optional automation (send input + fetch extracted last message) from another terminal:

```bash
TERMINAL_ID="<terminal-id>"

curl -X POST "http://localhost:9889/terminals/${TERMINAL_ID}/input" \
  --get \
  --data-urlencode 'message=Review this Python code for security issues'

curl "http://localhost:9889/terminals/${TERMINAL_ID}/output?mode=last"
```

For detailed documentation and examples, see [docs/codex-cli.md](docs/codex-cli.md).

### Launching Agents

Start the cao server:

```bash
cao-server
```

In another terminal, launch a terminal with an agent profile:

```bash
cao launch --agents code_supervisor

# Or specify a provider
cao launch --agents code_supervisor --provider kiro_cli
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

# Switch between windows (inside tmux)
Ctrl+b, then n          # Next window
Ctrl+b, then p          # Previous window
Ctrl+b, then <number>   # Go to window number (0-9)
Ctrl+b, then w          # List all windows (interactive selector)

# Delete a session
cao shutdown --session <session-name>
```

**List all windows (Ctrl+b, w):**

![Tmux Window Selector](./docs/assets/tmux_all_windows.png)

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

> **Note:** All orchestration modes support optional `working_directory` parameter when enabled via `CAO_ENABLE_WORKING_DIRECTORY=true`. See [Working Directory Support](#working-directory-support) for details.

**1. Handoff** - Transfer control to another agent and wait for completion

- Creates a new terminal with the specified agent profile
- Sends the task message and waits for the agent to finish
- Returns the agent's output to the caller
- Automatically exits the agent after completion
- Use when you need **synchronous** task execution with results

Example: Sequential code review workflow

![Handoff Workflow](./docs/assets/handoff-workflow.png)

**2. Assign** - Spawn an agent to work independently (async)

- Creates a new terminal with the specified agent profile
- Sends the task message with callback instructions
- Returns immediately with the terminal ID
- Agent continues working in the background
- Assigned agent sends results back to supervisor via `send_message` when complete
- Messages are queued for delivery if the supervisor is busy (common in parallel workflows)
- Use for **asynchronous** task execution or fire-and-forget operations

Example: A supervisor assigns parallel data analysis tasks to multiple analysts while using handoff to sequentially generate a report template, then combines all results.

See [examples/assign](examples/assign) for the complete working example.

![Parallel Data Analysis](./docs/assets/parallel-data-analysis.png)

**3. Send Message** - Communicate with an existing agent

- Sends a message to a specific terminal's inbox
- Messages are queued and delivered when the terminal is idle
- Enables ongoing collaboration between agents
- Common for **swarm** operations where multiple agents coordinate dynamically
- Use for iterative feedback or multi-turn conversations

Example: Multi-role feature development

![Multi-role Feature Development](./docs/assets/multi-role-feature-development.png)

### Custom Orchestration

The `cao-server` runs on `http://localhost:9889` by default and exposes REST APIs for session management, terminal control, and messaging. The CLI commands (`cao launch`, `cao shutdown`) and MCP server tools (`handoff`, `assign`, `send_message`) are just examples of how these APIs can be packaged together.

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
provider: q_cli  # Optional, defaults to q_cli
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

## Working Directory Support

CAO supports specifying working directories for agent handoff/delegation operations.

### Configuration

Enable working directory parameter in MCP tools:

```bash
export CAO_ENABLE_WORKING_DIRECTORY=true
```

### Behavior

- **When disabled (default)**: Working directory parameter is hidden from tools, agents start in supervisor's current directory
- **When enabled**: Tools expose `working_directory` parameter, allowing explicit directory specification
- **Default directory**: Current working directory (`cwd`) of the supervisor agent

### Usage Example

With `CAO_ENABLE_WORKING_DIRECTORY=true`:

```python
# Handoff to agent in specific package directory
result = await handoff(
    agent_profile="developer",
    message="Fix the bug in UserService.java",
    working_directory="/workspace/src/MyPackage"
)

# Assign task with specific working directory
result = await assign(
    agent_profile="reviewer",
    message="Review the changes in the authentication module",
    working_directory="/workspace/src/AuthModule"
)
```

### Why Disabled by Default?

Q CLI includes current working directory in context, causing agents to hallucinate/infer directories when the parameter is available. Disabling by default prevents this hallucination for users who don't need explicit directory control.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.