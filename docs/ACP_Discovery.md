# ACP Discovery: Agent Client Protocol and Its Implications for CAO

## Executive Summary

ACP (Agent Client Protocol) is an emerging standard that standardizes communication between code editors/IDEs and AI coding agents — analogous to LSP (Language Server Protocol), but for AI agents. All of CAO's providers (Claude Code, Codex, Gemini CLI, Kiro CLI, Kimi CLI) are listed in the ACP registry, meaning they all speak or can speak ACP.

ACP could fundamentally change CAO's architecture by replacing the tmux-based transport layer (the most fragile part of the system) with structured JSON-RPC communication. This would eliminate terminal scraping, ANSI parsing, idle prompt pattern matching, paste-buffer workarounds, and most provider-specific code. The MCP orchestration tools (`handoff`, `assign`, `send_message`, `check_inbox`) would remain unchanged — ACP and MCP are complementary protocols at different layers.

> Source: https://agentclientprotocol.com and https://kiro.dev/blog/kiro-adopts-acp/

---

## 1. What Is ACP

ACP standardizes how an **editor/client** communicates with an **AI coding agent**. The relationship is:

```
Client (IDE / Editor / Orchestrator) <── ACP ──> Agent (AI coding tool)
```

It is **not** an agent-to-agent protocol. ACP's design assumption (from the spec): *"users primarily work within their editor and invoke agents for specific assistance."*

### How ACP compares to MCP and A2A

| Protocol | Layer | Relationship | Transport | Purpose |
|----------|-------|-------------|-----------|---------|
| **ACP** | UI / Presentation | Client ↔ Agent | JSON-RPC over stdio (local), HTTP/WebSocket (remote, draft) | How an editor talks to an agent: send prompts, receive responses, handle diffs, manage permissions |
| **MCP** | Tool / Capability | Agent ↔ Tools | JSON-RPC over stdio, HTTP, SSE | How an agent accesses external tools and data sources |
| **A2A** | Coordination | Agent ↔ Agent | HTTP (JSON-RPC) | How agents discover and coordinate with each other |

These are **complementary, not competing**. An agent can be connected via ACP to a client, use MCP to access tools, and use A2A to coordinate with other agents — all simultaneously.

### The LSP analogy

LSP standardized editor↔language server communication so that any editor could work with any language server. Before LSP, every editor-language combination required custom integration. ACP does the same for AI agents: before ACP, every editor-agent combination required custom integration. With ACP, any ACP client can work with any ACP agent.

---

## 2. ACP Protocol Specification

### Transport

**Primary (finalized):** JSON-RPC 2.0 over stdio
- Client launches agent as a subprocess
- Agent reads JSON-RPC from stdin, writes to stdout
- Messages are newline-delimited, UTF-8 encoded
- Agent may log to stderr (client can capture or ignore)

**Secondary (draft):** Streamable HTTP for remote agents. Not finalized.

### Message types

Two JSON-RPC 2.0 message types:
- **Methods** (requests): Expect a response. Have an `id` field.
- **Notifications**: One-way, no response. No `id` field.

### Protocol lifecycle

```
1. Client sends `initialize` (version + capability negotiation)
2. Optional `authenticate` call
3. Client calls `session/new` or `session/load`
4. Client sends `session/prompt` with user message
5. Agent streams `session/update` notifications:
   - agent_message_chunk (streamed LLM text, Markdown)
   - agent_thought_chunk (chain-of-thought reasoning)
   - plan (execution plan with status per entry)
   - tool_call / tool_call_update (tool invocation progress)
   - available_commands_update (slash commands)
   - current_mode_update / config_option_update
6. Agent may call back to client:
   - session/request_permission (approve tool use)
   - fs/read_text_file / fs/write_text_file (file operations)
   - terminal/create / terminal/output / terminal/wait_for_exit (shell commands)
7. Agent finishes with a stop reason (end_turn, max_tokens, cancelled, refusal)
8. Repeat from step 4
```

### Capability negotiation

During `initialize`, both sides declare capabilities:

**Client capabilities:**
- `fs.readTextFile` / `fs.writeTextFile` — file system access (can return unsaved editor content)
- `terminal` — shell command execution

**Agent capabilities:**
- `loadSession` — can restore sessions
- `promptCapabilities.image`, `.audio`, `.embeddedContext` — content types
- `mcpCapabilities.http`, `.sse`, `.acp` — MCP transport support

