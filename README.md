# CLI Agent Orchestrator (CAO)

[![PyPI version](https://img.shields.io/pypi/v/cli-agent-orchestrator.svg)](https://pypi.org/project/cli-agent-orchestrator/)
[![Python versions](https://img.shields.io/pypi/pyversions/cli-agent-orchestrator.svg)](https://pypi.org/project/cli-agent-orchestrator/)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/awslabs/cli-agent-orchestrator)

**CLI Agent Orchestrator (CAO)** is an open-source multi-agent orchestration framework for AI coding CLIs — Claude Code, Kiro CLI, Codex CLI, Antigravity CLI, Hermes Agent, Kimi CLI, GitHub Copilot CLI, OpenCode, and Cursor CLI. CAO runs each agent in an isolated tmux session and coordinates them with a supervisor–worker pattern over the Model Context Protocol (MCP), so one supervisor agent can delegate tasks to multiple specialist agents in parallel, sequentially, or as a swarm.

## What is CAO?

CAO (pronounced "kay-oh") is a lightweight local orchestrator that sits between you and the CLI coding agents you already use. Instead of running a single agent at a time, CAO lets a supervisor agent launch, message, and coordinate multiple worker agents — each one a real CLI tool (Claude Code, Kiro, Codex, etc.) running in its own tmux terminal. Agents communicate through MCP-exposed primitives (**handoff**, **assign**, and **send_message**) and are managed via a CLI, a bundled Web UI, or an MCP management server. Because every agent is a full CLI process, CAO preserves tool behaviour, auth, and advanced features (Claude Code sub-agents, Kiro CLI custom agents, etc.) that a raw API wrapper cannot.

## Common use cases

- **Parallel code review / implementation** — supervisor assigns N reviewers to review N files concurrently, then merges their findings.
- **Cross-provider workflows** — supervisor on one CLI (e.g. Kiro), worker on another (e.g. Claude Code), per-profile provider selection.
- **Scheduled agent runs** — cron-style "every morning at 9am" triggers via [Flows](docs/flows.md).
- **Headless agent execution in CI** — `cao launch --headless --async` to run tasks unattended.
- **Multi-agent swarms with HITL** — humans can attach to any tmux session to intervene or steer.
- **Agent-driven agent management** — a primary agent uses [`cao-ops-mcp`](#cao-ops-mcp-server) to spawn and monitor CAO sessions from its own chat loop.

## Hierarchical Multi-Agent System

CAO implements a hierarchical multi-agent system — one supervisor agent delegates to specialised worker agents rather than running everything in a single context.

![CAO architecture: supervisor agent delegating to worker agents in isolated tmux sessions via MCP](./docs/assets/cao_architecture.png)

### Key Features

- **Hierarchical supervisor–worker orchestration** — a supervisor agent coordinates and delegates; workers focus on their domain. Preserves overall context without polluting workers.
- **Session isolation via tmux** — every agent runs in its own tmux session. Clean context separation, real PTY access, humans can `tmux attach` to steer at any time.
- **Orchestration primitives over MCP** — `handoff` (sync, wait for completion), `assign` (async, fire-and-forget), and `send_message` (inbox delivery between agents). Hermes workers also use `answer_user_prompt` for structured approval and clarify prompts; other providers may fall back to ordinary text delivery until they implement equivalent prompt states. See [Multi-Agent Orchestration](#multi-agent-orchestration).
- **Cross-provider mixing** — run workers on different CLIs in the same session. Pin a profile to a provider via agent frontmatter. See [Cross-Provider Orchestration](#cross-provider-orchestration).
- **Scheduled flows** — cron-like scheduling for unattended agent runs. See [docs/flows.md](docs/flows.md).
- **Web UI, CLI, and MCP control planes** — manage sessions from the browser, `cao session` commands, or the `cao-ops-mcp` server. See [docs/control-planes.md](docs/control-planes.md).
- **Tool restrictions per agent** — `role` + `allowedTools` in the profile, translated to each provider's native enforcement where available. See [docs/tool-restrictions.md](docs/tool-restrictions.md).
- **Persistent agent memory** — agents store and recall knowledge across sessions using `memory_store` and `memory_recall` MCP tools. CAO automatically injects relevant memories as context at session start. See [docs/memory.md](docs/memory.md).
- **Direct worker steering** — unlike traditional "sub-agent" features, you can attach to a running worker and intervene mid-task.
- **Full CLI feature access** — agents keep native CLI features: Claude Code [sub-agents](https://docs.claude.com/en/docs/claude-code/sub-agents), Kiro CLI custom agents, provider-native auth, etc.
- **Plugin system for outbound events** — forward inter-agent messages to Discord, Slack, Telegram, or any webhook target. See [Plugins](#plugins).

For detailed project structure and architecture, see [CODEBASE.md](CODEBASE.md).

## Installation

### Requirements

- **curl** and **git** — for downloading installers and cloning the repo
- **Python 3.10 or higher** — see [pyproject.toml](pyproject.toml)
- **tmux 3.3+** — used for agent session isolation
- **[uv](https://docs.astral.sh/uv/)** — fast Python package installer and virtual environment manager

### 1. Install Python 3.10+

```bash
# macOS (Homebrew)
brew install python@3.12

# Ubuntu/Debian
sudo apt update && sudo apt install python3.12 python3.12-venv

# Amazon Linux 2023 / Fedora
sudo dnf install python3.12
```

Verify:

```bash
python3 --version   # 3.10 or higher
```

> We recommend using [uv](https://docs.astral.sh/uv/) rather than a system-wide Python install like Anaconda. `uv` handles virtual environments and Python version resolution per-project.

### 2. Install tmux (3.3+)

```bash
bash <(curl -s https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/refs/heads/main/tmux-install.sh)
```

### 3. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # Add uv to PATH (or restart your shell)
```

### 4. Install CLI Agent Orchestrator

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main --upgrade
```

This pulls the latest `main` commit and includes the pre-built Web UI inside the wheel, so **you do not need Node.js or `npm install` to use CAO**. Node.js is only required if you plan to run the frontend in dev mode (hot-reload) or rebuild the bundle yourself — see [docs/web-ui.md](docs/web-ui.md).

#### Install from PyPI (optional)

PyPI publishes tagged releases only, so it will lag behind `main` between releases. Prefer the `git+` install above if you want the latest fixes.

```bash
uv tool install cli-agent-orchestrator --upgrade

# Pin a specific release
uv tool install cli-agent-orchestrator==2.1.0
```

For local development (`git clone` + `uv sync`) and the testing/quality workflow, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Devcontainer Feature

CAO includes an official devcontainer feature for container-native installation.

- Usage and options: [docs/devcontainer-feature.md](docs/devcontainer-feature.md)
- Local validation commands: [docs/devcontainer-feature.md#validation](docs/devcontainer-feature.md#validation)
- Release plan: [docs/devcontainer-feature.md#release-plan](docs/devcontainer-feature.md#release-plan)

## Prerequisite: a CLI agent tool

CAO drives existing CLI agent tools — it does not replace them. Before using CAO, install at least one of the following. You can install more than one and mix them in the same orchestration.

| Provider | Documentation | Authentication |
|----------|---------------|----------------|
| **Kiro CLI** (default) | [Provider docs](docs/kiro-cli.md) · [Installation](https://kiro.dev/docs/kiro-cli) | AWS credentials |
| **Claude Code** | [Provider docs](docs/claude-code.md) · [Installation](https://docs.anthropic.com/en/docs/claude-code/getting-started) | Anthropic API key |
| **Codex CLI** | [Provider docs](docs/codex-cli.md) · [Installation](https://github.com/openai/codex) | OpenAI API key |
| **Hermes Agent** | [Provider docs](docs/hermes.md) | Hermes auth; optional `hermesProfile` wrapper; configure `cao-mcp-server` in the selected Hermes profile for orchestration tools |
| **Kimi CLI** | [Provider docs](docs/kimi-cli.md) · [Installation](https://platform.moonshot.cn/docs/kimi-cli) | Moonshot API key |
| **GitHub Copilot CLI** | [Provider docs](docs/copilot-cli.md) · [Installation](https://github.com/features/copilot/cli) | GitHub auth |
| **OpenCode CLI** *(experimental — temporary inbox polling fallback for multi-agent callbacks, [#203](https://github.com/awslabs/cli-agent-orchestrator/issues/203))* | [Provider docs](docs/opencode-cli.md) · [Installation](https://opencode.ai) | Per-model API key |
| **Cursor CLI** | [Provider docs](docs/cursor-cli.md) · [Installation](https://cursor.com/cli) | Cursor subscription / API key |
| **Antigravity CLI** | [Provider docs](docs/antigravity-cli.md) · [Installation](https://antigravity.google) | Google account (shared with the Antigravity IDE login) |

## Quick Start

### 1. Install agent profiles

```bash
cao install code_supervisor      # the supervisor that delegates to workers
cao install developer            # optional worker
cao install reviewer             # optional worker
```

You can also install agents from local files or URLs:

```bash
cao install ./my-custom-agent.md
cao install https://example.com/agents/custom-agent.md
```

For creating custom agent profiles, see [docs/agent-profile.md](docs/agent-profile.md).

#### Profile management (`cao profile`)

```bash
cao profile list                                    # List all installed profiles
cao profile show <name|file>                        # Inspect frontmatter details
cao profile validate <name|file>                    # Schema + deprecation checks
cao profile templates                               # List scaffolding templates
cao profile create -t aws/stepfunction -c config.json  # Generate from template
cao profile remove <name>                           # Delete from local store
```

### 2. Start the server

```bash
cao-server
```

### 3. Launch the supervisor

In another terminal:

```bash
cao launch --agents code_supervisor

# Or specify a provider
cao launch --agents code_supervisor --provider claude_code
# Valid: kiro_cli | claude_code | codex | antigravity_cli | hermes | kimi_cli | copilot_cli | opencode_cli | cursor_cli

# Unrestricted access, skip confirmation (DANGEROUS)
cao launch --agents code_supervisor --yolo
```

The supervisor coordinates and delegates tasks to worker agents using the orchestration patterns.

### 4. Shutdown

```bash
cao shutdown --all                      # shut down every CAO session
cao shutdown --session cao-my-session   # shut down a specific session
```

### Sessions run in tmux

All agent sessions run in tmux — you can `tmux attach -t <session-name>` to watch agents in real time. For the full list of tmux shortcuts and the interactive window selector, see [docs/tmux.md](docs/tmux.md).

CAO also supports [herdr](https://herdr.dev/) as an experimental alternative backend. herdr is agent-aware, so it replaces tmux output polling with real-time status events. For setup and configuration, see [docs/herdr.md](docs/herdr.md).

## Web UI

CAO ships a bundled web dashboard for managing agents, terminals, and flows from the browser. The pre-built UI is packaged inside the wheel, so there is nothing extra to install — just start the server:

```bash
cao-server
```

Then open http://localhost:9889.

![CAO Web UI](https://github.com/user-attachments/assets/e7db9261-62b1-4422-b9f5-6fe5f65bdea4)

For hot-reload dev mode, remote access over SSH, and rebuilding the frontend from source (only these require Node.js), see [docs/web-ui.md](docs/web-ui.md). For frontend architecture, see [web/README.md](web/README.md).

## MCP Apps — host-rendered fleet UI

Beyond the browser dashboard, CAO can render its fleet UI **inside an MCP App-capable host** (Claude / Claude Desktop, ChatGPT, VS Code GitHub Copilot, Microsoft 365 Copilot, Goose, Postman, MCPJam, Archestra.AI — see the [client matrix](https://modelcontextprotocol.io/extensions/client-matrix)) using the [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) extension (SEP-1865) — so you observe and steer agents without leaving your chat host. It ships three single-file views (`ui://cao/dashboard`, `ui://cao/agent`, `ui://cao/event-stream`) plus a build-free topology widget, backed by an in-process event ring buffer and a single audited `submit_command` mutation path.

![CAO MCP Apps — fleet dashboard rendered in an MCP App host](docs/media/mcp-apps-dashboard.png)

*The fleet dashboard rendered from the built `ui://cao/dashboard` bundle. Full motion walk-through (dashboard → agent detail → live event stream): [`docs/media/mcp-apps-demo.webm`](docs/media/mcp-apps-demo.webm). Regenerate via `cd cao_mcp_apps && npm run build:all && node scripts/record-demo.mjs`.*

It is **default-off and behavior-preserving** — packaged as the built-in `mcp_apps` plugin and registered only when enabled:

```bash
export CAO_MCP_APPS_ENABLED=true
cao-server        # REST + SSE /events on :9889
cao-mcp-server    # registers the MCP App tools/resources for your host
```

New in this area: `src/cli_agent_orchestrator/ext_apps/` (resources + topology widget), `cao_mcp_apps/` (JIT-free React views), `src/cli_agent_orchestrator/plugins/builtin/mcp_apps.py` (the plugin), with docs in [docs/mcp-apps.md](docs/mcp-apps.md), a worked example in [examples/mcp-apps/](examples/mcp-apps/), and the [skills/cao-mcp-apps](skills/cao-mcp-apps/SKILL.md) operator playbook. Optional default-off OAuth 2.1 scopes (`cao:read`/`cao:write`/`cao:admin`) gate mutations when an IdP is configured.

The single-file views are built from source under `cao_mcp_apps/` — see [cao_mcp_apps/README.md](cao_mcp_apps/README.md) for the dev workflow (build, test, and the CI coverage/JIT/bundle-size gates). Node.js is only needed to rebuild them, not to run CAO.

## Multi-Agent Orchestration

CAO agents coordinate through a local HTTP server (default `localhost:9889`). CLI agents reach it via MCP tools to route messages, track status, and drive orchestration.

Each agent terminal is assigned a unique `CAO_TERMINAL_ID` environment variable. The server uses this ID to route messages, track terminal status (IDLE / PROCESSING / COMPLETED / ERROR), and coordinate operations. When an agent calls an MCP tool, the server identifies the caller by their `CAO_TERMINAL_ID` and orchestrates accordingly.

### Orchestration Modes

> **Note:** All orchestration modes support an optional `working_directory` parameter when enabled via `CAO_ENABLE_WORKING_DIRECTORY=true`. See [docs/working-directory.md](docs/working-directory.md).

**1. Handoff** — transfer control to another agent and wait for completion.

- Creates a new terminal with the specified agent profile
- Sends the task message and waits for the agent to finish
- Returns the agent's output to the caller and exits the agent
- **Automatically deletes the worker terminal on success** — the scrollback and metadata are saved to `~/.cao/logs/terminal/` before deletion, so you can restore the terminal for debugging with `cao terminal restore <terminal_id>` as long as the session still exists. See [docs/terminal-lifecycle.md](docs/terminal-lifecycle.md) for the full lifecycle, snapshot schema, and restore semantics.
- Use when you need **synchronous** execution with results

Example: sequential code review workflow.

![Handoff Workflow](./docs/assets/handoff-workflow.png)

**2. Assign** — spawn an agent to work independently (async).

- Creates a new terminal, sends the task with callback instructions, returns immediately
- The supervisor's terminal ID is appended to the task message automatically (disable with `CAO_ENABLE_SENDER_ID_INJECTION=false`), and recorded on the worker terminal so callbacks route structurally
- The assigned agent sends results back via `send_message` when done — omitting `receiver_id` routes the reply to the assigning terminal; messages queue if the supervisor is busy
- Use for **asynchronous** execution or fire-and-forget operations

Example: a supervisor assigns parallel data-analysis tasks to multiple analysts while using handoff to generate a report template, then combines results. See [examples/assign](examples/assign).

![Parallel Data Analysis](./docs/assets/parallel-data-analysis.png)

**3. Send Message** — communicate with an existing agent.

- Sends a message to a specific terminal's inbox; delivered when the terminal is idle
- Enables ongoing collaboration and multi-turn conversations
- Common in **swarm** operations
- Supports [eager delivery](docs/inbox-delivery.md) for providers that buffer input during processing (eliminates inter-turn latency)

Example: multi-role feature development.

![Multi-role Feature Development](./docs/assets/multi-role-feature-development.png)

### Cross-Provider Orchestration

Workers inherit the provider of the terminal that spawned them by default. To pin a profile to a specific provider, add `provider` to its frontmatter:

```markdown
---
name: developer
provider: claude_code
---
```

Valid values: `kiro_cli`, `claude_code`, `codex`, `antigravity_cli`, `hermes`, `kimi_cli`, `copilot_cli`, `opencode_cli`, `cursor_cli`. The `cao launch --provider` flag always takes precedence for the initial session. See [`examples/cross-provider/`](examples/cross-provider/).

### Tool Restrictions

CAO controls what each agent can do via `role` and `allowedTools` in the profile. CAO translates restrictions to each provider's native enforcement where available. See [docs/tool-restrictions.md](docs/tool-restrictions.md) for the full reference.

### Custom Orchestration

`cao-server` exposes REST APIs for session management, terminal control, and messaging. The built-in CLI commands and MCP tools are just packagings of those APIs — you can combine the three orchestration modes into custom workflows or build new patterns on top of the underlying API. See [docs/api.md](docs/api.md).

## Extensibility & Integration

Three programmatic surfaces for driving CAO from outside, plus two extension points (skills and plugins). For the decision guide on which surface to use, see [docs/control-planes.md](docs/control-planes.md).

### Session Management CLI

`cao session` commands manage sessions programmatically — ideal for scripting, CI pipelines, or any caller that can run a shell command.

| Command | Description |
|---------|-------------|
| `cao session list` | List active sessions |
| `cao session status <name>` | Show conductor status and last output |
| `cao session status <name> --workers` | Include worker terminal statuses |
| `cao session send <name> "msg"` | Send a message and wait for completion |
| `cao session send <name> "msg" --async` | Fire-and-forget |
| `cao session send <name> "msg" --timeout N` | Wait up to N seconds |
| `cao launch --agents <profile>` | Launch a new supervisor session |
| `cao shutdown --session <name>` | Shut down a specific session |
| `cao shutdown --all` | Shut down every CAO session |
| `cao terminal restore <terminal_id>` | Restore a deleted terminal's scrollback into a new window for debugging |

Headless launch (send an initial task without attaching):

```bash
cao launch --agents supervisor --headless --yolo \
  --session-name my-task --working-directory '/path/to/project' "Your task here"
```

Add `--async` to return immediately without waiting for completion.

> Session names are auto-prefixed with `cao-`. Use the prefixed form (e.g. `cao-my-task`) in later commands.

For the command reference and the agent-facing skill, see the [Session Management skill](skills/cao-session-management/SKILL.md).

Because `cao session` is just shell commands, any AI assistant that supports shell-callable skills should be able to drive CAO this way — e.g. Claude Code, Kiro CLI, [OpenClaw](https://github.com/openclaw/openclaw), or [Hermes Agent](https://github.com/NousResearch/hermes-agent). See [docs/external-tool-integration.md](docs/external-tool-integration.md) for integrating CAO session management into external tools.

### CAO Ops MCP Server

`cao-ops-mcp` exposes the same management operations as structured MCP tools for a primary agent (Claude Code, Claude Desktop, etc.). It is the MCP-flavoured equivalent of `cao session` — pick `cao-ops-mcp` when your caller speaks MCP, `cao session` otherwise.

| Server | Who uses it | Purpose |
|--------|-------------|---------|
| `cao-mcp-server` | Agents **inside** a CAO session | Inter-agent orchestration (`handoff`, `assign`, `send_message`) |
| `cao-ops-mcp` | A primary agent **outside** a CAO session | Meta management (install profiles, launch/monitor sessions) |

**Setup** — add to your primary agent's MCP configuration. Requires `cao-server` running at `localhost:9889`.

For Claude Code, add to `.mcp.json`:

```json
{
  "mcpServers": {
    "cao-ops-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/awslabs/cli-agent-orchestrator.git@main", "cao-ops-mcp-server"]
    }
  }
}
```

Other agents: use the equivalent stdio MCP command:

```
uvx --from git+https://github.com/awslabs/cli-agent-orchestrator.git@main cao-ops-mcp-server
```

**Available tools** — `list_profiles`, `get_profile_details`, `install_profile`, `launch_session`, `send_session_message`, `list_sessions`, `get_session_info`, `shutdown_session`.

Typical workflow: `list_profiles` → `install_profile` → `launch_session` → `send_session_message` → `get_session_info` → `shutdown_session`.

### Flows — scheduled agent sessions

Schedule agent sessions to run automatically using cron expressions:

```bash
cao schedule add daily-standup.md
cao schedule list
cao schedule run daily-standup   # manual run, ignores schedule
```

Flows support static prompts or conditional execution via a gating script. `cao-server` must be running for scheduled execution.

For the full guide — flow file format, the conditional-execution pattern, and all `cao schedule` commands — see [docs/flows.md](docs/flows.md).

### Skills

Skills are portable, structured guides (following the universal [SKILL.md](https://github.com/anthropics/skills) format) that encode domain knowledge for agents. They work across coding assistants (Claude Code, Kiro CLI, Codex CLI, Kimi CLI, GitHub Copilot, Cursor, OpenCode, LobeHub) and frameworks ([Strands Agents SDK](https://strandsagents.com/docs/user-guide/concepts/plugins/skills/), [Microsoft Agent Framework](https://devblogs.microsoft.com/agent-framework/give-your-agents-domain-expertise-with-agent-skills-in-microsoft-agent-framework/)).

CAO ships built-in skills and also manages "managed skills" shared across all agent sessions. Built-ins (`cao-supervisor-protocols`, `cao-worker-protocols`) are auto-seeded at server startup. You can add your own:

```bash
cao skills list
cao skills add ./my-coding-standards
cao skills add ./my-coding-standards --force   # overwrite
cao skills remove my-coding-standards
```

Skills are delivered to providers automatically (native `skill://` resources for Kiro CLI; runtime prompt injection for Claude Code / Codex / Kimi; baked-in `.agent.md` for Copilot).

For the full reference — authoring, loading, delivery mechanics — see [docs/skills.md](docs/skills.md). For integrating with OpenClaw or other external tools, see [docs/external-tool-integration.md](docs/external-tool-integration.md).

### Plugins

Plugins are observer-only Python extensions that react to server-side events inside `cao-server` — lifecycle changes and message delivery. They are the **outbound** surface of CAO: the interfaces above drive CAO in; plugins stream events out. Typical uses: forwarding inter-agent messages to Discord/Slack/Telegram, audit logging, metrics export.

- **Installation, events, troubleshooting:** [docs/plugins.md](docs/plugins.md)
- **Ready-to-run example:** [examples/plugins/cao-discord/](examples/plugins/cao-discord/)
- **Author your own:** [cao-plugin skill](skills/cao-plugin/SKILL.md)
- **How plugins fit with the inbound surfaces:** [docs/control-planes.md](docs/control-planes.md)

## Security

`cao-server` is designed for **localhost-only use**. The WebSocket terminal endpoint (`/terminals/{id}/ws`) provides full PTY access and rejects non-loopback connections. Do not expose the server to untrusted networks without adding authentication.

**DNS rebinding protection** — the server validates HTTP `Host` headers and rejects requests where the host is not `localhost` or `127.0.0.1` with `400 Bad Request`. This guards against [DNS rebinding attacks](https://owasp.org/www-community/attacks/DNS_Rebinding).

If you need to expose the server on a network (not recommended for development), the Host header validation will also reject those requests unless the hostname is in the allowed list.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and best practices.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Releases

CAO publishes to [PyPI](https://pypi.org/project/cli-agent-orchestrator/) via an OIDC-authenticated GitHub Actions pipeline (TestPyPI → smoke test → maintainer-approved prod). See [docs/RELEASING.md](docs/RELEASING.md).

## License

Apache-2.0.
