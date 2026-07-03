# Configuration

CAO stores user configuration in a single file, `~/.aws/cli-agent-orchestrator/settings.json`. This consolidates what used to be two separate files (`settings.json` and `config.json`) into one unified schema, resolved through one precedence chain:

```
CLI flag  >  CAO_* environment variable  >  settings.json  >  built-in default
```

`ConfigService` (`services/config_service.py`) is the single reader/writer behind this precedence chain. Callers that previously read `config.json` or scattered `os.getenv("CAO_*")` calls for the values below now resolve through it.

> `.env` file handling (`utils/env.py`, forwarded provider env vars) is a separate, out-of-scope surface — unaffected by this doc.

## settings.json schema

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
    "compile_timeout_s": 120.0
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

Every top-level key is optional — omit any section/key you don't want to override; `ConfigService` fills in built-in defaults.

## `cao config` CLI

```bash
cao config get terminal.backend       # resolved value (env/file/default applied)
cao config set terminal.backend herdr # persist to settings.json
cao config list                       # every known key, resolved
cao config path                       # absolute path to settings.json
```

## Sections

### Agents (`agents`)

CAO discovers agent profiles by scanning multiple directories, in this order (first match wins):

1. **Local store** — `~/.aws/cli-agent-orchestrator/agent-store/`
2. **Provider-specific directories** — `agents.dirs`, keyed by provider
3. **Extra custom directories** — `agents.extra_dirs`
4. **Built-in store** — bundled with the CAO package

| Key | Provider | Default Path |
|-----|----------|-------------|
| `kiro_cli` | Kiro CLI | `~/.kiro/agents` |
| `claude_code` | Claude Code | `~/.aws/cli-agent-orchestrator/agent-store` |
| `codex` | Codex | `~/.aws/cli-agent-orchestrator/agent-store` |
| `cao_installed` | CAO Installed | `~/.aws/cli-agent-orchestrator/agent-context` |

Override via REST API, Web UI Settings page, `cao config set agents.dirs.<provider> <path>`, or editing `settings.json` directly. Only specified providers are updated; others keep their defaults.

`agents.roles` defines custom [role](../CODEBASE.md) → `allowedTools` bundles, layered on top of the built-in `supervisor` / `reviewer` / `developer` roles.

### Skills (`skills`)

Skills (loaded on demand via the `load_skill` MCP tool) are discovered from, in order:

1. **Global skill store** — `~/.aws/cli-agent-orchestrator/skills/`
2. **Extra custom directories** — `skills.extra_dirs`

`skills.extra_dirs` lets you keep a project's skills in the project repo (e.g. `<repo>/.cao/skills`) and register the directory instead of copying/symlinking each skill into the global store.

### Server (`server`)

Timeouts and buffer sizes used by the CAO runtime. All values have safe defaults — only override if you experience timeouts or queue overflows.

| Setting | Default | Description |
|---------|---------|--------------|
| `mcp_request_timeout` | `30` | Seconds to wait for HTTP calls between the MCP server process and the CAO API. |
| `event_bus_max_queue_size` | `1024` | Max events buffered per subscriber queue in the internal event bus. |
| `provider_init_timeout` | `60` | Seconds to wait for the initial shell prompt before launching a CLI provider. |
| `startup_prompt_handler_timeout` | `20` | Seconds the Claude Code startup prompt handler waits for workspace trust dialogs. |

### Memory (`memory`)

| Setting | Default | Description |
|---------|---------|--------------|
| `enabled` | `true` | Master switch for the memory subsystem. |
| `compile_mode` | `"llm"` | `llm` or `append`. `append` skips the LLM wiki-compiler entirely. |
| `flush_threshold` | `0.85` | Context-usage fraction that triggers a memory flush. |
| `compile_timeout_s` | `120.0` | Wall-clock timeout for the wiki compile call. |

### Terminal backend (`terminal`)

CAO's default backend is [tmux](tmux.md). [herdr](https://herdr.dev/) is an experimental, opt-in alternative — a terminal-native agent runtime that exposes real-time status events instead of requiring CAO to poll and pattern-match terminal output.

```json
{
  "terminal": {
    "backend": "herdr",
    "herdr_session": "my-session"
  }
}
```

- `backend`: `"tmux"` (default) or `"herdr"` [EXPERIMENTAL].
- `herdr_session`: the herdr session name to connect to (default `"cao"`).

Select a backend for a single run without touching `settings.json`:

```bash
cao-server --terminal herdr
```

`--terminal` (CLI flag) beats `CAO_TERMINAL_BACKEND` (env var) beats `terminal.backend` (file) beats the `"tmux"` default — the standard precedence chain. See [herdr.md](herdr.md) for herdr-specific setup, viewing/attaching, and troubleshooting.

### MCP Apps (`apps`)

Default-off. See [../src/cli_agent_orchestrator/ext_apps/apps.py](../src/cli_agent_orchestrator/ext_apps/apps.py) for the `ui://cao/*` MCP App resource surface this gates.

| Setting | Default | Description |
|---------|---------|--------------|
| `enabled` | `false` | Enables the MCP Apps surface (dashboard/agent/event-stream views + app tools + topology widget). |
| `static_dir` | `null` | Override for the built `apps_static/` directory (packaged/dev-tree locations are tried automatically otherwise). |

### Network (`network`)

`cao-server` is a local-only service by default. These lists **extend** (not replace) the loopback-only built-in defaults, so loopback access is preserved even when set.

