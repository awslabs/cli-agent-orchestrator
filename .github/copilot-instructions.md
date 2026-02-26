# Copilot Instructions for `cli-agent-orchestrator`

## Build, test, and lint commands

Use `uv` for all local development commands (matches CI/workflows).

```bash
# Install dependencies (dev + extras used in CI)
uv sync --all-extras --dev

# Run unit-style suite used by CI (no e2e, no q_cli integration)
uv run pytest test/ --ignore=test/providers/test_q_cli_integration.py --ignore=test/e2e -m "not e2e" -v

# Run all default tests (pyproject addopts applies coverage and excludes e2e)
uv run pytest -v

# Run a single test file
uv run pytest test/providers/test_codex_provider_unit.py -v

# Run a single test class / test case
uv run pytest test/providers/test_codex_provider_unit.py::TestCodexBuildCommand -v
uv run pytest test/providers/test_codex_provider_unit.py::TestCodexBuildCommand::test_build_command_no_profile -v

# Run e2e tests explicitly (requires running cao-server and real CLI auth)
uv run pytest -m e2e test/e2e/ -v

# Lint/type-check commands
uv run black --check src/ test/
uv run isort --check-only src/ test/
uv run mypy src/
```

## High-level architecture

- **Three entrypoints**:
  - `cao` (Click CLI): user-facing commands (`launch`, `shutdown`, `install`, `init`, `flow`)
  - `cao-server` (FastAPI on `localhost:9889`): session/terminal/inbox/flow HTTP API
  - `cao-mcp-server` (FastMCP): MCP tools `handoff`, `assign`, `send_message` that call the HTTP API
- **Core runtime flow**:
  1. Terminal/session operations go through `services/terminal_service.py`
  2. Metadata persists in SQLite via `clients/database.py`
  3. Real terminal interaction happens via tmux client (`clients/tmux.py`)
  4. Provider-specific behavior is delegated through `providers/manager.py` + provider implementations (`q_cli`, `kiro_cli`, `claude_code`, `codex`)
- **Async coordination model**:
  - `handoff`: synchronous create/send/wait/read-output/exit flow
  - `assign`: fire-and-forget worker creation
  - `send_message`: queued inbox delivery between terminals
- **Inbox delivery path**:
  - Messages are persisted first (`inbox` table), then delivered when receiver becomes idle.
  - `watchdog` polls terminal log files (`tmux pipe-pane` output) and triggers delivery checks.
- **Flow scheduler**:
  - `flow_service` parses markdown frontmatter, evaluates cron via APScheduler, optionally executes scripts, renders prompt templates, then launches new sessions.

## Key repository conventions

- **Provider strings must match `ProviderType` enum values**: `q_cli`, `kiro_cli`, `claude_code`, `codex`.
- **Default provider is `kiro_cli`** unless explicitly overridden.
- **CAO tmux session naming**: managed sessions use `cao-` prefix (`SESSION_PREFIX`).
- **Terminal creation order matters**: create tmux session/window -> persist DB metadata -> initialize provider -> start `pipe-pane` logging.
- **Input delivery convention**: terminal input uses bracketed paste; provider decides Enter behavior via `paste_enter_count` (default 2).
- **Ready-state convention**: code frequently treats both `IDLE` and `COMPLETED` as acceptable pre-input ready states for providers.
- **Flow script contract**: script output must be JSON with both `execute` (bool) and `output` (object) keys.
- **Pytest defaults in `pyproject.toml`**:
  - `addopts = "--cov=src --cov-report=term-missing -m 'not e2e'"`
  - e2e tests are excluded unless explicitly selected.
