# OpenClaw Integration

## What is OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is an open-source, self-hosted AI agent gateway (227k+ GitHub stars, MIT license) that bridges messaging apps to AI agents. It supports Telegram, Slack, Discord, WhatsApp, Signal, iMessage, and more through a channel-adapter architecture — each platform gets a dedicated adapter that normalizes messages into a common internal format. The gateway routes messages to a configured LLM (Claude, GPT, Gemini, or local models via Ollama), executes tool calls, and delivers responses back through the originating platform.

OpenClaw runs as a single Node.js process (TypeScript, requires Node 22+) on port `18789` (WebSocket, bound to `127.0.0.1` by default). It natively supports MCP servers via `@modelcontextprotocol/sdk`, configured under the `mcpServers` key in `openclaw.json`. The **mcporter** skill (installable from ClawHub) provides a CLI and runtime for listing, calling, and managing MCP server tools — making it straightforward to expose any MCP-compatible server to OpenClaw's agent.

## Comparison with Claude Code Remote Control

| Dimension | Claude Code Remote Control | CAO + OpenClaw |
|---|---|---|
| **Scope** | 1:1 — one user controls one Claude Code session | 1:many — multiple users across multiple messaging platforms control multiple agent sessions |
| **Supported providers** | Claude only | All providers CAO supports (Claude Code, Codex, Kimi, Gemini CLI, Aider, etc.) |
| **User interface** | Anthropic phone app / web | Telegram, Slack, Discord, WhatsApp, or any OpenClaw channel |
| **Cost** | Included with Claude subscription | Self-hosted — LLM API costs only, no per-seat fees |
| **Hosting model** | Managed by Anthropic | Fully self-hosted (OpenClaw gateway + CAO) |
| **Multi-agent orchestration** | No — single agent session | Yes — CAO manages multiple concurrent terminal sessions with different providers |
| **Extensibility** | Limited to Claude Code capabilities | OpenClaw skills ecosystem (10,700+ community skills) + CAO MCP tools |

## What CAO Would Need

### New read-only MCP tools

CAO would expose three new MCP tools for OpenClaw to query terminal state:

- **`list_terminals`** — Returns all active terminal sessions with their IDs, provider names, and current status (idle / running / error).
- **`get_terminal_status`** — Given a terminal ID, returns detailed status: provider, working directory, whether a task is running, and elapsed time.
- **`get_terminal_output`** — Given a terminal ID and an optional line offset, returns recent terminal output (stdout/stderr). Supports pagination to avoid oversized responses.

All three tools are read-only and safe to expose without confirmation prompts.

### OpenClaw mcporter configuration

Point OpenClaw's mcporter at CAO's MCP server in `openclaw.json`:

```json
{
  "agents": {
    "main": {
      "mcpServers": {
        "cao": {
          "command": "npx",
          "args": ["cao-mcp-server", "--port", "3001"]
        }
      }
    }
  }
}
```

Once configured, the OpenClaw agent can call `list_terminals`, `get_terminal_status`, and `get_terminal_output` from any messaging platform.

### Optional: webhook endpoints for push notifications

For proactive notifications (rather than polling), CAO could expose optional webhook endpoints:

- **`/webhooks/status-change`** — Fires when a terminal session transitions state (e.g., running to idle, or error).
- **`/webhooks/task-complete`** — Fires when a long-running task finishes, including exit code and summary.

OpenClaw would register these webhooks and forward notifications to the appropriate messaging channel, enabling users to receive alerts without repeatedly querying status.
