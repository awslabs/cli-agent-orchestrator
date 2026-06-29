# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CLI Agent Orchestrator (CAO) is a local multi-agent orchestrator for AI coding CLIs (Claude Code, Kiro CLI, Codex, Gemini, Hermes, Kimi, Copilot, OpenCode, Cursor, Q CLI). Each agent runs as a *real CLI process inside its own tmux session*; CAO coordinates them with a supervisor–worker pattern. The orchestrator does not wrap provider APIs — it drives the actual terminal, so every CLI's native features and auth are preserved.

## Commands

Dev uses `uv`. Always prefix Python tooling with `uv run`.

```bash
uv sync                                                      # install deps (incl. dev group)
uv run cao --help                                            # the CLI entrypoint

# Tests — default addopts already excludes e2e and adds coverage
uv run pytest test/ --ignore=test/e2e -m "not integration" -v   # fast unit run (what CI gates on)
uv run pytest test/providers/test_claude_code_unit.py -v         # single file
uv run pytest test/providers/test_codex_provider_unit.py::TestCodexBuildCommand -v   # single class/test
uv run pytest -n auto                                            # parallel
uv run pytest -m integration -v                                 # integration (needs provider CLI installed+authed)
uv run pytest -m e2e test/e2e/ -v                              # e2e (needs running cao-server + tmux + authed CLIs)

# Quality (run all three before committing — CI enforces them)
uv run black src/ test/
uv run isort src/ test/
uv run mypy src/

# Regenerate captured provider-output fixtures when a CLI changes its TUI
uv run python test/providers/fixtures/generate_fixtures.py
```

Running the system locally: `cao-server` (starts FastAPI on `localhost:9889` + Web UI), then `cao launch --agents code_supervisor` in another terminal. `cao shutdown --all` to clean up. Sessions live in tmux — `tmux attach -t <session>` to watch/steer an agent live.

## Operating the server

CAO is one background daemon (`cao-server`) with the CLI/MCP/WebUI as thin REST clients. The daemon is managed *without leaving `cao`*:

- **Lifecycle:** `cao server start` (detached; no-op if already healthy — single-instance guard via `/health` + a pidfile at `~/.aws/cli-agent-orchestrator/server.pid`), `cao server stop`, `cao server restart`, `cao server status` (PID/port/health components/version). `cao server start --foreground` runs it blocking with live logs.
- **Auto-start:** `cao launch` starts the daemon automatically if it isn't running, so the common path never needs a separate `cao-server` terminal. Opt out with `cao launch --no-auto-start` (errors instead of spawning).
- **Health check:** `cao doctor` is *tiered*. Static (`cao doctor`) spawns nothing and costs no tokens: server reachability, per-provider binary install status over `PREFERRED_PROVIDERS`, discoverable profiles, and effective `settings.json` timeouts. Live (`cao doctor --live [--provider X]`) spawns a throwaway agent per installed provider and asserts it reaches IDLE within `provider_init_timeout` (real agents — costs tokens). It's the manual twin of the opt-in e2e smoke test (`test/e2e/test_provider_smoke.py`).
- **Logs:** `cao logs [--server] [--terminal <id>] [-f] [-n N]` tails the latest `cao_*.log` (server) or a terminal's per-terminal log — read the truth without `grep`/`tail`. Launch/send failures now name the relevant log and point at `cao logs --server` + `cao doctor --live`.

**Provider priority is `opencode_cli` (default) → `claude_code` → `codex`** (`PREFERRED_PROVIDERS` in `constants.py`). When no provider is explicitly chosen (no `--provider`, no profile `provider:`) and the default's CLI binary is missing, CAO downgrades to the first *installed* preferred provider and logs a warning — an explicit `--provider` (or a profile-declared `provider:`) is a deliberate choice and never downgrades (it errors loudly if absent).

**Timeout knobs** (`settings.json` → `server`, defaults bumped): `mcp_request_timeout` (120s), `provider_init_timeout` (90s). Raise these if a slow provider keeps masquerading as a launch failure (`cao doctor --live` reports its real time-to-IDLE).