| Setting | Extends | Use case |
|---|---|---|
| `allowed_hosts` | `ALLOWED_HOSTS` (Host header allowlist for `TrustedHostMiddleware`) | Fronting cao-server with a reverse proxy at a non-localhost hostname. |
| `cors_origins` | `CORS_ORIGINS` (browser origins permitted by CORS) | Serving the web UI from a non-default port or origin. |
| `ws_allowed_clients` | `WS_ALLOWED_CLIENTS` (client IPs permitted to attach to the PTY WebSocket) | Running `cao-server` inside Docker (host browser arrives via a bridge IP). |

> **Security note:** the WebSocket PTY endpoint is unauthenticated. Only add client IPs you actually trust to `ws_allowed_clients` — anyone reaching the listener at one of those IPs gets full PTY access to running agent terminals.

### Auth (`auth`)

Default-off OAuth 2.1 auth core; see [security/auth.py](../src/cli_agent_orchestrator/security/auth.py). Auth activates only when `jwks_uri` (or `AUTH0_DOMAIN`) is set.

| Setting | Description |
|---------|--------------|
| `jwks_uri` | Generic IdP JWKS endpoint. |
| `audience` | Expected token audience. |
| `issuer` | Issuer advertised by the RFC 9728 PRM endpoint. |

### Logging (`logging`)

| Setting | Default | Description |
|---------|---------|--------------|
| `level` | `"INFO"` | Log level for the CAO server log file. |

## Environment variable reference

Every `CAO_*` variable below maps 1:1 to a `settings.json` key and is resolved through the same precedence chain — an env var always beats the file, and always loses to an explicit CLI flag.

| Env var | Config path | Type |
|---|---|---|
| `CAO_TERMINAL_BACKEND` | `terminal.backend` | str |
| `CAO_HERDR_SESSION` | `terminal.herdr_session` | str |
| `CAO_MCP_APPS_ENABLED` | `apps.enabled` | bool |
| `CAO_MCP_APPS_STATIC_DIR` | `apps.static_dir` | str |
| `CAO_AUTH_JWKS_URI` | `auth.jwks_uri` | str |
| `CAO_AUTH_AUDIENCE` | `auth.audience` | str |
| `CAO_AUTH_ISSUER` | `auth.issuer` | str |
| `CAO_LOG_LEVEL` | `logging.level` | str |
| `CAO_ALLOWED_HOSTS` | `network.allowed_hosts` | comma-separated list |
| `CAO_CORS_ORIGINS` | `network.cors_origins` | comma-separated list |
| `CAO_WS_ALLOWED_CLIENTS` | `network.ws_allowed_clients` | comma-separated list |
| `CAO_MEMORY_ENABLED` | `memory.enabled` | bool |
| `CAO_MEMORY_COMPILE_MODE` | `memory.compile_mode` | str (`llm`/`append`) |
| `CAO_MEMORY_FLUSH_THRESHOLD` | `memory.flush_threshold` | float |
| `CAO_MCP_REQUEST_TIMEOUT` | `server.mcp_request_timeout` | int |
| `CAO_EVENT_BUS_MAX_QUEUE_SIZE` | `server.event_bus_max_queue_size` | int |
| `CAO_PROVIDER_INIT_TIMEOUT` | `server.provider_init_timeout` | int |
| `CAO_STARTUP_PROMPT_HANDLER_TIMEOUT` | `server.startup_prompt_handler_timeout` | int |

The full table lives in `ConfigService.ENV_REGISTRY` (`services/config_service.py`) — the source of truth this doc mirrors.

### Not yet routed through ConfigService

A number of other `CAO_*` variables (runtime/process-identity vars like `CAO_TERMINAL_ID`, `CAO_SESSION_NAME`, `CAO_WORKFLOW_RUN_ID`; provider-tuning vars like `CAO_HERMES_*`, `CAO_AGENTS_DIR`, `CAO_API_HOST`/`CAO_API_PORT`, `CAO_PYTE_STATUS`, `CAO_EAGER_INBOX_DELIVERY`; and `CAO_AUTH_LOCAL_TOKEN`) are still read ad hoc via `os.getenv` at their call sites, mostly in `constants.py`, `mcp_server/server.py`, `security/auth.py`, and the `providers/*` modules. These were deliberately left out of this pass to keep the diff scoped to the two surfaces issue #357 named explicitly (`settings.json` + `config.json`); folding them into the registry is a natural follow-up but not required for config unification.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/settings/agent-dirs` | Get current agent directories (merged with defaults) |
| `POST` | `/settings/agent-dirs` | Update agent directories |
| `GET` | `/settings/skill-dirs` | Get the global skill store path and extra skill directories |
| `POST` | `/settings/skill-dirs` | Set extra custom skill directories |
| `GET` | `/settings/memory` | Whether the memory subsystem is enabled |

See [api.md](api.md) for the full API reference.

## Migrating from the old two-file setup

Previously, `terminal_backend` / `herdr_session` lived in a separate `~/.aws/cli-agent-orchestrator/config.json`, read inline by `backends/factory.py`. On first read after upgrading, if `config.json` exists and `settings.json` has no `terminal` section yet, `ConfigService` copies `terminal_backend` → `terminal.backend` and `herdr_session` → `terminal.herdr_session` into `settings.json` and logs the move once. `config.json` is left on disk untouched but is no longer read afterward — it is deprecated.
