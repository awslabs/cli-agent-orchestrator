# Remote runners

[EXPERIMENTAL] With `terminal.backend = "remote"` (#745), one `cao-server`
keeps everything CAO knows and runs no terminal itself. Every terminal it
creates is placed on a `cao-runner`: a small execution-only process that
drives tmux on its own host or pod and streams the pane's bytes back. Nothing
above the terminal backend changes. Assign, handoff, `send_message`, the inbox,
status detection and the HTTP API work as they do locally, because they still
run in the one server and see an ordinary `TerminalBackend`.

The Kubernetes example
[`examples/cao-clusters/kubernetes/remote-runners/`](../examples/cao-clusters/kubernetes/remote-runners/README.md)
runs one server, a supervisor runner and a pool of worker runners on a local
kind cluster.

## What runs where

| | `cao-server` (one) | `cao-runner` (any number) |
|---|---|---|
| HTTP API, MCP tool targets, auth | yes | no |
| SQLite registry (`terminals.runner_id` records the placement) | yes | no |
| Provider adapters, profile resolution, status detection, inbox | yes | no |
| Placement: claims, pool, `max_terminals` | yes (the runner hub) | announces its own limits |
| tmux and the provider CLI (the agent) | no | yes |
| Provider files (`{terminal_id}*` in `CAO_HOME_DIR/tmp`) | written here | staged copies, by name |
| The agent's in-session `cao-mcp-server` | no | yes, calling the server's API |

The runner dials out to the server's **runner hub**, a WebSocket listener on
its own port (`9891` by default), authenticates with a shared token and says
what it can host. Every backend call the server makes (create a window, send
keys, read history, arm a pipe, kill) becomes one request on that connection.
Pane output comes back as numbered frames on the same connection and is written
into the server's local FIFO, where the unchanged `FifoManager` /
`StatusMonitor` / log pipeline consumes it.

The hub has its own port, thread and event loop on purpose. Backend calls are
synchronous, and some are made on the API's event loop; if their replies had
to arrive on that same loop, the call would block the loop that has to deliver
its own reply. A separate port also lets network policy admit runners to `9891`
and clients to `9889` independently.

## Configuration

### Server (`cao-server --terminal remote`)

| Env var | Default | Purpose |
|---|---|---|
| `CAO_TERMINAL_BACKEND` | `tmux` | `remote` selects this backend (or `cao-server --terminal remote`). |
| `CAO_TERMINAL_REMOTE_PROVIDERS` | `claude_code,mock_cli` | Providers the remote backend will launch; any other is refused before anything is created. |
| `CAO_RUNNER_TOKEN_FILE` | *(none)* | File holding the runner token, e.g. a mounted Secret. Wins over `CAO_RUNNER_TOKEN`. |
| `CAO_RUNNER_TOKEN` | *(none)* | The token itself, when no file is used. One of the two is required. |
| `CAO_RUNNER_BIND_HOST` | `127.0.0.1` | Address the hub listens on; `0.0.0.0` for runners in other pods. |
| `CAO_RUNNER_PORT` | `9891` | Hub port. |
| `CAO_RUNNER_TLS_CERT` / `CAO_RUNNER_TLS_KEY` | *(none)* | Serve `wss://` with this certificate chain and key. A key without a certificate is a startup error. |
| `CAO_RUNNER_AGENT_API_HOST` / `CAO_RUNNER_AGENT_API_PORT` | *(none)* | Address an agent on a runner uses to reach this server, written into its MCP config. |
| `CAO_RUNNER_CALL_TIMEOUT` | `30` | Seconds to wait for a runner's reply before the outcome is unknown (`504`). |
| `CAO_RUNNER_PLACEMENT_TIMEOUT` | `60` | Seconds a new terminal waits for a runner with room (`503` after). |
| `CAO_RUNNER_EXPIRY` | `600` | Seconds a disconnected runner stays a member (see [Restart](#restart-and-reconnect)). |
| `CAO_RUNNER_RESERVED_SESSIONS` | *(none)* | Session names only a claiming runner may host. |

### Runner (`cao-runner`)

| Env var | Default | Purpose |
|---|---|---|
| `CAO_RUNNER_SERVER_URL` | *(required)* | Hub URL: `ws://host:9891`, or `wss://host:9891` for TLS. |
| `CAO_RUNNER_TOKEN_FILE` / `CAO_RUNNER_TOKEN` | *(one required)* | As on the server; the file wins. |
| `CAO_RUNNER_CA_FILE` | system trust store | CA bundle the hub's certificate is verified against (`wss://` only). |
| `CAO_RUNNER_ID` | hostname | Stable id, `[a-z0-9-]`, at most 63 characters. |
| `CAO_RUNNER_CLAIMS` | *(none)* | Sessions only this runner may host. |
| `CAO_RUNNER_ACCEPTS_UNCLAIMED` | `1` | Whether it also takes unclaimed terminals (pool runner). |
| `CAO_RUNNER_MAX_TERMINALS` | `1` | Terminals it hosts at most. |
| `CAO_RUNNER_READY_FILE` | `CAO_HOME_DIR/runner-ready` | Exists exactly while the runner is connected to the hub; `''` disables it. Point an exec `readinessProbe` at it. |

The full configuration reference is [Configuration](configuration.md#environment-variable-reference).

## Failure outcomes

A backend call on the remote backend has four possible results, and the HTTP
routes that reach the backend (`POST /sessions`, `POST
/sessions/{name}/terminals[:operator]`, `GET`/`DELETE /sessions/{name}`, and
`GET /terminals/{id}`, `…/working-directory`, `…/input`, `…/key`, `…/output`,
`…/output/range`, `…/exit`, `DELETE /terminals/{id}`, `POST
/terminals/{id}/inbox/messages`) keep them apart, including when a service
re-raised the runner's error as its own (session teardown does):

| Status | Class | Meaning | Safe to retry |
|---|---|---|---|
| `503` | `RunnerUnavailable` | The runner is not connected, or no runner had room within `CAO_RUNNER_PLACEMENT_TIMEOUT`. Nothing was sent. | yes |
| `504` | `RunnerOutcomeUnknown` | Sent, but no reply within `CAO_RUNNER_CALL_TIMEOUT`, or the connection dropped first. It may or may not have run. | only if repeating it is harmless |
| `502` | `RunnerReportedError` | The runner ran it and reported a failure (e.g. tmux could not be read); the detail carries the runner's reason. | depends on the reason |
| status `unknown` | — | The terminal's runner is out of reach. `GET /terminals/{id}` reports `unknown` rather than the last status parsed before the runner went away, so a stale `processing` or `completed` is never presented as current. It clears when the runner reports the terminal again. | — |

The ordinary meanings are unchanged: a window that is gone is still `404`, an
invalid working directory still `400`. The server never resends a call on its
own. Where it gave up on a terminal it may have left running — a create whose
reply was lost, a kill the runner did not confirm, a terminal whose runner
expired — the terminal goes into the **reap set** (persisted in
`CAO_HOME_DIR/runner-hub.json`) and is killed the next time its runner reports
it. Teardown of such a terminal reports `unknown`, not `deleted`.

Every window-scoped call carries the terminal id its route was resolved for,
and the runner refuses it as "window not found" (`404`) when that window now
belongs to a different terminal; a kill refused that way kills nothing. A late or retried call can therefore never land on a newer terminal
that reused the window name.

## Restart and reconnect

The registry row is the durable record of placement (`terminals.runner_id`,
written when the create succeeds). The runner's own report is the record of
what is actually running. They are reconciled like this:

1. **Server restart.** Every row with a runner is seeded back as a route before
   the hub accepts a connection, and its runner becomes a member that has not
   reported yet. Until it does, those terminals read `unknown`, and nothing a
   member might host is reported absent.
2. **The report wins.** When a runner reports a window the server has placed
   on a different runner, the reporting runner holds the pane: the route moves
   to it and the row is corrected. A row that names no runner (written before
   the column existed) is filled in from the report the same way.
3. **Gone from its runner.** A placed terminal its runner no longer reports
   (in a hello, or in two consecutive inventories) loses its route and reads
   `unknown`. The row is left to ordinary teardown.
4. **No row.** A terminal a runner reports that has no registry row is
   unowned: it is reaped after two consecutive reports. Never within 60 s of
   a placement this server process made (the row is written right after the
   create returns), and never while the registry cannot be read. After a
   server crash between a create and its row, nothing will ever write that
   row, so the terminal is reaped once its runner reports it twice.
5. **Expiry.** A runner silent for `CAO_RUNNER_EXPIRY` is dropped from
   membership; its terminals are declared lost (reaped if it returns) and stay
   `unknown`.

A runner that reconnects (same process, after a network blip) keeps its pipes
and re-sends the output it buffered meanwhile (up to 4 MiB; a gap beyond that
is logged). A **restarted** runner process whose tmux server survived
re-adopts the windows tagged with its own id (`@cao_runner_id`, next to
`@cao_terminal_id`), and the hub re-arms their output pipes at once, so status
detection resumes from live bytes. Windows tagged by any other runner, or not
tagged (a local CAO sharing the tmux server), are never adopted.

A newcomer presenting the id of a runner that is still connected is refused,
not swapped in: the shared token would otherwise let any holder take over
another runner's terminals. The cost is that a genuine runner whose old
connection is half-open is refused until the hub's ping times that connection
out, up to `PING_INTERVAL + PING_TIMEOUT` (40 s).

## Security

- The hub refuses to start without a token, compares it in constant time,
  and rejects a wrong one before the WebSocket is accepted (`401`). A runner
  refused for its token logs an error and keeps retrying with backoff (the
  operator fixes the Secret; the pod reads not ready meanwhile); one refused
  for its protocol version or hello exits, since retrying cannot fix that.
- The token is read once, from `CAO_RUNNER_TOKEN_FILE` if set (an unreadable
  file means *no* token, never a fallback to `CAO_RUNNER_TOKEN`), and both
  variables are then removed from the process environment. `cao-server` does
  so first thing at startup, before anything it starts can inherit them;
  `cao-runner` before its first pane. Prefer the file: an environment value
  stays readable in `/proc/<pid>/environ` for the life of the process.
- The token authenticates a runner, not an agent. Agents run as the runner's
  user and can read whatever it can, including a mounted token file, so treat
  everything on a runner as trusted with the token. Per-runner credentials
  are phase 2.
- The runner trusts the server completely: the server can type anything into
  any pane it hosts.
- `ws://` is cleartext: the token and every frame (agent input, pane output)
  cross the network unencrypted. Either serve `wss://`
  (`CAO_RUNNER_TLS_CERT`/`_KEY`, runners verifying it with
  `CAO_RUNNER_CA_FILE`) or terminate TLS in front of the hub (ingress or
  service mesh) and give runners a `wss://` URL. Plain `ws://` is only for a
  network you trust, as in the kind example, whose NetworkPolicy limits who
  can connect, not who can read.
- The runner token does not protect the HTTP API. Turn on API authentication
  ([Security](security.md)) when runners or anything else can reach `9889`.

## Known limits

- Providers: only those in `CAO_TERMINAL_REMOTE_PROVIDERS` (default
  `claude_code`, `mock_cli`). A provider that reads or writes the server's own
  filesystem at launch (Kiro, OpenCode, Copilot, Cursor, Grok, Kimi, …) needs
  per-provider work before it can run remotely.
- Not supported, and refused explicitly: `use_worktree`, interactive `cao
  launch` (use `--headless`), attach and browser attach.
- Python workflows and flow pre-scripts validate their directories on the
  server's local filesystem (`services/workflow_service.py`,
  `services/workflow_spec_service.py`), bypassing the backend. Under the remote
  backend they should be refused, and are not yet (phase 1, P1).
- Session-scoped calls (`kill_session`, `session_exists`) are keyed by session
  name only; CAO's generated session names make reuse unlikely, but they carry
  no terminal guard.
- All pane bytes stream back to the one server, which parses them for status
  as it would for local terminals. Tens of terminals are fine; hundreds need
  measuring.
- Every runner id ever seen is a member until it expires, and an unrouted
  terminal or session can be proven absent only once every member has
  reported. Pool runners named after their pods (as in the kind example) get
  new ids on each rollout, so for `CAO_RUNNER_EXPIRY` after one, a create or
  delete that must prove a session absent answers `503`. Give runners stable
  ids (a StatefulSet) or a shorter expiry where that matters.
- One server: its SQLite state, locks and FIFOs live on one volume. The
  example runs it as a single-replica StatefulSet.

## Trade-offs against #834

#834 (`3.0-beta1`) ran a thick bridge in each runtime: its own database,
status monitor and profile resolution, reached by callers through
`POST /runtimes/{id}/terminals`. This backend keeps one source of truth on the
server and makes the runner execution-only, so placement is the server's
decision and the orchestration layer (assign, handoff, inbox, run-step) needs
no second API. From #834 it adopts: placement persisted on the terminal row,
the `503`/`504`/`502` split, the token file, a readiness file, `wss://`,
per-call terminal guards, and `unknown` for an unreachable runtime. It does
not adopt: a channel on the API port (see above), displacing a live
connection by a newcomer with its id, the runtime-side database and status
monitor, and `CAO_LOCAL_EXECUTION`.
