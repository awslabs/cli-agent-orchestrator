# Settings

CAO stores user configuration in `~/.aws/cli-agent-orchestrator/settings.json`. This file is managed by the settings service and can be edited via the Web UI Settings page or the REST API.

## Agent Profile Directories

CAO discovers agent profiles by scanning multiple directories. When loading or listing profiles, directories are scanned in this order (first match wins):

1. **Local store** — `~/.aws/cli-agent-orchestrator/agent-store/`
2. **Provider-specific directories** — Configured per provider (see defaults below)
3. **Extra custom directories** — User-added paths
4. **Built-in store** — Bundled with the CAO package

### Default Directories

| Key | Provider | Default Path |
|-----|----------|-------------|
| `kiro_cli` | Kiro CLI | `~/.kiro/agents` |
| `claude_code` | Claude Code | `~/.aws/cli-agent-orchestrator/agent-store` |
| `codex` | Codex | `~/.aws/cli-agent-orchestrator/agent-store` |
| `cao_installed` | CAO Installed | `~/.aws/cli-agent-orchestrator/agent-context` |

The `cao_installed` directory is where `cao install` places agent profiles. This keeps installed profiles separate from hand-authored ones in `agent-store`.

### Overriding Directories

Override any provider directory via the REST API or Web UI Settings page:

```bash
# Via REST API
curl -X POST http://localhost:9889/settings/agent-dirs \
  -H "Content-Type: application/json" \
  -d '{"kiro_cli": "/custom/path/to/agents"}'
```

Or edit `settings.json` directly:

```json
{
  "agent_dirs": {
    "kiro_cli": "/custom/path/to/agents"
  }
}
```

Only specified providers are updated; others retain their defaults.

### Extra Directories

Add additional directories that are scanned for agent profiles across all providers:

```json
{
  "extra_agent_dirs": [
    "/path/to/team-shared-agents",
    "/path/to/project-specific-agents"
  ]
}
```

## Skill Directories

CAO discovers skills (loaded on demand via the `load_skill` MCP tool) by scanning, in order (first match wins):

1. **Global skill store** — `~/.aws/cli-agent-orchestrator/skills/`
2. **Extra custom directories** — User-added paths (`extra_skill_dirs`)

This mirrors agent-profile resolution: a skill in the global store is not shadowed by one with the same name in a later extra directory. `extra_skill_dirs` lets you keep a project's skills in the project repo (e.g. `<repo>/.cao/skills`) and register the directory, instead of copying or symlinking each skill into the global store.

```json
{
  "extra_skill_dirs": [
    "/path/to/team-shared-skills",
    "/path/to/project-specific-skills"
  ]
}
```

## settings.json Format

```json
{
  "agent_dirs": {
    "kiro_cli": "~/.kiro/agents",
    "claude_code": "~/.aws/cli-agent-orchestrator/agent-store",
    "codex": "~/.aws/cli-agent-orchestrator/agent-store",
    "cao_installed": "~/.aws/cli-agent-orchestrator/agent-context"
  },
  "extra_agent_dirs": [],
  "extra_skill_dirs": []
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/settings/agent-dirs` | Get current agent directories (merged with defaults) |
| `POST` | `/settings/agent-dirs` | Update agent directories |
| `GET` | `/settings/skill-dirs` | Get the global skill store path and extra skill directories |
| `POST` | `/settings/skill-dirs` | Set extra custom skill directories |

See [api.md](api.md) for the full API reference.

## Server Tuning Settings

The `server` key in `settings.json` controls timeouts and buffer sizes used by the CAO runtime. All values have safe defaults — only override if you experience timeouts or queue overflows.

```json
{
  "server": {
    "mcp_request_timeout": 30,
    "event_bus_max_queue_size": 1024,
    "provider_init_timeout": 60,
    "startup_prompt_handler_timeout": 20
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `mcp_request_timeout` | `30` | Seconds to wait for HTTP calls between the MCP server process and the CAO API. Increase if you see timeout errors during `handoff` or `assign` operations. |
| `event_bus_max_queue_size` | `1024` | Maximum events buffered per subscriber queue in the internal event bus. Increase if you see "Queue full, dropping event" errors with many active workers. |
| `provider_init_timeout` | `60` | Seconds to wait for the initial shell prompt before launching a CLI provider. Some providers apply additional provider-specific initialization timeouts after launch. Increase if you see shell startup timeouts on slow machines. |
| `startup_prompt_handler_timeout` | `20` | Seconds the Claude Code startup prompt handler waits for workspace trust / bypass dialogs before giving up. Rarely needs changing. |

## Server Network Settings

`cao-server` is a local-only service by default. The host header, CORS, and
WebSocket client allowlists ship locked down to loopback. Three env vars let
operators extend each list when running CAO behind a reverse proxy or inside a
container — see issues [#149](https://github.com/awslabs/cli-agent-orchestrator/issues/149)
and [#151](https://github.com/awslabs/cli-agent-orchestrator/issues/151).

All three accept a comma-separated list and **extend** (not replace) the built-in
defaults, so loopback access is preserved even when the env var is set:

| Env var | Extends | Use case |
|---|---|---|
| `CAO_ALLOWED_HOSTS` | `ALLOWED_HOSTS` (Host header allowlist used by `TrustedHostMiddleware`) | Fronting cao-server with a reverse proxy at a hostname other than `localhost` / `127.0.0.1`. |
| `CAO_CORS_ORIGINS` | `CORS_ORIGINS` (browser origins permitted by CORS) | Serving the web UI from a non-default port, or from another origin (e.g. a custom dashboard). |
| `CAO_WS_ALLOWED_CLIENTS` | `WS_ALLOWED_CLIENTS` (client IPs permitted to attach to the PTY WebSocket) | Running `cao-server` inside Docker where the host browser arrives via a bridge IP (e.g. `172.17.0.1`). |

Example — running `cao-server` in a container that accepts WebSocket attaches
from the Docker bridge:

```bash
CAO_ALLOWED_HOSTS=cao.local \
CAO_CORS_ORIGINS=http://cao.local:8080 \
CAO_WS_ALLOWED_CLIENTS=172.17.0.1 \
  uv tool run cao-server --host 0.0.0.0
```

> **Security note:** the WebSocket PTY endpoint is unauthenticated. Only add
> client IPs you actually trust to `CAO_WS_ALLOWED_CLIENTS` — anyone who can
> reach the listener at one of those IPs gets full PTY access to running
> agent terminals.
