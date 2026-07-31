---
sidebar_position: 3
---

# Environment Variables

CAO uses `CAO_*` environment variables as one tier in the configuration precedence chain: CLI flag > env var > settings.json > default.

## Wired Through ConfigService

These variables map 1:1 to a `settings.json` key. Setting either the env var or the file key has the same runtime effect.

| Env Var | Config Path | Type | Default |
|---------|-------------|------|---------|
| `CAO_TERMINAL_BACKEND` | `terminal.backend` | str | `"tmux"` |
| `CAO_HERDR_SESSION` | `terminal.herdr_session` | str | `"cao"` |
| `CAO_MCP_APPS_ENABLED` | `apps.enabled` | bool | `false` |
| `CAO_MCP_APPS_STATIC_DIR` | `apps.static_dir` | str | `null` |
| `CAO_LOG_LEVEL` | `logging.level` | str | `"INFO"` |
| `CAO_MEMORY_ENABLED` | `memory.enabled` | bool | `true` |
| `CAO_MEMORY_COMPILE_MODE` | `memory.compile_mode` | str | `"llm"` |
| `CAO_MEMORY_FLUSH_THRESHOLD` | `memory.flush_threshold` | float | `0.85` |
| `CAO_MEMORY_LINT_ENABLED` | `memory.lint_enabled` | bool | `true` |
| `CAO_MCP_REQUEST_TIMEOUT` | `server.mcp_request_timeout` | int | `30` |
| `CAO_EVENT_BUS_MAX_QUEUE_SIZE` | `server.event_bus_max_queue_size` | int | `1024` |
| `CAO_PROVIDER_INIT_TIMEOUT` | `server.provider_init_timeout` | int | `60` |
| `CAO_STARTUP_PROMPT_HANDLER_TIMEOUT` | `server.startup_prompt_handler_timeout` | int | `20` |

## Network and Auth (Env-Var Only)

These have schema entries but only the env var is actually honored at runtime.

| Env Var | Purpose | Notes |
|---------|---------|-------|
| `CAO_ALLOWED_HOSTS` | Extend Host header allowlist for TrustedHostMiddleware | Comma-separated; extends (not replaces) loopback defaults |
| `CAO_CORS_ORIGINS` | Extend browser origins permitted by CORS | Comma-separated |
| `CAO_WS_ALLOWED_CLIENTS` | Extend client IPs permitted to attach to PTY WebSocket | Comma-separated; security-sensitive |
| `CAO_FORWARDED_ALLOW_IPS` | Trusted proxy IPs for X-Forwarded-For parsing | Comma-separated |
| `CAO_AUTH_JWKS_URI` | IdP JWKS endpoint (activates auth when set) | |
| `CAO_AUTH_AUDIENCE` | Expected token audience | |
| `CAO_AUTH_ISSUER` | Issuer for RFC 9728 PRM endpoint | |
| `CAO_AUTH_LOCAL_TOKEN` | Local bearer token for development/testing | |

## Server and Runtime

| Env Var | Purpose | Default |
|---------|---------|---------|
| `CAO_API_HOST` | Bind address for cao-server | `127.0.0.1` |
| `CAO_API_PORT` | Port for cao-server | `9889` |
| `CAO_PYTE_STATUS` | Enable pyte-rendered status detection | `true` |
| `CAO_EAGER_INBOX_DELIVERY` | Deliver inbox messages during PROCESSING for capable providers | `false` |
| `CAO_ENABLE_WORKING_DIRECTORY` | Enable `working_directory` parameter on orchestration tools | `false` |
| `CAO_ENABLE_SENDER_ID_INJECTION` | Auto-append supervisor terminal ID to assign messages | `true` |

## Per-Terminal (Set Automatically)

These are set by CAO on each terminal's environment and used for routing/identification. Do not set them manually.

| Env Var | Purpose |
|---------|---------|
| `CAO_TERMINAL_ID` | Unique 8-char hex ID for this terminal (set by tmux session env) |
| `CAO_SESSION_NAME` | Name of the parent CAO session (herdr backend only; the default tmux backend sets just `CAO_TERMINAL_ID`) |
| `CAO_WORKFLOW_RUN_ID` | Workflow run identifier (when executing a workflow) |
| `CAO_WORKFLOW_STEP_ID` | Current workflow step identifier |
| `CAO_WORKFLOW_GENERATION` | Run generation, incremented on resume |

## Provider-Specific

| Env Var | Purpose |
|---------|---------|
| `CAO_AGENTS_DIR` | Override Kiro CLI agent directory (default: `~/.kiro/agents`) |
| `CAO_GRAPH_EXPORT_ROOT` | Override graph export confinement root |
| `CAO_PROFILE_ALLOWED_HOSTS` | Allowlist for profile install from URLs (comma-separated) |

## Managed Environment File

CAO also supports a managed `.env` file at `~/.aws/cli-agent-orchestrator/.env`. Values here are substituted into agent profiles when they are loaded or installed &mdash; they are **not** injected into the agent's terminal environment. To pass real environment variables to a session, use `cao launch --env KEY=VALUE`. Manage the file with:

```bash
# Set a variable
cao env set MY_API_KEY sk-abc123

# List variables
cao env list

# Remove a variable
cao env unset MY_API_KEY
```

Variables in the `.env` file can be referenced in agent profiles using `${VAR}` syntax. Flow prompts use a separate mechanism: `[[key]]` placeholders filled from the flow script's JSON `output` object.