**Driving agents:** prefer headless/async — `cao launch --headless --async` or `cao session send --async` — and attach via `tmux attach -t <session>` only to *observe/steer*, never as the control path (attaching mid-init can drop keystrokes; see issue #220). Pin providers explicitly (`--provider` or a profile `provider:`) for reproducible heterogeneous panels rather than relying on the install-aware default.

## Web UI

React + Vite + Tailwind in `web/`. The built bundle is a *build artifact* not committed to git — it lands in `src/cli_agent_orchestrator/web_ui/`. If `cao-server` returns `404 Not Found`, the bundle is missing: `cd web && npm install && npm run build`, then `uv tool install --reinstall .`. Dev mode: `npm run dev` (port 5173, proxies API to `:9889`). Node is only needed for frontend work — pure-Python usage installs from the wheel.

## Architecture

The system is **event-driven** around a central pub/sub `event_bus` with wildcard topic matching. The canonical pipeline:

```
tmux pane → FIFO (named pipe) → fifo_reader → publishes terminal.{id}.output
                                                  ├─ status_monitor → terminal.{id}.status (parses TUI to derive IDLE/PROCESSING/COMPLETED/ERROR)
                                                  ├─ log_writer → debug logs
                                                  └─ inbox_service (consumes .status) → delivers queued messages when terminal goes IDLE/COMPLETED
```

Request flow is layered: **entry points** (CLI / MCP servers) → **FastAPI HTTP API** (`api/main.py`, port 9889) → **services** (business logic) → **clients** (`tmux`, `database`/SQLite) and **providers** (per-CLI integration). The CLI commands and MCP tools are thin packagings of the REST API — there is one source of truth (the API), three control planes on top.

Key directories under `src/cli_agent_orchestrator/`:
- `api/` — FastAPI endpoints, the single backend surface. Localhost-only; validates `Host` header (DNS-rebinding guard).
- `services/` — all business logic. event_bus, fifo_reader, status_monitor, inbox_service, terminal_service, session_service, flow_service (cron scheduling), memory_service (cross-session agent memory), install_service, plugin_dispatch.
- `providers/` — one module per CLI (`claude_code.py`, `kiro_cli.py`, `codex.py`, …). Each subclasses `base.py` and teaches CAO that CLI's prompt detection, ready/trust-prompt handling, command construction, and tool-restriction translation. **This is where most provider-specific bugs live** — TUI output parsing is fragile and version-sensitive.
- `backends/` — abstraction over the session runtime: `tmux_backend.py` (default) vs `herdr_backend.py` (experimental, agent-aware, event-based instead of output polling). Selected via `factory.py`/`registry.py`.
- `clients/` — `tmux.py` (sets `CAO_TERMINAL_ID`, send_keys / bracketed-paste) and `database.py` (SQLite: terminals + inbox_messages).
- `mcp_server/` — `cao-mcp-server`: tools for agents *inside* a session (`handoff`, `assign`, `send_message`).
- `ops_mcp_server/` — `cao-ops-mcp-server`: tools for a primary agent *outside* CAO to manage sessions (install/launch/monitor).
- `cli/commands/` — `cao` subcommands (launch, session, shutdown, flow, install, skills, memory, terminal, env, info).
- `plugins/` — observer-only extensions reacting to server events (outbound: Discord/Slack/webhooks, memory injection). Registered via `cao.plugins` entry points in pyproject.
- `agent_store/` + `skills/` — bundled agent profiles (`.md` with frontmatter) and SKILL.md guides seeded into sessions.

### Orchestration primitives (how agents coordinate)
- **handoff** — synchronous: spawn a worker terminal, send task, block until COMPLETED, return output, then exit/delete the worker (scrollback snapshotted to `~/.aws/cli-agent-orchestrator/logs/` for `cao terminal restore`).
- **assign** — async fire-and-forget: spawn worker, inject the caller's terminal id, return immediately; worker calls `send_message` back when done.
- **send_message** — deliver to a terminal's inbox; held PENDING until the receiver is idle, then DELIVERED.

Every terminal has a unique `CAO_TERMINAL_ID` env var — the server routes all messages and tracks status by this id.

### State and home dir
Runtime state lives under `~/.aws/cli-agent-orchestrator/` (`CAO_HOME_DIR`): `db/` (SQLite), `logs/`, `fifos/` (named pipes), `agent-store/`, `skills/`, `memory/`. Port and paths are in `constants.py`; port overridable via `CAO_API_PORT`.

## Conventions & gotchas

- **black line-length 100, isort profile=black.** mypy config is in `mypy.ini` (note: `pyproject.toml` also has a `[tool.mypy]` block — `mypy.ini` is the effective per-module config with targeted error-code suppressions for SQLAlchemy/CLI/MCP modules).
- Adding a provider: implement a `providers/<name>.py` subclass of `base.py`, register it, add a `valid` provider value, and add `test_<name>_unit.py` (fixture-driven) + optionally `_integration.py`. Provider unit tests run in dedicated path-triggered CI workflows.
- **Q CLI is slated for deprecation** — don't build new workflows on it; default to Kiro CLI, Claude Code, or Codex.
- Behavior is heavily configurable via `CAO_*` env vars (e.g. `CAO_ENABLE_WORKING_DIRECTORY`, `CAO_ENABLE_SENDER_ID_INJECTION`); see `docs/settings.md`.
- Deep-dive docs live in `docs/` (one per provider, plus `event-driven-architecture.md`, `control-planes.md`, `terminal-lifecycle.md`, `tool-restrictions.md`, `memory.md`, `flows.md`). `CODEBASE.md` has the original architecture diagrams (slightly behind current dir layout). `DEVELOPMENT.md` is the full dev/test reference.
