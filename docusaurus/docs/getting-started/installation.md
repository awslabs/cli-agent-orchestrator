---
sidebar_position: 1
---

# Installation

## Prerequisites

- **Python 3.10+**
- **tmux** — a terminal multiplexer that runs each agent in its own isolated terminal window. CAO uses tmux to manage agent sessions without requiring multiple terminal tabs.
- **At least one supported AI CLI agent** installed on your machine:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`) — requires Anthropic API key
  - [Kiro CLI](https://kiro.dev) — requires AWS credentials (this is the default provider)
  - [Codex](https://github.com/openai/codex) — requires OpenAI API key
  - Or any of: Copilot CLI, Cursor CLI, Kimi CLI, OpenCode CLI, Hermes, Antigravity CLI
- **uv** (recommended) — a fast Python package manager. Install it with:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

## Install CAO

import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

<Tabs>
<TabItem value="uv" label="uv (recommended)">

```bash
uv tool install cli-agent-orchestrator
```

</TabItem>
<TabItem value="pip" label="pip">

```bash
pip install cli-agent-orchestrator
```

</TabItem>
<TabItem value="source" label="From source">

```bash
git clone https://github.com/awslabs/cli-agent-orchestrator.git
cd cli-agent-orchestrator
uv tool install .
```

</TabItem>
</Tabs>

## Install tmux (if needed)

```bash
# macOS
brew install tmux

# Ubuntu/Debian
sudo apt-get install tmux

# Amazon Linux / RHEL
sudo yum install tmux
```

## Start the API server

CAO uses a local API server for session coordination. Start it in a dedicated terminal:

```bash
cao-server
```

You should see output like:
```
INFO:     Started server process
INFO:     Uvicorn running on http://127.0.0.1:9889
```

The server **must be running** before you can use `cao launch`, `cao session`, or any orchestration commands. Without it, commands will fail with `Failed to connect to cao-server`.

:::tip
Leave `cao-server` running in its own terminal tab or tmux window for the duration of your session.
:::

## Verify installation

In another terminal:

```bash
cao --version
# cao, version 2.x.x

cao profile list
# Shows available agent profiles
```

## Updating

```bash
cao update
```

This auto-detects your install method (PyPI, git, local) and runs the appropriate upgrade command. See [`cao update`](../reference/cli-commands.md#cao-update) for details.

:::note
`cao update` requires that CAO was installed as a uv tool. If you installed with pip, use `pip install --upgrade cli-agent-orchestrator` instead.
:::

## What gets installed

| Command | Purpose |
|---------|---------|
| `cao` | Main CLI for launching and managing agent sessions |
| `cao-server` | API server for session coordination (port 9889) |
| `cao-mcp-server` | MCP server exposing orchestration tools to agents |
| `cao-ops-mcp-server` | Ops MCP server for external integrations |

Configuration and data are stored in `~/.aws/cli-agent-orchestrator/`. This path is used regardless of which provider you use — no AWS account is required unless you're using Kiro CLI specifically.
