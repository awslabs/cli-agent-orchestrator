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
| **Auth model** | OAuth via Anthropic account, same-user only | Needs custom auth layer (see below) |
| **Extensibility** | Limited to Claude Code capabilities | OpenClaw skills ecosystem (10,700+ community skills) + CAO MCP tools |

## What CAO Would Need

### MCP tools

CAO would expose MCP tools for both monitoring and remote control:

**Read tools** (safe to expose without confirmation):

- **`list_terminals`** — Returns all active terminal sessions with their IDs, provider names, and current status (idle / running / error).
- **`get_terminal_status`** — Given a terminal ID, returns detailed status: provider, working directory, whether a task is running, and elapsed time.
- **`get_terminal_output`** — Given a terminal ID and an optional line offset, returns recent terminal output (stdout/stderr). Supports pagination to avoid oversized responses.

**Write tools** (require authentication and authorization):

- **`send_terminal_input`** — Send a command or text input to a terminal session. This is the core remote-control primitive — equivalent to typing in the terminal.
- **`create_terminal`** — Spawn a new terminal session with a specified provider and working directory.
- **`stop_terminal`** — Gracefully stop or kill a running terminal session.

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

### Optional: webhook endpoints for push notifications

For proactive notifications (rather than polling), CAO could expose optional webhook endpoints:

- **`/webhooks/status-change`** — Fires when a terminal session transitions state (e.g., running to idle, or error).
- **`/webhooks/task-complete`** — Fires when a long-running task finishes, including exit code and summary.

OpenClaw would register these webhooks and forward notifications to the appropriate messaging channel, enabling users to receive alerts without repeatedly querying status.

## Authentication and Authorization

Remote control through a messaging gateway introduces a critical question: how do we verify who is sending commands and what they're allowed to do?

### The problem

Claude Code Remote Control sidesteps this — it uses OAuth through a single Anthropic account, so the remote user is always the same authenticated owner. CAO + OpenClaw is different: multiple users on Telegram/Slack could potentially send commands to the same CAO instance. Without auth, anyone in an allowed OpenClaw channel could spawn terminals or send arbitrary commands.

### OpenClaw's built-in auth (not sufficient)

OpenClaw provides **operator-level** security, not user-level:

- **Pairing codes** — unknown senders get a code that the operator must approve via `openclaw pairing approve`
- **Per-channel allowlists** — `allowFrom` lists of Telegram usernames, Slack user IDs, etc.
- **Execution approvals** — human-in-the-loop confirmation for shell commands
- **Tool allow/deny lists** — agent-level, not per-user

What's missing: no per-user RBAC (open issue [#8081](https://github.com/openclaw/openclaw/issues/8081)), no per-user tool scoping, no MCP tool-level authorization. All approved users on a channel get the same access.

### What the MCP spec provides

The MCP spec mandates **OAuth 2.1** for HTTP-based transports with PKCE, bearer tokens, and scope-based authorization. It also defines **tool annotations** (`readOnlyHint`, `destructiveHint`) but these are advisory UX hints, not security boundaries.

Relevant active proposals:
- **SEP-1880** — per-tool scope requirements (e.g., `cao:terminal.write`)
- **RFC 9396 integration** — rich authorization requests for per-invocation context

### Recommended approach: OAuth 2.1 + scope-based tool authorization

CAO's MCP server should implement the MCP spec's OAuth 2.1 flow with custom scopes:

| Scope | Grants access to |
|---|---|
| `cao:read` | `list_terminals`, `get_terminal_status`, `get_terminal_output` |
| `cao:write` | `send_terminal_input`, `create_terminal`, `stop_terminal` |
| `cao:admin` | All tools + webhook management |

**Identity flow through OpenClaw:**

```
Telegram/Slack user
  → OpenClaw identifies user (platform user ID + allowlist)
  → OpenClaw looks up or requests OAuth token for that user
  → Token included in MCP requests to CAO server
  → CAO validates token, checks scopes, executes tool
```

For the OAuth enrollment step, OpenClaw sends the user an authorization URL via chat. The user clicks, authorizes in a browser, and OpenClaw captures the resulting token and maps it to their chat identity.

### Alternative: API key + allowlist (simpler, less secure)

For single-operator deployments where OpenClaw and CAO run on the same machine:

1. CAO MCP server requires a static API key in the `Authorization` header
2. OpenClaw is the only client that knows the key
3. OpenClaw's own allowlist controls who can talk to the bot
4. All authorized users get the same access level

This is simpler but provides no per-user differentiation — it trusts OpenClaw's allowlist entirely.

### Open questions

- **Should CAO implement its own OAuth authorization server, or delegate to an external IdP** (Auth0, Keycloak, etc.)? External IdP is more robust but adds infrastructure.
- **How to handle the browser redirect in a chat-only context?** The user needs to click an OAuth link sent via Telegram/Slack. This is a known UX friction point for bot-based OAuth flows.
- **Per-terminal permissions?** Should a user only control terminals they created, or any terminal? This matters for shared team environments.
- **Rate limiting and audit logging** — write operations should be logged with the authenticated user identity for traceability.
