---
sidebar_position: 2
---

# Quick Start

By the end of this guide, you'll have a supervisor agent running in a tmux session that can delegate tasks to worker agents. The full workflow takes about 2 minutes.

## Prerequisites

Before starting, make sure you have:
- CAO installed (`cao --version` should print a version)
- At least one AI CLI agent installed (e.g., Claude Code, Kiro CLI, or Codex)
- Credentials configured for your provider (e.g., `ANTHROPIC_API_KEY` for Claude Code, AWS credentials for Kiro CLI)

## 1. Start the server

In a dedicated terminal, start the CAO API server:

```bash
cao-server
```

You should see `Uvicorn running on http://127.0.0.1:9889`. Leave this running.

## 2. Launch a session

Open a **new terminal** and launch a session with an agent profile:

```bash
cao launch --agents code_supervisor --session-name my-session --provider claude_code
```

This will:
1. Show you a confirmation prompt with the agent's tool permissions
2. After you confirm, open a tmux window with the agent running
3. The agent starts processing in its terminal

:::info The `cao-` prefix
CAO prefixes every session with `cao-`, so `--session-name my-session` creates a
session named **`cao-my-session`**. Later commands (`cao session status`,
`cao session send`, `cao shutdown --session`) take the full prefixed name — pass
`my-session` and you'll get `No terminals found for session 'my-session'`. Run
`cao session list` any time you need the exact name.
:::

:::tip Choosing a provider
The `--provider` flag tells CAO which AI CLI to use. If you omit `--provider`, CAO uses the profile's `provider:` field, falling back to `kiro_cli`. Otherwise, specify whichever CLI you have installed:
- `--provider claude_code` — for Claude Code
- `--provider codex` — for OpenAI Codex
- `--provider copilot_cli` — for GitHub Copilot

Run `cao profile list` to see all available profiles.
:::

### Common launch flags

| Flag | Description |
|------|-------------|
| `--agents` | Agent profile to launch **(required)** |
| `--session-name` | Name for the session (auto-generated if omitted) |
| `--headless` | Run detached — don't attach to the tmux window |
| `--provider` | AI CLI provider. Defaults to the profile's `provider:` field, then `kiro_cli` |
| `--async` | Send the message and return immediately |
| `--yolo` | **[DANGEROUS]** Unrestricted tool access, skips all prompts |
| `--allowed-tools` | Override allowed tools for this session (repeatable) |
| `--auto-approve` | Skip the launch confirmation prompt |
| `--memory` | Also launch a memory manager terminal |
| `--working-directory` | Set the working directory for the agent |
| `--env KEY=VALUE` | Forward environment variables (repeatable) |

## 3. Check session status

```bash
cao session status cao-my-session
```

Expected output:
```
Session:  cao-my-session
Terminal: a1b2c3d4
Agent:    code_supervisor
Provider: claude_code
Status:   idle
```

Add `--workers` to see worker agents spawned by the supervisor, or `--json` for machine-readable output.

## 4. List all sessions

```bash
cao session list
```

## 5. Send a follow-up message

```bash
cao session send cao-my-session "Add OAuth2 support to the auth module"
```

This sends the message to the session's conductor (the first terminal — typically your supervisor agent). The command waits for the agent to finish processing and prints its response.

Use `--async` to send without waiting, or `--timeout 120` to set a custom wait time (default: 300s).

## 6. Shut down

```bash
# Shut down a specific session
cao shutdown --session cao-my-session

# Or shut down all sessions
cao shutdown --all
```

## How multi-agent orchestration works

When you launch a supervisor agent, it has access to **MCP tools** for delegating work. These are NOT CLI commands — they are tools the agent calls internally through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), an open standard for connecting AI agents to tools and data.

| MCP Tool | Behavior | Use Case |
|----------|----------|----------|
| `handoff` | Synchronous — blocks until the delegate finishes | Sequential tasks with dependencies |
| `assign` | Asynchronous — dispatches work and continues | Independent parallel tasks |
| `send_message` | Sends a message to another agent's inbox | Inter-agent coordination |

For example, when you ask a supervisor to "build an auth module," it may:
- Use `assign` to spin up a developer agent and a test-writer agent in parallel
- Use `handoff` to delegate a code review and wait for the result
- Workers use `send_message` to report results back to the supervisor

**You** interact with CAO through the CLI (`cao launch`, `cao session send`).  
**Agents** interact with each other through MCP tools.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Failed to connect to cao-server` | Make sure `cao-server` is running in another terminal |
| `Profile 'X' not found` | Run `cao profile list` to see available profiles. Install with `cao install <path-to-profile.md>` |
| Agent not responding | Attach to the tmux session: `tmux attach -t cao-my-session` |
| Port 9889 already in use | Another `cao-server` instance is running. Kill it or use `CAO_API_PORT=9890 cao-server` |

## Next steps

- Learn about [Profiles](../features/profiles.md) — how agents are configured
- Understand the [Architecture](../core-concepts/architecture.md) — how components fit together
- Explore [Orchestration Patterns](../patterns/handoff.md) in depth
- Set up [Scheduled Flows](../features/scheduled-flows.md) for automation
