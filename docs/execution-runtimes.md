# Execution runtimes

An execution runtime runs agents for a central `cao-server` that runs none
itself ([#745](https://github.com/awslabs/cli-agent-orchestrator/issues/745)).
This is the first slice: one server, runtimes that dial in, and every
terminal operation routed to the runtime that runs the terminal. Local mode
is unchanged: a terminal with no runtime is served from the server's own tmux
exactly as before.

## What runs where

| | `cao-server` | Execution runtime (`cao-bridge`) |
|---|---|---|
| HTTP API, authentication | yes | no; it serves nothing |
| Central SQLite state | yes, one row per terminal, naming its runtime | pane bookkeeping only |
| tmux, provider CLIs, agents | no, for remote terminals; none at all with `CAO_LOCAL_EXECUTION=0` | yes |
| Status detection | receives it | derives it beside the pane and pushes it; rereads a terminal still `processing` every 3 s, as the server's polls do locally |
| Agent profiles, provider files | no, for remote terminals | yes: resolved from the runtime's own profile store, written by its `create_terminal` |
| Workspace | no, for remote terminals | yes: a launch's `working_directory` is a path in the runtime; the central row records the directory the runtime resolved |
| Provider credentials | no, for remote terminals | yes: the runtime's own (in the EKS example, its service account's IAM role) |
| Terminal logs and output | no, for remote terminals | yes: read on request through the server |
| Flow pre-scripts, Python workflow scripts | yes, even with `CAO_LOCAL_EXECUTION=0` | no, not in this slice |

Set `CAO_LOCAL_EXECUTION=0` on a central server that must run no agents
itself. Every local terminal creation is then refused; the routes that would
start one (`POST /sessions`, `POST /sessions/{name}/terminals`,
`POST /terminals/run-step`) return `409`. The setting covers agents only: a
flow's pre-script and a Python workflow script still run on the server host,
as the server's user. The flow's session launch, or the workflow's agent
steps, are then refused. Moving that user code to an execution runtime, as
[#745](https://github.com/awslabs/cli-agent-orchestrator/issues/745) requires,
is a later slice; until then `CAO_LOCAL_EXECUTION=0` does not keep user code
off the server.

The runtime opens one WebSocket to `WS /runtime/channel`, outbound only, with
the shared token in the `x-cao-runtime-token` header. Its runtime id (for
`cao-bridge`, `CAO_BRIDGE_RUNTIME_ID`) is addressed as one URL path segment, so
it is letters, digits, `.`, `_` and `-`; a Kubernetes pod name fits. Both sides exchange a
hello carrying the protocol version. The runtime's hello also lists every
terminal it runs, with its current status, so a reconnect restores status
without replaying anything. A terminal the server has recorded on that runtime
but the hello does not list is gone, lost with the runtime's previous state
(its pod was replaced, or `cao-bridge` restarted without the terminal's tmux),
so the server drops its record: `GET /terminals/{id}` answers `404`, and
`GET /runtimes` no longer lists it. While the runtime is away, its records
stay, reading `unknown`. That covers a dropped connection; a restarted
`cao-bridge` process that kept its tmux is different (see
[Known limits](#known-limits-of-this-slice)).
A newer connection from the same runtime replaces
the older one, which the server then closes and no longer listens to. A
runtime's hellos are reconciled one at a time: a newer connection's hello
waits for an older one's, and one replaced while it is being handled drops no
further record (the newer connection registers only once a drop in progress
has landed), so the newer hello decides which records stay.

## Running `cao-bridge`

You need `cao-bridge` only where agents are to run apart from the server: in
another pod, on another machine, or beside a server started with
`CAO_LOCAL_EXECUTION=0`. Local mode needs none: a `cao-server` on its own runs
agents in its own tmux, as before.

`cao-bridge` is a console script installed with CAO, like `cao-server`. It
serves nothing: it dials the server, runs the commands it is sent against the
tmux beside it, and pushes each terminal's status back. Give it its own
`CAO_HOME_DIR`, since the server's database is not the runtime's, and its own
provider credentials and agent profiles, since launches resolve them there.

| Environment variable | Side | Default | Purpose |
|---|---|---|---|
| `CAO_RUNTIME_TOKEN_FILE` | both | *(none)* | File holding the shared runtime token. Read first; when it is set, only the file counts. |
| `CAO_RUNTIME_TOKEN` | both | *(none)* | The token itself, when no file is set. Both are removed from the process environment once read. |
| `CAO_BRIDGE_SERVER_URL` | runtime | *(required)* | The server's channel, `ws://` or `wss://`, e.g. `ws://cao-server:9889/runtime/channel`. |
| `CAO_BRIDGE_RUNTIME_ID` | runtime | *(required)* | This runtime's id: letters, digits, `.`, `_` and `-`, starting with a letter or digit, at most 128 characters. In the EKS example, the pod name. |
| `CAO_BRIDGE_READY_FILE` | runtime | `$CAO_HOME_DIR/bridge-ready` | Exists only while the channel is up; a readiness probe can test for it. |
| `CAO_HOME_DIR` | both | `~/.aws/cli-agent-orchestrator` | Each side's state; give the runtime its own. |
| `CAO_LOCAL_EXECUTION` | server | on | `0` (or `false`, `no`, `off`) and the server starts no agents itself. |
| `CAO_RUNTIME_LAUNCH_TIMEOUT` | server | `240` | Seconds a launch may take before it is reported as `504`. |
| `CAO_NODE_MODE` | runtime | `server` | In the EKS image only: `bridge` makes its entrypoint start `cao-bridge` instead of `cao-server`. |

To try it on one machine, start a server that runs no agents and a runtime
beside it, each with its own state:

```bash
openssl rand -hex 32 > /tmp/cao-runtime-token

CAO_HOME_DIR=/tmp/cao-server CAO_API_PORT=9889 CAO_LOCAL_EXECUTION=0 \
  CAO_RUNTIME_TOKEN_FILE=/tmp/cao-runtime-token cao-server &

CAO_HOME_DIR=/tmp/cao-runtime CAO_BRIDGE_RUNTIME_ID=laptop \
  CAO_BRIDGE_SERVER_URL=ws://127.0.0.1:9889/runtime/channel \
  CAO_RUNTIME_TOKEN_FILE=/tmp/cao-runtime-token cao-bridge &

curl -s http://127.0.0.1:9889/runtimes      # lists "laptop"
curl -s -X POST http://127.0.0.1:9889/runtimes/laptop/terminals \
  -H 'Content-Type: application/json' \
  -d '{"agent_profile": "developer", "provider": "claude_code"}'
```

The terminal's tmux session is the runtime's, and every operation on it goes
through the server's API as in [Commands](#commands). Both processes here
share your tmux server; nothing else is shared. The `mock_cli` provider needs
its binary on the runtime's `PATH` (it is
`test/providers/fixtures/bin/mock_cli` in this repository, and is installed
in the EKS image).

Once started, `cao-bridge`:

- connects, sends its hello and reports Ready (the ready file) once the
  server answers. It logs each connect, reconnect and refusal at INFO and
  above to stderr, so `kubectl logs` shows them, and in full to its log file
  under `$CAO_HOME_DIR/logs`.
- reconnects after a lost channel, with backoff from 1 s doubling to 30 s,
  reset once a connection has held for 10 s. While it is away it is not
  Ready, and the server reads its terminals as `unknown`.
- exits, rather than retrying, on a configuration it cannot use (a missing
  setting, an invalid URL or runtime id), a refused token (HTTP 401 or 403),
  a different protocol version, or a ready file it cannot remove.
- at startup, forgets terminals whose tmux session is gone (lost with an
  earlier container), so its first hello lists only what it runs.
- runs at most 1024 terminals, the most a hello may list. A runtime that
  runs more logs an error and keeps retrying without dialling, until some
  are stopped.
- rereads, every 3 s, the status of each terminal it last reported as
  `processing`, and reports it once it has changed.

Run it under an init that reaps child processes: agents leave finished
processes behind, and `cao-bridge` does not reap them. The EKS example runs it
under `tini`. See [Security](#security) for what the shared token allows.

## Commands

The server sends one command per operation and waits for its result, which it
matches by `op_id`. The runtime runs commands for one terminal in arrival
order; different terminals run concurrently.

| HTTP operation | Command | Runtime action |
|---|---|---|
| `POST /runtimes/{runtime_id}/terminals` | `launch` | start the agent in a new tmux session |
| `POST /terminals/{id}/input` | `input` | send the message, keeping sender and orchestration type |
| `POST /terminals/{id}/key` | `key` | send one special key |
| `GET /terminals/{id}/output` | `output` | read `full` or `last` output |
| `GET /terminals/{id}/working-directory` | `working_directory` | read the pane's working directory |
| `POST /terminals/{id}/exit` | `exit` | exit the provider CLI |
| `DELETE /terminals/{id}`, `DELETE /sessions/{name}` | `delete` | tear the terminal down, then the server drops its row |

`GET /terminals/{id}` answers from the status the runtime last pushed. While
the runtime is disconnected, the status is `unknown`. The server publishes the
same as `terminal.{id}.status` events, which its status consumers (such as
approval prompts) act on: each status the runtime pushes, and each status a
hello restores; `unknown` for each of the runtime's terminals when its channel
goes; and `unknown` for a terminal whose record a hello drops, or cannot drop.
A connection a newer one replaced publishes nothing.

Each command for a terminal names the session of the central row it was
routed by, and the runtime acts only on its terminal under that id in that
session. A command that outlived its terminal therefore never reaches a newer
terminal reusing the id: a delete of one no longer there succeeds, and any
other command fails with `502`.

Operations that work only on the server's own panes and logs refuse a remote
terminal with `409` rather than act on nothing: reusing it in
`POST /terminals/run-step`, `POST /terminals/{id}/inbox/messages` to it (a
queued message would never be delivered), and
`GET /terminals/{id}/output/range` (its log is in the runtime).

A launch may take up to `CAO_RUNTIME_LAUNCH_TIMEOUT` seconds (a server
setting; 240 by default) before it is reported as `504`. Set it above the
longest provider start-up the runtime's profiles allow.

The server dispatches event plugin hooks for a remote terminal as for a local
one: a launch, once its row is recorded, emits `post_create_terminal` and
`post_create_session`, and a delete emits `post_kill_terminal` (and
`post_kill_session` for a session). The built-in memory event plugins skip a
remote terminal, since its working directory is in the runtime.

If the server cannot record a launched terminal, it sends `delete` to the
runtime. The `500` it then returns says whether the agent may still be
running. That delete goes only to the runtime instance that ran the launch:
if another instance has connected under the same runtime id meanwhile, the
outcome is reported as unconfirmed, and the terminal is deleted when the
original instance reconnects and lists it. The server also deletes any
terminal a runtime runs that it has no
record of: one listed in the runtime's hello, or one in a launch result that
arrives after the launch timed out or its connection dropped. It retries that
delete until the runtime confirms it, or the connection ends (the runtime's
next hello lists the terminal again). A runtime that
starts an agent but cannot report it stops it, and the launch fails with `502`
naming the terminal. If it cannot stop it either, its failed result names the
terminal, and the server deletes it as one it has no record of. So a lost
launch result cannot leave an agent running
unseen. Nor can a cancelled request: once the agent runs, recording it (or
undoing it) completes even if the caller goes away.

The server checks a launch result before recording it. An invalid one fails
the launch with `502`, after deleting the terminal it names, if any. A session
name or terminal id already in use, on another runtime or on the server
itself, fails it with `409`, after deleting the new terminal; runtimes pick
session names independently, so a retry gets a fresh one. For the same
reason, a local session cannot be created under the name of a remote one, a
delete of a session a runtime recorded is always carried out in that runtime,
and the server's stale-row and retention sweeps leave remote rows alone.

## Failure outcomes

| Status | Meaning | Safe to retry |
|---|---|---|
| `503` | The runtime is not connected, or did not take the command within its timeout. Nothing was sent. | yes |
| `504` | Sent, but no result arrived (timeout or disconnect). The outcome is unknown. | only if repeating the operation is harmless |
| `502` | The runtime ran the command and reported a failure; the detail carries its reason. | depends on the reason |

The server never resends a caller's command by itself. Its own compensating
`delete` of a terminal it has no record of is the exception: that one is
retried until the runtime confirms it (see [Commands](#commands)).

## Security

- A server with no token configured refuses every runtime. A wrong token is
  refused before the WebSocket is accepted (HTTP 403), and a different
  protocol version is refused after the hello (close code 1002). `cao-bridge`
  exits on either rather than retrying.
- `cao-bridge` reports Ready through its readiness file
  (`CAO_BRIDGE_READY_FILE`) only while its channel is up. A readiness file it
  cannot remove, at startup or once the channel goes, stops it with an error
  rather than leave it reporting Ready while disconnected. That is deliberate
  even for a transient error: its restart removes the file, or refuses to
  start. An agent in the runtime (the same user) can therefore keep it from
  starting, as it can with anything else the token is trusted with (below).
  A readiness file it cannot write only logs a warning: the runtime then
  reads not Ready while connected.
- A hello lists at most 1024 terminals; the server refuses a larger one
  (close code 1002), so one hello cannot make the server start unbounded
  work. `cao-bridge` never sends one: a runtime running more logs an error
  and retries until enough are stopped. So a runtime runs at most 1024
  terminals: a launch past that, counting launches in flight, answers `429`
  (and `cao-bridge` itself refuses one, `502`). The bound on hellos is per
  hello: reconnects are not rate-limited, so a token holder can still
  make the server start up to 1024 cleanups each time it reconnects.
- The token is read once from `CAO_RUNTIME_TOKEN_FILE`, or from
  `CAO_RUNTIME_TOKEN`; both are then removed from the process environment.
  When `CAO_RUNTIME_TOKEN_FILE` is set, only the file counts: if it is
  unreadable (logged as an error) or empty, the process has no token even
  with `CAO_RUNTIME_TOKEN` set, so the server refuses every runtime and
  `cao-bridge` exits.
  `cao-server` reads it first thing at startup, before any event plugin loads.
  tmux never passes either variable to a pane. The EKS image's entrypoint
  removes both forms before its setup steps (`cao init`, `cao install`, the
  provider warm-up) and passes them only to the `cao-bridge` or `cao-server`
  it finally starts.
- A command that has not been written when its connection is replaced or
  drops is never written; it fails with `503`.
- The token authenticates a runtime, not an agent. Agents in a runtime run as
  the same user as `cao-bridge`, so treat everything in a runtime as trusted
  with that token.
- Nor does the token tie a runtime to its id: the id is whatever its hello
  says. Anyone holding the token can connect as another runtime's id. That
  replaces and closes the other runtime's connection, and the impostor then
  receives the commands for that runtime's terminals and reports their status.
  Its hello also decides which of those terminals the server keeps: one that
  lists none drops all of their records. Dropping a record does not stop the
  agent in the runtime, but once the genuine runtime reconnects and lists that
  terminal again, the server has no record of it and deletes it there. So the
  token lets its holder tear down another runtime's agents. Two runtimes
  configured with the same id do the same to each other. The server
  serialises a runtime's record drops with its reconnects, so no drop lands
  after a newer connection has registered; but a record already dropped by
  then is gone, and the newer hello cannot restore it.
  Until per-runtime credentials arrive in a later slice, give the token only
  to runtimes that may act for one another.
- The runtime token does not protect the HTTP API. Turn on API authentication
  on the server (`CAO_AUTH_LOCAL_TOKEN`, or an IdP; see
  [Configuration](configuration.md)), as the EKS example does, and give a
  runtime no API credential: agents there then cannot drive the API.
  `WS /runtime/channel` and `/health` need no bearer.
- The channel adds no encryption of its own. Over `ws://`, as in the EKS
  example, the token and every frame (agent input, captured output, status)
  cross the network between runtime and server in cleartext, so the example
  relies on that network being trusted: its NetworkPolicy limits who can
  connect, not who can read the traffic. Where the network is not trusted,
  terminate TLS in front of `cao-server` and give `cao-bridge` a `wss://` URL.

## Known limits of this slice

- Inbox delivery, supervisor delegation through the MCP server, flows and
  workflows target local terminals only. A message to a remote terminal is
  refused (`409`), so `send_message` reports the failure.
- `GET /sessions` and the CLI's shared-server commands do not list remote
  terminals; `GET /runtimes` does. `GET /sessions/{session_name}` reads a
  remote session by name, with each terminal's status from its runtime.
- Output is read on request. There is no output streaming or replay for a
  remote terminal: `GET /terminals/{id}/output/range` answers `409`, and the
  PTY WebSocket refuses one (close code `4004`).
- The memory context added to a remote terminal's first input is resolved
  in the runtime, not from the server's memory store.
- Agents on one runtime are not isolated from one another: they run as the
  same user, in the same filesystem, and can read the runtime token. To keep
  workers apart, give each its own runtime (one terminal per runtime).
- A restarted `cao-bridge` process does not re-attach to tmux panes that
  outlived it, as a restarted `cao-server` does not for its local terminals.
  What follows depends on what the restarted bridge's hello lists:
  - Its tmux survived, and with it the runtime's rows: the hello lists those
    terminals, so the server keeps their records. Their status stays
    `unknown` (the restarted bridge reads no status for a pane it did not
    start, so its hello reports `unknown`), while input, output and delete
    still work.
  - Its tmux did not (in the EKS example a restart of `cao-bridge` restarts
    its container, which takes tmux and those terminals with it): the bridge
    drops their rows, its hello does not list them, and the server drops
    their records (`404`, and `unknown` is published).
  - The runtime lost its rows but kept its tmux (its state was removed by
    hand, say): the hello does not list those terminals either, so the server
    drops their records, but their agents keep running in the runtime with
    nothing tracking them. Stop them there.

## Deploy

[`examples/cao-clusters/kubernetes/remote-runtime/`](../examples/cao-clusters/kubernetes/remote-runtime/README.md)
runs one server and one runtime on Amazon EKS.
