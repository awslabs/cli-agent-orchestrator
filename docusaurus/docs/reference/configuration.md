---
sidebar_position: 2
---

# Configuration

CAO stores user configuration in a single JSON file with a unified precedence chain:

```
CLI flag  >  CAO_* environment variable  >  settings.json  >  built-in default
```

## File Location

```
~/.aws/cli-agent-orchestrator/settings.json
```

Print the path programmatically:

```bash
cao config path
```

## Full Schema

Every top-level key is optional. Omit any section you do not need to override; `ConfigService` fills in built-in defaults.

```json
{
  "agents": {
    "dirs": {
      "kiro_cli": "~/.kiro/agents",
      "claude_code": "~/.aws/cli-agent-orchestrator/agent-store",
      "codex": "~/.aws/cli-agent-orchestrator/agent-store",
      "cao_installed": "~/.aws/cli-agent-orchestrator/agent-context"
    },
    "extra_dirs": [],
    "disabled_dirs": [],
    "roles": {}
  },
  "skills": {
    "extra_dirs": []
  },
  "server": {
    "mcp_request_timeout": 30,
    "event_bus_max_queue_size": 1024,
    "provider_init_timeout": 60,
    "startup_prompt_handler_timeout": 20
  },
  "memory": {
    "enabled": true,
    "compile_mode": "llm",
    "flush_threshold": 0.85,
    "compile_timeout_s": 120.0,
    "lint_enabled": true
  },
  "terminal": {
    "backend": "tmux",
    "herdr_session": "cao"
  },
  "apps": {
    "enabled": false,
    "static_dir": null
  },
  "network": {
    "allowed_hosts": [],
    "cors_origins": [],
    "ws_allowed_clients": []
  },
  "auth": {
    "jwks_uri": "",
    "audience": "",
    "issuer": ""
  },
  "logging": {
    "level": "INFO"
  }
}
```

## Section Reference

### `agents`

Controls where CAO discovers agent profiles.

| Key | Type | Description |
|-----|------|-------------|
| `dirs` | object | Provider-keyed paths to agent profile directories |
| `extra_dirs` | array | Additional directories to scan for profiles |
| `disabled_dirs` | array | Configured directories toggled off (profiles hidden without deletion) |
| `roles` | object | Custom role-to-allowedTools mappings for names other than the built-in `supervisor`/`reviewer`/`developer`, which cannot be overridden |

### `skills`

| Key | Type | Description |
|-----|------|-------------|
| `extra_dirs` | array | Additional directories to scan for skills |

### `server`

Timeouts and buffer sizes for the CAO runtime. Override only if you experience timeouts or queue overflows.

| Key | Default | Description |
|-----|---------|-------------|
| `mcp_request_timeout` | `30` | Seconds for HTTP calls between MCP server and CAO API |
| `event_bus_max_queue_size` | `1024` | Max events buffered per subscriber in the event bus |
| `provider_init_timeout` | `60` | Seconds to wait for a CLI agent to reach IDLE (also caps startup-prompt handling) |
| `startup_prompt_handler_timeout` | `20` | Idle-gap (seconds) between startup prompts before handler exits (used by Claude Code, Kimi CLI, Antigravity CLI) |

### `memory`

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Master switch for the memory subsystem |
| `compile_mode` | `"llm"` | `"llm"` (wiki compiler) or `"append"` (skip LLM compilation) |
| `flush_threshold` | `0.85` | Context-usage fraction that triggers a memory flush |
| `compile_timeout_s` | `120.0` | Wall-clock timeout for wiki compile |
| `lint_enabled` | `true` | Enable memory wiki linting. Fail-closed: an explicit `false` from either the env var or `settings.json` disables it, regardless of the usual precedence |

### `terminal`

| Key | Default | Description |
|-----|---------|-------------|
| `backend` | `"tmux"` | `"tmux"` or `"herdr"` (experimental) |
| `herdr_session` | `"cao"` | herdr session name to connect to |

### `apps`

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable the MCP Apps surface (dashboard/agent/event-stream views) |
| `static_dir` | `null` | Override for the built `apps_static/` directory |

### `network` (env-var only)

These keys exist in the schema but only their `CAO_*` env-var counterparts have runtime effect.

| Key | Env Var | Description |
|-----|---------|-------------|
| `allowed_hosts` | `CAO_ALLOWED_HOSTS` | Host header allowlist for TrustedHostMiddleware |
| `cors_origins` | `CAO_CORS_ORIGINS` | Browser origins permitted by CORS |
| `ws_allowed_clients` | `CAO_WS_ALLOWED_CLIENTS` | Client IPs permitted to attach to PTY WebSocket |

### `auth` (env-var only)

Default-off OAuth 2.1 auth. Activates when an IdP is configured &mdash; either `AUTH0_DOMAIN` or `CAO_AUTH_JWKS_URI`.

| Key | Env Var | Description |
|-----|---------|-------------|
| `jwks_uri` | `CAO_AUTH_JWKS_URI` | Generic IdP JWKS endpoint |
| `audience` | `CAO_AUTH_AUDIENCE` | Expected token audience |
| `issuer` | `CAO_AUTH_ISSUER` | Issuer for RFC 9728 PRM endpoint |

### `logging`

| Key | Default | Description |
|-----|---------|-------------|
| `level` | `"INFO"` | Log level for the CAO server log file |

## `cao config` CLI

```bash
# Get the resolved value for a dotted key
cao config get terminal.backend

# Set a value (persists to settings.json)
cao config set terminal.backend herdr

# List every known key with its resolved value
cao config list

# Print the absolute path to settings.json
cao config path
```

## Precedence Chain

For any configuration value, CAO resolves through:

1. **CLI flag** -- e.g., `cao-server --terminal herdr`
2. **Environment variable** -- e.g., `CAO_TERMINAL_BACKEND=herdr`
3. **settings.json** -- the file value
4. **Built-in default** -- hardcoded in the source

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/settings/agent-dirs` | Get current agent directories |
| `POST` | `/settings/agent-dirs` | Update agent directories |
| `GET` | `/settings/skill-dirs` | Get skill store path and extras |
| `POST` | `/settings/skill-dirs` | Set extra skill directories |
| `GET` | `/settings/memory` | Memory subsystem status |
