---
sidebar_position: 1
---

# CLI Agent Orchestrator

CLI Agent Orchestrator (CAO) is a lightweight orchestration system for managing multiple AI agent sessions in tmux terminals. It enables hierarchical multi-agent collaboration through supervisor and worker agent patterns.

## Why CAO?

- **Agent isolation** — Each agent runs in its own tmux window with full context separation
- **Multiple orchestration patterns** — Handoff (synchronous), Assign (asynchronous), Send Message (direct communication)
- **Multi-provider support** — Works with 10 CLI agent providers
- **Scheduled flows** — Cron-like task scheduling for automated workflows
- **MCP Server** — Expose orchestration capabilities to any MCP-compatible client
- **Web UI** — Monitor and manage agent sessions from a browser dashboard
- **Profile system** — Discover and configure agent profiles dynamically

## Supported Providers

| Provider | Identifier |
|----------|------------|
| Claude Code | `claude_code` |
| Kiro CLI | `kiro_cli` |
| Codex | `codex` |
| Kimi CLI | `kimi_cli` |
| GitHub Copilot CLI | `copilot_cli` |
| OpenCode CLI | `opencode_cli` |
| Oh My Pi | `omp` |
| Hermes | `hermes` |
| Cursor CLI | `cursor_cli` |
| Antigravity CLI | `antigravity_cli` |

> **Note:** Antigravity CLI is the Google-backed provider (formerly Gemini CLI).

## Installation

```bash
uv tool install cli-agent-orchestrator
```

## Quick Start

```bash
# Start the API server (required — run in a separate terminal)
cao-server

# Launch a session with a profile (uses your default provider)
cao launch --agents code_supervisor --session-name my-session

# Send a follow-up message to the session
cao session send cao-my-session "Start the code review workflow"

# Shut down when done
cao shutdown --all
```

A **profile** defines an agent's role, tools, and system prompt. Run `cao profile list` to see what's available. See the [Quick Start guide](getting-started/quick-start.md) for a full walkthrough.

## Key Concepts

| Term | Meaning |
|------|---------|
| **Session** | A tmux session containing one or more agent terminals. Created by `cao launch`. |
| **Terminal** | A single agent running in a tmux window. Identified by an 8-character hex ID. |
| **Conductor** | The first terminal in a session — typically your supervisor agent. |
| **Profile** | A markdown file defining an agent's role, system prompt, provider, and tool access. |
| **Provider** | The AI CLI backend (e.g., `claude_code`, `kiro_cli`) that powers an agent. |
| **MCP** | [Model Context Protocol](https://modelcontextprotocol.io) — an open standard for connecting AI agents to tools. CAO exposes its orchestration as MCP tools so agents can spawn/message other agents. |

## Quick Links

- [Installation](getting-started/installation.md)
- [Quick Start](getting-started/quick-start.md)
- [Orchestration Patterns](patterns/handoff.md)
- [CLI Reference](reference/cli-commands.md)
- [GitHub Repository](https://github.com/awslabs/cli-agent-orchestrator)