All capabilities omitted are treated as unsupported.

### Session management

- `session/new` — create new session with working directory and MCP server configs
- `session/load` — restore session (replays conversation history)
- `session/fork` (draft) — create new session from existing one (sub-agents, PR descriptions)
- `session/resume` (draft) — lightweight reconnection without history replay
- `session/cancel` — cancel current prompt

Session modes: Ask Mode, Architect Mode, Code Mode (switchable by client or agent).

### Sending a prompt

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_abc123",
    "prompt": [
      {"type": "text", "text": "Fix the authentication bug"}
    ]
  }
}
```

The `prompt` field is an array of `ContentBlock` objects (text, image, audio, resource, resource_link).

### Receiving responses

The agent streams multiple `session/update` notifications. Each has a `type` field:

| Update type | Content |
|-------------|---------|
| `agent_message_chunk` | Streamed text from the LLM (Markdown by default) |
| `agent_thought_chunk` | Internal reasoning / chain-of-thought |
| `plan` | Execution plan entries with priority and status |
| `tool_call` | Tool invocation started (pending) |
| `tool_call_update` | Tool progress/completion with content (text, diffs, terminal output) |
| `user_message_chunk` | Replay of user messages during session load |

### Stop reasons

When the agent finishes:
- `end_turn` — completed normally
- `max_tokens` — hit token limit
- `max_turn_requests` — hit model request limit
- `refusal` — agent declined
- `cancelled` — client cancelled via `session/cancel`

### Tool calls

ACP tool calls are **informational notifications from agent to client** — they report what the agent is doing, not request tools from the client (that's MCP's role).

Tool call lifecycle:
```
Agent decides to use a tool
  → session/update type=tool_call (status: pending)
  → Agent MAY call session/request_permission
  → Client responds with allow/reject
  → session/update type=tool_call_update (status: executing)
  → session/update type=tool_call_update (status: completed/failed)
