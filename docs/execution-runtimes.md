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
| Status detection | receives it | derives it beside the pane and pushes it |
| Agent profiles, provider files | no, for remote terminals | yes: resolved from the runtime's own profile store, written by its `create_terminal` |
| Workspace | no, for remote terminals | yes: a launch's `working_directory` is a path in the runtime |
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
the older one, which the server then closes and no longer listens to.

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
same as `terminal.{id}.status` events, which its status consumers (approval
prompts, inbox delivery) act on: each status the runtime pushes, `unknown` for
each of the runtime's terminals when its channel goes, and `unknown` for a
terminal whose record a hello drops. After a reconnect, the runtime pushes each
terminal's status again. A connection a newer one replaced publishes nothing.

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
  rather than leave it reporting Ready while disconnected.
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
  lists none drops all of their records. Two runtimes configured with the same
  id do the same to each other.
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
- A restarted `cao-bridge` process does not re-attach to tmux panes that
  outlived it, as a restarted `cao-server` does not for its local terminals:
  their status stays `unknown`, while input, output and delete still work.
  In the EKS example a restart of `cao-bridge` restarts its container, which
  takes tmux and those terminals with it: the bridge drops their rows, and the
  server their records when the restarted bridge says hello.

## Deploy

[`examples/cao-clusters/kubernetes/remote-runtime/`](../examples/cao-clusters/kubernetes/remote-runtime/README.md)
runs one server and one runtime on Amazon EKS.
