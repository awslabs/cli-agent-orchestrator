# backends

A backend is where CAO's agent terminals actually run. Status detection and inbox
delivery work differently on each one, so a check that passes on tmux can fail on herdr.
Know which backend you're on before you trust a status.

## What exists

- **`tmux`** (default, stable): `backends/tmux_backend.py`. CAO reads pane output and
  matches patterns to detect status; the inbox watchdog polls. See [tmux.md](../tmux.md).
- **`herdr`** (experimental): `backends/herdr_backend.py` and
  `services/herdr_inbox_service.py`. A CAO session is a herdr workspace, and a terminal is
  a tab. herdr reports agent status over a Unix socket, so inbox delivery is
  event-driven. See [herdr.md](../herdr.md).
- **`herdr-app`** (experimental): `backends/herdr_app_backend.py`. Terminals run in a
  herdr-core (`hc`) session that the Herdr desktop app can show. See
  [herdr-app.md](../herdr-app.md).
- **`BackendFactory`** picks one at startup (`backends/factory.py:27-107`).

## How a user reaches it

Precedence, highest first (`backends/factory.py:3-4`, `services/config_service.py:230-233`):

1. `cao-server --terminal tmux|herdr|herdr-app`
2. `CAO_TERMINAL_BACKEND`
3. `terminal.backend` in `settings.json` (or `cao config set terminal.backend <name>`)
4. `tmux`

Session names: `terminal.herdr_session` / `CAO_HERDR_SESSION` (default `cao`) for herdr,
and `terminal.herdr_app_session` / `CAO_HERDR_APP_SESSION` (default `cao`, never
`default`) for herdr-app.

## How to drive it

These use `mock_cli`, so no provider login is needed (see [providers](providers.md) for
putting it on `PATH`).

**tmux**, with a server started by plain `cao-server`:

```bash
curl -s http://localhost:9889/health | python3 -m json.tool | grep backend
```
```
    "terminal_backend": "tmux",
```

```bash
cao launch --agents developer --provider mock_cli --headless "hello map"
tmux list-sessions
cao shutdown --session cao-<id>
```
```
Session created: cao-<id>
Terminal created: developer-<suffix>
hello map
cao-<id>: 1 windows …
✓ Shutdown session 'cao-<id>'
```

**herdr** (captured on CAO 2.5.4 with herdr 0.9.3), with the server started by
`cao-server --terminal herdr`:

```bash
curl -s http://localhost:9889/health | python3 -m json.tool | grep -E 'backend|herdr'
```
```
    "terminal_backend": "herdr",
        "herdr": "ok",
        "herdr-core": "unavailable",
```

```bash
cao launch --agents developer --provider mock_cli --headless "hello herdr"
herdr --session cao workspace list
```
```
Session created: cao-<id>
Terminal created: developer-<suffix>
hello herdr
{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":[{"active_tab_id":"w1:t1","agent_status":"unknown","focused":true,"label":"cao-<id>","number":1,"pane_count":1,"tab_count":1,"workspace_id":"w1"}]}}
```

