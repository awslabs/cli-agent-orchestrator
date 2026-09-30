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

Set `CAO_LOCAL_EXECUTION=0` on a central server that must run no agents
itself. Every local terminal creation is then refused; the routes that would
start one (`POST /sessions`, `POST /sessions/{name}/terminals`,
`POST /terminals/run-step`) return `409`.

The runtime opens one WebSocket to `WS /runtime/channel`, outbound only, with
the shared token in the `x-cao-runtime-token` header. Both sides exchange a
hello carrying the protocol version. The runtime's hello also lists every
terminal it runs, with its current status, so a reconnect restores status
without replaying anything. A newer connection from the same runtime replaces
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
the runtime is disconnected, the status is `unknown`.

The server dispatches event plugin hooks for a remote terminal as for a local
one: a launch, once its row is recorded, emits `post_create_terminal` and
`post_create_session`, and a delete emits `post_kill_terminal` (and
`post_kill_session` for a session). The built-in memory event plugins skip a
remote terminal, since its working directory is in the runtime.

If the server cannot record a launched terminal, it sends `delete` to the
runtime. The `500` it then returns says whether the agent may still be
running. The server also deletes any terminal a runtime runs that it has no
record of: one listed in the runtime's hello, or one in a launch result that
arrives after the launch timed out or its connection dropped. A runtime that
starts an agent but cannot report it stops it, and the launch fails with `502`
naming the terminal. So a lost launch result cannot leave an agent running
unseen.

## Failure outcomes

| Status | Meaning | Safe to retry |
|---|---|---|
| `503` | The runtime is not connected. Nothing was sent. | yes |
| `504` | Sent, but no result arrived (timeout or disconnect). The outcome is unknown. | only if repeating the operation is harmless |
| `502` | The runtime ran the command and reported a failure; the detail carries its reason. | depends on the reason |

The server never resends a command by itself.

## Security

- A server with no token configured refuses every runtime. A wrong token is
  refused before the WebSocket is accepted (HTTP 403), and a different
  protocol version is refused after the hello (close code 1002). `cao-bridge`
  exits on either rather than retrying.
- The token is read once from `CAO_RUNTIME_TOKEN_FILE`, or from
  `CAO_RUNTIME_TOKEN`, which is then removed from the process environment.
  tmux never passes `CAO_RUNTIME_TOKEN` to a pane. The EKS image's entrypoint
  removes it before its setup steps (`cao init`, `cao install`, the provider
  warm-up) and passes it only to the `cao-bridge` or `cao-server` it finally
  starts.
- A command that has not been written when its connection is replaced or
  drops is never written; it fails with `503`.
- The token authenticates a runtime, not an agent. Agents in a runtime run as
  the same user as `cao-bridge`, so treat everything in a runtime as trusted
  with that token.
- The runtime token does not protect the HTTP API. Turn on API authentication
  on the server (`CAO_AUTH_LOCAL_TOKEN`, or an IdP; see
  [Configuration](configuration.md)), as the EKS example does, and give a
  runtime no API credential: agents there then cannot drive the API.
  `WS /runtime/channel` and `/health` need no bearer.

## Known limits of this slice

- Inbox delivery, supervisor delegation through the MCP server, flows and
  workflows target local terminals only.
- `GET /sessions` and the CLI's shared-server commands do not list remote
  terminals; `GET /runtimes` does.
- Output is read on request. There is no output streaming or replay for a
  remote terminal, and the PTY WebSocket refuses one (close code `4004`).
- The memory context added to a remote terminal's first input is resolved
  in the runtime, not from the server's memory store.

## Deploy

[`examples/cao-clusters/kubernetes/remote-runtime/`](../examples/cao-clusters/kubernetes/remote-runtime/README.md)
runs one server and one runtime on Amazon EKS.