```

Eight tool kind categories for UI optimization: `read`, `edit`, `delete`, `move`, `search`, `fetch`, `execute`, `think`, `other`.

Tool calls can produce: text content, file diffs (`{oldText, newText, path}`), or terminal output (via `terminalId`).

### Permission model

Before executing a tool, the agent may call `session/request_permission`:
```json
{
  "method": "session/request_permission",
  "params": {
    "title": "Edit file auth.py",
    "options": [
      {"optionId": "1", "name": "Allow once", "kind": "allow_once"},
      {"optionId": "2", "name": "Always allow", "kind": "allow_always"},
      {"optionId": "3", "name": "Reject", "kind": "reject_once"}
    ]
  }
}
```

### Terminal access

Agents can execute shell commands through client-provided terminals (requires `clientCapabilities.terminal`):

| Method | Purpose |
|--------|---------|
| `terminal/create` | Create and start a terminal command (async, returns `terminalId`) |
| `terminal/output` | Non-blocking read of accumulated output |
| `terminal/wait_for_exit` | Blocking wait until command finishes |
| `terminal/kill` | Force-terminate |
| `terminal/release` | Cleanup and invalidate |

### File system access

Requires `clientCapabilities.fs.readTextFile` / `fs.writeTextFile`:

| Method | Purpose |
|--------|---------|
| `fs/read_text_file` | Read file content (can return unsaved editor changes) |
| `fs/write_text_file` | Write/create file |

### MCP integration

ACP and MCP work together. When creating a session, the client passes MCP server configurations:

```json
{
  "method": "session/new",
  "params": {
    "cwd": "/path/to/project",
    "mcpServers": [
      {"name": "cao-tools", "transport": "stdio", "command": "cao-mcp-server"}
    ]
  }
}
```

The agent connects to those MCP servers directly. ACP reuses *"JSON representations used in MCP where possible"* and *"mirrors MCP structures to enable seamless forwarding."*

A draft proposal (MCP-over-ACP) would tunnel MCP traffic through the ACP connection itself, eliminating the need for separate MCP server processes.

---

## 3. Agents in the ACP Ecosystem

### ACP Registry (confirmed agents)

Source: `https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json`

| Agent | Version | Distribution | CAO Provider? |
|-------|---------|-------------|---------------|
| **Claude Code** | v0.16.0 | `@zed-industries/claude-code-acp@0.16.0` (Zed adapter wrapper) | Yes |
| **Codex CLI** | v0.9.2 | Binary releases (Darwin/Linux/Windows) | Yes |
| **Gemini CLI** | v0.27.3 | `@google/gemini-cli@0.27.3` | Yes |
| **Kimi CLI** | v1.9.0 | Binary releases | Yes |
| **GitHub Copilot** | v1.425.0 | `@github/copilot-language-server@1.425.0` | No |
| **JetBrains Junie** | v704.1.0 | `@jetbrains/junie-cli@704.1.0` | No |
| **Mistral Vibe** | v2.0.2 | Binary releases (Apache-2.0) | No |
| **Auggie CLI** | v0.15.0 | `@augmentcode/auggie@0.15.0` | No |
| **OpenCode** | v1.1.53 | Binary releases (MIT) | No |
| **Qoder CLI** | v0.1.26 | `@qoder-ai/qodercli@0.1.26` | No |
| **Qwen Code** | v0.10.0 | `@qwen-code/qwen-code@0.10.0` | No |
| **Factory Droid** | v0.56.3 | Binary releases | No |

### Additional agents (listed on website, not in registry)

AgentPool, Blackbox AI, Cline, Code Assistant, Docker cagent, fast-agent, Goose, **Kiro CLI**, Minion Code, OpenHands, Pi, Stakpak, VT Code

### Supported ACP clients (IDEs/editors)

Eclipse, Emacs, JetBrains IDEs, Neovim, Toad, Zed

### CAO provider ACP support status

| Provider | ACP support | Notes |
|----------|------------|-------|
| **Claude Code** | Via Zed adapter wrapper | Not native — `@zed-industries/claude-code-acp` wraps CLI to speak ACP |
| **Codex CLI** | Native | In registry with binary releases |
| **Gemini CLI** | Native | In registry, reference implementation for ACP |
| **Kiro CLI** | Native | Listed on website, config via `~/.jetbrains/acp.json` with `kiro-cli acp` |
| **Kimi CLI** | Native | In registry with binary releases |
| **Q CLI** | Not listed | Not in registry or website |

---

## 4. How ACP Could Help CAO

### 4.1 Current CAO architecture (what ACP would replace)

CAO currently communicates with agents through a **tmux-based transport layer**:

```
CAO Server
  │
  ├── tmux send-keys / paste-buffer  ──→  Agent CLI (running in tmux pane)
  │
  ├── tmux capture-pane  ←──────────────  Agent terminal output (raw ANSI)
  │
  ├── tail + regex on log files  ←──────  Idle detection (watchdog)
  │
  └── Provider-specific code:
        get_status()                     → regex on terminal output
        extract_last_message_from_script() → parse ANSI, find response markers
        _build_command()                 → CLI flags, temp config files
        initialize()                     → wait_for_shell + send command + wait_until_status
        exit_cli()                       → "/exit" or Ctrl-D
```

This works but is the **most fragile part of CAO**. Most bugs are in this layer:
- Idle prompt patterns vary by provider (✨ for Kimi, `>` for Codex, `$` for shell)
- ANSI escape sequences must be stripped
- `paste_enter_count` workarounds for TUI agents
- Terminal scroll position affects `capture-pane` output
- Processing indicators differ (💫, spinner text, `Working...`)
- Response extraction depends on finding markers in raw terminal text
- Each new provider requires ~250 lines of provider-specific scraping code

### 4.2 ACP-based CAO architecture (what it would look like)

```
CAO Server (ACP Client)
  │
  ├── session/prompt (JSON-RPC over stdio)  ──→  Agent (ACP subprocess)
  │
  ├── session/update notifications  ←──────────  Structured responses (Markdown)
  │       agent_message_chunk                     Clean text, no ANSI
  │       tool_call / tool_call_update            Tool progress with diffs
  │       plan                                    Execution plans
  │
  ├── Stop reasons  ←─────────────────────────  end_turn, cancelled, refusal
  │       (replaces idle detection entirely)
  │
  └── MCP servers passed via session/new:
        handoff, assign, send_message, check_inbox
        (unchanged — same MCP tools, different transport to agent)