The workspace `label` is the CAO session name. Inbox delivery on herdr logs its trigger
(`trigger: herdr-event`, `herdr-settle`, `settled-sweep`, `reconcile-sweep`), so
`grep trigger: ~/.aws/cli-agent-orchestrator/logs/cao_*.log` shows whether the event path
is alive ([herdr.md](../herdr.md#event-driven-inbox-delivery)).

**herdr-app**, with a scratch `HOME` and `CAO_HOME_DIR`, and `hc` 0.1.22 on `PATH`:

```bash
export CAO_HERDR_APP_SESSION=<scratch-session> HC_SESSION=<scratch-session>
cao-server --port 19889 --terminal herdr-app
curl -s http://localhost:19889/health | python3 -m json.tool | grep -E 'backend|herdr'
```
```
    "terminal_backend": "herdr-app",
        "herdr": "unavailable",
        "herdr-core": "ok",
```

```bash
cao launch --agents developer --provider mock_cli --headless "hello app map"
cao session status cao-<id> --json
HC_SESSION=<scratch-session> hc ls
```
```
Session created: cao-<id>
Terminal created: developer-<suffix>
hello app map
{"session":"cao-<id>","conductor":{"id":"<terminal-id>","provider":"mock_cli","status":"completed","last_output":"hello app map",…}}
{"hidden_finished":0,"terminals":[{"name":"cao-<id>",…}]}
```

Stop only the scratch core with `HC_SESSION=<scratch-session> hc server stop`.
See [herdr-app.md](../herdr-app.md#enable-it) and its
[dedicated CAO window](../herdr-app.md#a-dedicated-cao-window).

Tests: `test/backends/` (for example `test_herdr_backend.py`,
`test_herdr_inbox_service.py`).

## What usually lies

- **A scratch `CAO_HOME_DIR` doesn't isolate the backend.** tmux uses your default tmux
  server ([tmux.md](../tmux.md#scrollback-depth-history-limit)). herdr finds its socket
  from `HOME` (or `XDG_CONFIG_HOME`) and the session name, so a scratch server with the
  default `cao` session talks to your real herdr session
  (`backends/herdr_backend.py:1205-1216`). For a fully separate run, also set `HOME` (or
  a different `terminal.herdr_session`) and `TMUX_TMPDIR`.
- **`CAO_TERMINAL_BACKEND` left over in your shell beats `settings.json`.** A shell or
  pane that exported it quietly changes the backend of every server and test run started
  there (`services/config_service.py:230`). Tests then fail in inbox and dispatch code
  that the diff never touched. Check `curl /health` for `terminal_backend`, and unset
  `CAO_*` and `HERDR_*` before a test run.
- **herdr.md says herdr must be running before `cao-server` starts. It needn't be.**
  CAO starts `herdr --session <name> server` itself when the socket is missing, and
  waits up to 15 s for it (`backends/herdr_backend.py:1218-1263`). The
  troubleshooting text at [herdr.md](../herdr.md#socket-connection-errors-in-cao-logs)
  predates this. herdr.md also lists `--terminal` as `tmux` or `herdr`; `herdr-app` is
  accepted too (`api/main.py:12737-12742`).
- **That herdr server outlives `cao-server`.** It's started in its own session
  (`start_new_session=True`, `backends/herdr_backend.py:1243-1248`). After a test run or
  a scratch server, a `herdr --session cao server` process stays behind. Find yours by
  the socket it holds open under your own `HOME`, and stop only that one. Seen on
  2026-11-20.
- **`--env` values that work on tmux can fail on herdr.** On herdr, a forwarded
  value with shell characters such as `$ ; | & ! * ? < >` fails terminal creation; tmux
  accepts it (`backends/herdr_backend.py:1294-1299`).
- **`cao session list` empty after a server restart on herdr.** It's usually expected:
  startup removes database rows whose herdr tabs are gone, and logs
  `Startup DB cleanup: removed N ghost terminal(s)`
  (`services/herdr_inbox_service.py:469`).
- **A session visible in herdr but not in CAO.** `terminal.herdr_session` doesn't match
  the session herdr runs under. Compare with `herdr --session <name> workspace list`
  ([herdr.md](../herdr.md#session-visible-in-herdr-but-not-in-cao)).
- **Only a short env allowlist reaches tmux agents.** `HOME`, `PATH`, `SHELL`, and the
  `CAO_*`, `KIRO_*`, `MISE_*` and `AWS_*` prefixes. Anything else needs
  `cao launch --env KEY=VALUE` ([tmux.md](../tmux.md#forwarding-env-vars-to-spawned-agents)).
- **CAO raises the global tmux `history-limit` to 50000.** Your own tmux windows on the
  same server get it too ([tmux.md](../tmux.md#scrollback-depth-history-limit)).
- **On herdr-app, a native `error` status is ignored.** Inbox delivery also waits about
  10 s after a turn ends, so "delivered at once" isn't what to expect
  ([herdr-app.md](../herdr-app.md#behavior-to-know-about)).
