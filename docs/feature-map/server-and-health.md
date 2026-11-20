# server-and-health

Everything else in CAO goes through one local HTTP server. If it's down, on the wrong
home, or on the wrong backend, every later check fails in confusing ways, so check it
first.

## What exists

- **`cao-server`**: the FastAPI server (`src/cli_agent_orchestrator/api/main.py`). It
  listens on `127.0.0.1:9889` by default (`CAO_API_HOST`, `CAO_API_PORT`;
  `constants.py:499-500`).
- **The CAO home**: `~/.aws/cli-agent-orchestrator`, or `CAO_HOME_DIR` if set
  (`constants.py:153-158`). It holds the database (`db/`), the logs (`logs/`, with one
  file per terminal under `logs/terminal/`), `settings.json` and the server lock.
- **The server lock**: `server.lock` in the CAO home. One `cao-server` per home
  (`utils/server_lock.py:57`).
- **`cao init`**: creates the database, repairs memory and seeds the built-in skills
  (`cli/commands/init.py:148-160`).
- **`cao info`**: prints the database path and, inside a CAO session, that session's
  terminal count (`cli/commands/info.py:33-78`).
- **`GET /health`**: the server's version, active terminal backend and a few
  binary probes (`api/main.py:2285-2337`).
- Settings and env vars: [configuration.md](../configuration.md). API reference:
  [api.md](../api.md).

## How a user reaches it

- `cao-server` (flags: `--host`, `--port`, `--terminal tmux|herdr|herdr-app`,
  `--agents-dir`, `--decision <point>=off|shadow|on`).
- `cao init`, `cao info`, `cao --version`.
- `curl http://localhost:9889/health`.
- The web UI's server card reads `version` from `/health`.

## How to drive it

```bash
cao --version
```
```
cao, version 2.5.4
```

```bash
cao init
```
```
memory_repair mode=apply total=0 repaired=0 unchanged=0 skipped=0 failed=0
CLI Agent Orchestrator initialized successfully. Seeded 16 builtin skills.
```

Start the server in its own terminal (`cao-server`), then:

```bash
curl -s http://localhost:9889/health | python3 -m json.tool
```
```json
{
    "status": "ok",
    "service": "cli-agent-orchestrator",
    "version": "2.5.4",
    "terminal_backend": "tmux",
    "features": ["delegation_ceiling"],
    "provider_init_timeout": 60,
    "components": {
        "cao": "ok",
        "herdr": "unavailable",
        "herdr-core": "unavailable",
        "claude": "unavailable"
    }
}
```

Expect `status: ok` and the backend you meant. The `version` should match
`cao --version`.

```bash
cao info
```
```
Database path: ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db
Not currently in a CAO session.
```

Check the one-server-per-home rule by starting a second server on another port:

```bash
cao-server --port 9890
```
```
cao: another server (PID 12345, port 9889, started 2026-11-20T02:55Z) already owns
     ~/.aws/cli-agent-orchestrator
     Stop it, or run this instance with a different CAO_HOME_DIR and --port.
     Two servers on one data directory corrupt inbox delivery and terminal state.
…
ERROR:    Application startup failed. Exiting.
```

Tests: `test/api/`, `test/utils/test_server_lock.py`, `test/api/test_server_lock_lifespan.py`.

## What usually lies

- **`components.claude: "ok"` doesn't mean Claude Code works.** Each component is
  `shutil.which()` on the server's own `PATH`, nothing more: no login, no version
  check (`api/main.py:2292-2293`, `2331-2335`). `claude` is the only provider probed
  there. For every provider, use `GET /agents/providers` (see
  [providers](providers.md)).
- **`components.herdr: "ok"` doesn't mean a herdr server is running.** It only means the
  `herdr` binary is on `PATH`. What decides the backend is `terminal_backend`
  (`api/main.py:2297-2302`).
- **A second server is refused even on a different port.** The lock is per CAO home, not
  per port (`utils/server_lock.py:57`; `api/main.py:1756-1769`). For a scratch server,
  set both `CAO_HOME_DIR` and `--port`.
- **The refused server exits with status 3, not 1.** The lock code raises
  `SystemExit(1)` (`utils/server_lock.py:223-226`), but it runs inside uvicorn's startup,
  and uvicorn reports a failed startup as exit status 3. A script that checks for `1`
  misses it. Seen on 2026-11-20 on CAO 2.5.4.
- **Inside a CAO agent pane, `cao-server` and `cao` point at the parent server.** CAO
  passes its own `CAO_API_HOST`, `CAO_API_PORT` and `CAO_HOME_DIR` to every pane
  (`utils/mcp_resolution.py:43-59`), and the tmux allowlist lets any `CAO_*` variable
  through ([tmux.md](../tmux.md#forwarding-env-vars-to-spawned-agents)). So a test or
  scratch server started from an agent pane uses the parent's home, unless you set these
  variables yourself.
- **`cao info` is not a health check.** It calls the server only when the current tmux
  session (or `CAO_SESSION_NAME`) starts with `cao-`. Anywhere else it prints "Not
  currently in a CAO session." whether a server is running or not
  (`cli/commands/info.py:43-78`).
- **`cao-server --host ::` can't be reached by its own agents.** The server listens
  IPv6-only, and the address CAO passes to agents can't express an IPv6 host. Use
  `0.0.0.0`; agents are then pointed at `127.0.0.1` (`api/main.py:12507-12521`).
  Checked live on 2026-10-26.
- **Swapping the Python environment under running `cao-mcp-server` or `cao-tui`
  processes breaks their lazy imports.** Restart them after an upgrade; matching the
  Python version is not enough. Unverified: reported from use, and nothing in this repo backs it.