```

### 4.3 What ACP replaces vs what stays

| Current (tmux-based) | ACP-based | Impact |
|---|---|---|
| Launch agent in tmux pane via `_build_command()` | Launch agent as ACP subprocess via stdio | Eliminates tmux dependency for agent lifecycle |
| Detect status via regex on terminal output (`get_status()`) | Receive structured stop reasons (`end_turn`, `cancelled`) | Eliminates all idle prompt patterns, processing indicator regexes |
| Send input via `tmux send-keys` / paste-buffer | Send `session/prompt` JSON-RPC message | Eliminates `paste_enter_count`, paste-buffer workarounds, Enter key timing |
| Extract response by parsing ANSI output (`extract_last_message_from_script()`) | Receive `agent_message_chunk` notifications (clean Markdown) | Eliminates ANSI stripping, response marker detection, scroll position issues |
| Provider-specific idle patterns (✨, `>`, `$`) | Capability negotiation during `initialize` | Eliminates per-provider pattern maintenance |
| Watchdog polls log files + `tmux capture-pane` for idle detection | Stop reasons tell CAO exactly when agent is done | Eliminates watchdog entirely |
| ~250 lines of provider-specific code per provider | Thin ACP client adapter (~50 lines, mostly shared) | ~80% reduction in provider code |
| **MCP orchestration tools** (`handoff`, `assign`, `send_message`, `check_inbox`) | **Same** — passed via `session/new` MCP server config | No change |
| **CAO REST API** (sessions, terminals, input, output) | **Same** — API layer unchanged | No change |
| **SQLite database** (terminals, inbox messages) | **Same** | No change |

### 4.4 Specific problems ACP solves for CAO

**Problem 1: Status detection is fragile**

Current: Each provider has a `get_status()` method that runs regex patterns against raw terminal output. Patterns are different for every CLI (Kimi uses ✨, Codex uses `>`, Gemini has a TUI with spinner). New providers require capturing real terminal output and writing new patterns. Status detection fails when terminal scroll position changes, when the TUI redraws, or when the prompt character appears in response text.

ACP solution: The agent sends a structured stop reason (`end_turn`, `cancelled`, `refusal`, `max_tokens`) when it finishes. No regex. No terminal scraping. The status is unambiguous.

**Problem 2: Sending input is unreliable**

Current: CAO uses `tmux send-keys` or `tmux load-buffer` + `tmux paste-buffer` to type text into the agent's terminal. This requires tuning `paste_enter_count` (how many Enter keys to send after pasting), dealing with TUI agents that interpret paste differently, and handling multi-line input that can break if the terminal width causes wrapping.

ACP solution: CAO sends `session/prompt` with the text as a JSON field. No pasting, no Enter keys, no terminal width issues.

**Problem 3: Extracting responses requires parsing raw terminal output**

Current: Each provider has `extract_last_message_from_script()` that parses the terminal's scrollback buffer to find where the agent's response starts and ends. This involves stripping ANSI escape codes, finding response markers (which differ by provider), and handling cases where the response spans multiple screen pages.

ACP solution: The agent streams `agent_message_chunk` notifications with clean Markdown text. CAO concatenates chunks. No parsing, no ANSI, no marker detection.

**Problem 4: Each new provider requires ~250 lines of scraping code**

Current: Adding Kimi CLI required writing `kimi_cli.py` with idle patterns, processing patterns, response extraction, command building, initialization, cleanup, and exit. Most of this code is terminal scraping specific to Kimi's TUI layout.

ACP solution: A thin ACP adapter shared across all providers. Provider-specific code reduces to: the command to launch the agent and any agent-specific session configuration. The protocol handles everything else.

**Problem 5: Watchdog for idle detection is expensive**

Current: The watchdog runs `tail -n 100` on log files and `tmux capture-pane` every 5 seconds per terminal to detect when agents go idle. At 50 agents, that's ~100 subprocess calls per cycle.

ACP solution: Stop reasons are pushed by the agent when it finishes. No polling needed. CAO knows exactly when the agent is done because the agent tells it via the protocol.

### 4.5 What ACP does NOT solve

- **Agent-to-agent messaging** — ACP has no concept of agents talking to each other. The `check_inbox` / `send_message` MCP tools are still needed.
- **Multi-agent coordination** — ACP is 1:1 (one client, one agent). CAO's orchestration (multiple agents in a session, handoff, assign) is handled by MCP tools and the CAO server, not ACP.
- **Agent discovery** — ACP doesn't help CAO find or select agents. CAO's provider registry and agent profiles serve this purpose.
- **Message queuing** — The asyncio.Queue / RabbitMQ design from `check-inbox-design.md` is still needed for the scatter-gather inbox.

### 4.6 The hybrid architecture

The recommended architecture uses ACP for agent transport and MCP for orchestration tools:

```
┌──────────────────────────────────────────────────────────────┐
│                     CAO Server (FastAPI :9889)                 │
│                                                                │
│  ┌────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│  │  REST API   │   │ ACP Client    │   │ Message Queue     │   │
│  │            │   │ Manager       │   │ (asyncio.Queue)   │   │
│  │ POST /input│──>│              │   │                   │   │
│  │ GET /output│<──│ session/prompt│   │ check_inbox ←─────│   │
│  │ GET /status│<──│ stop reasons  │   │ send_message ────>│   │
│  └────────────┘   └──────┬───────┘   └───────────────────┘   │
│                          │                                    │
│              ┌───────────┼───────────┐                        │
│              │           │           │                        │
│         ┌────▼────┐ ┌────▼────┐ ┌────▼────┐                  │
│         │ Agent 1  │ │ Agent 2  │ │ Agent 3  │                  │
│         │ (ACP     │ │ (ACP     │ │ (ACP     │                  │
│         │ subprocess)│ │ subprocess)│ │ subprocess)│              │
│         └────┬────┘ └────┬────┘ └────┬────┘                  │
│              │           │           │                        │
│         ┌────▼────┐ ┌────▼────┐ ┌────▼────┐                  │
│         │ MCP Srv  │ │ MCP Srv  │ │ MCP Srv  │                  │
│         │(handoff, │ │(handoff, │ │(handoff, │                  │
│         │ assign,  │ │ assign,  │ │ assign,  │                  │
│         │ send_msg,│ │ send_msg,│ │ send_msg,│                  │
│         │check_inbx)│ │check_inbx)│ │check_inbx)│              │
│         └─────────┘ └─────────┘ └─────────┘                  │
└──────────────────────────────────────────────────────────────┘
```

**Key change:** The arrow between CAO Server and agents changes from `tmux send-keys / capture-pane` to `ACP JSON-RPC over stdio`. Everything else (REST API, MCP tools, message queue) stays the same.

---

## 5. Implementation Considerations

### 5.1 Python SDK

ACP has an official Python SDK:

```
pip install agent-client-protocol
```

Provides Pydantic models, async base classes, and JSON-RPC plumbing. Includes examples for both agent and client implementations. Gemini CLI is the reference production implementation.

Other SDKs: TypeScript (`@agentclientprotocol/sdk`), Rust (`agent-client-protocol`), Kotlin, plus community SDKs (Go, Dart, Swift, Emacs).

### 5.2 What CAO would need to implement

CAO would act as an **ACP client**. The client responsibilities:

| Responsibility | What CAO needs to do |
|---|---|
| Launch agent subprocess | Spawn process with stdin/stdout pipes (replace tmux) |
| Send `initialize` | Declare client capabilities (fs, terminal) |
| Create sessions | `session/new` with working directory + MCP server configs |
| Send prompts | `session/prompt` with content blocks |
| Receive responses | Process `session/update` stream, concatenate message chunks |
| Handle permission requests | Auto-approve (CAO runs in `--yolo` / auto-approve mode) |
| Provide file system | Implement `fs/read_text_file`, `fs/write_text_file` (delegate to actual filesystem) |
| Provide terminals | Implement `terminal/create`, `terminal/output`, etc. (delegate to subprocess) |
| Detect completion | Read stop reason from `session/prompt` response |
| Manage sessions | Track session IDs, handle cleanup |

### 5.3 Impact on existing code

| Component | Current | After ACP | Change |
|---|---|---|---|
| `providers/base.py` | BaseProvider ABC with tmux methods | AcpProvider base with JSON-RPC methods | Major rewrite |
| `providers/claude_code.py` | ~250 lines of terminal scraping | ~50 lines (launch command + config) | ~80% reduction |
| `providers/kimi_cli.py` | ~250 lines of terminal scraping | ~50 lines (launch command + config) | ~80% reduction |
| `providers/gemini_cli.py` | ~300 lines of terminal scraping | ~50 lines (launch command + config) | ~80% reduction |
| `providers/codex.py` | ~250 lines of terminal scraping | ~50 lines (launch command + config) | ~80% reduction |
| `providers/kiro_cli.py` | ~250 lines of terminal scraping | ~50 lines (launch command + config) | ~80% reduction |
| `clients/tmux.py` | TmuxClient for terminal operations | Not needed for agent comm (keep for user-facing terminal display) | Reduced scope |
| `services/inbox_service.py` | Watchdog + LogFileHandler | Removed (stop reasons replace idle detection) | Deleted |
| `utils/terminal.py` | `wait_for_shell`, `wait_until_status` | Replaced by ACP session lifecycle | Simplified |
| `mcp_server/server.py` | Orchestration tools | Unchanged | No change |
| `api/main.py` | REST API | Unchanged (translates HTTP to ACP internally) | Minor wiring |

### 5.4 Risks and unknowns

| Risk | Detail | Mitigation |
|---|---|---|
| Claude Code ACP is not native | Requires `@zed-industries/claude-code-acp` Zed adapter wrapper | Test thoroughly; may need to keep tmux fallback for Claude Code |
| Q CLI has no ACP support | Not in registry or website | Keep tmux provider for Q CLI; adopt ACP for other 5 providers |
| ACP remote transport is draft | HTTP/WebSocket not finalized | Use stdio (local subprocess) — sufficient for CAO's single-machine model |
| Protocol maturity | Many features still in RFD (session fork, resume, proxy chains) | Use only finalized spec features: initialize, session/new, session/prompt, session/update |
| Governance | Zed Industries governs ACP, moving toward foundation | Monitor governance changes; ACP is open-source with multi-vendor adoption |
| Agent quirks over ACP | Agents may behave differently over ACP vs their native TUI | E2E test each provider in ACP mode |

### 5.5 Migration strategy

A gradual migration, not a big-bang rewrite:

**Phase 1: ACP client library**
- Implement `AcpClientManager` in CAO using the Python SDK
- Support: initialize, session/new, session/prompt, session/update
- Unit tests with mock agent

**Phase 2: Dual-mode providers**
- Add `transport` field to provider config: `tmux` (default) or `acp`
- Implement ACP transport for one provider (Gemini CLI — reference implementation)
- E2E test: Gemini CLI via ACP vs tmux — verify identical behavior

**Phase 3: Provider migration**
- Migrate providers one by one: Codex → Kimi CLI → Kiro CLI → Claude Code
- Keep tmux fallback for providers with ACP issues
- Q CLI stays tmux-only (no ACP support)

**Phase 4: Tmux deprecation**
- Once all ACP providers pass E2E tests, deprecate tmux as default transport
- Keep tmux as fallback for providers without ACP support

---

## 6. ACP and the Inbox Problem

ACP does **not** solve the supervisor inbox problem described in `check-inbox-design.md`. The inbox problem is about agent-to-agent messaging; ACP is about client-to-agent communication.

However, ACP makes the inbox solution **simpler to implement**:

| Inbox concern | With tmux | With ACP |
|---|---|---|
| Detecting when supervisor is idle (for message delivery) | Watchdog polls terminal output every 5s | Stop reason `end_turn` tells CAO immediately |
| Delivering messages to supervisor | `tmux send-keys` (types raw text into terminal) | `session/prompt` (sends structured JSON) |
| Knowing when worker finishes | `wait_until_terminal_status` polls API | Stop reason from worker's ACP session |
| Extracting worker output for `handoff` | Parse terminal scrollback (`extract_last_message_from_script`) | Concatenate `agent_message_chunk` notifications |

With ACP, the `check_inbox` design becomes the **only** delivery path. The watchdog removal (Section 4.5 of check-inbox-design.md) happens naturally — ACP's structured stop reasons eliminate the need for idle detection polling.

**Recommendation:** Implement `check_inbox` first (MCP tool level, works with current tmux architecture). Then adopt ACP (transport level, replaces tmux). The two are independent but ACP makes the inbox solution cleaner.

---

## 7. Conclusion

ACP represents a significant opportunity for CAO:

1. **Eliminates the most fragile code** — terminal scraping, ANSI parsing, idle prompt patterns, paste-buffer workarounds
2. **Reduces provider code by ~80%** — from ~250 lines per provider to ~50 lines
3. **All CAO providers support ACP** — Codex, Gemini CLI, Kimi CLI, Kiro CLI natively; Claude Code via adapter
4. **Python SDK exists** — `pip install agent-client-protocol`
5. **Complementary to MCP** — orchestration tools (handoff, assign, send_message, check_inbox) are unchanged
6. **Gradual migration possible** — dual-mode providers, one-by-one migration, tmux fallback

The recommended sequence:
1. Ship `check_inbox` (fixes the immediate scatter-gather problem, works with tmux)
2. Evaluate ACP adoption (fixes the transport layer, eliminates provider bugs)
3. These are independent initiatives that both improve CAO's architecture
