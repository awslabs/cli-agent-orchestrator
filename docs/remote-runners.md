# Remote runners

[EXPERIMENTAL] With `terminal.backend = "remote"` (#745), one `cao-server` keeps
everything CAO knows and runs no terminal itself. Every terminal it creates is
placed on a `cao-runner`: a small execution-only process that drives tmux on its
own host or pod and streams the pane's bytes back. Nothing above the terminal
backend changes. Any agent can delegate work: assign, handoff, `send_message`,
the inbox, status detection and the HTTP API work as they do locally, because
they still run in the one server and see an ordinary `TerminalBackend`.

The Kubernetes example
[`examples/cao-clusters/kubernetes/remote-runners/`](../examples/cao-clusters/kubernetes/remote-runners/README.md)
runs one server and a pool of identical runners: a cluster-neutral base, and an
overlay for a local kind cluster.

## What runs where

| | `cao-server` (one) | `cao-runner` (any number) |
|---|---|---|
| HTTP API, MCP tool targets | yes | no |
| SQLite registry (`terminals.runner_id` records the placement) | yes | no |
| Provider adapters, profile resolution, status detection, inbox | yes | no |
| Placement: claims, pool, `max_terminals` | yes (the runner hub) | announces its own limits |
| tmux and the provider CLI (the agent) | no | yes |
| Provider files (`{terminal_id}*` in `CAO_HOME_DIR/tmp`) | written here | staged copies, by name |
| The agent's in-session `cao-mcp-server` | no | yes, calling the server's API |

The runner dials out to the server's **runner hub**, a WebSocket listener on its
own port (`9891` by default), authenticates with a shared token and says what it
can host. Every backend call the server makes (create a window, send keys, read
history, arm a pipe, kill) becomes one request on that connection. Pane output
comes back as numbered frames on the same connection and is written into the
server's local FIFO, where the unchanged `FifoManager` / `StatusMonitor` / log
pipeline consumes it.

The hub has its own port, thread and event loop on purpose. Backend calls are
synchronous, and some are made on the API's event loop; if their replies had to
arrive on that same loop, the call would block the loop that has to deliver its
own reply. A separate port also lets network policy admit runners to `9891` and
clients to `9889` independently.

## Configuration

### Server (`cao-server --terminal remote`)

| Env var | Default | Purpose |
|---|---|---|
| `CAO_TERMINAL_BACKEND` | `tmux` | `remote` selects this backend (or `cao-server --terminal remote`). |
| `CAO_TERMINAL_REMOTE_PROVIDERS` | `claude_code,mock_cli` | Providers the remote backend will launch; any other is refused before anything is created. See [Providers](#providers) before adding one. |
| `CAO_RUNNER_TOKEN_FILE` | *(none)* | File holding the runner token, e.g. a mounted Secret. Wins over `CAO_RUNNER_TOKEN`. |
| `CAO_RUNNER_TOKEN` | *(none)* | The token itself, when no file is used. One of the two is required. |
| `CAO_RUNNER_BIND_HOST` | `127.0.0.1` | Address the hub listens on; `0.0.0.0` for runners in other pods. |
| `CAO_RUNNER_PORT` | `9891` | Hub port. |
| `CAO_RUNNER_TLS_CERT` / `CAO_RUNNER_TLS_KEY` | *(none)* | Serve `wss://` with this certificate chain and key. A key without a certificate is a startup error. |
| `CAO_RUNNER_AGENT_API_HOST` / `CAO_RUNNER_AGENT_API_PORT` | *(none)* | Address an agent on a runner uses to reach this server, written into its MCP config. |
| `CAO_RUNNER_CALL_TIMEOUT` | `30` | Seconds to wait for a runner's reply before the outcome is unknown (`504`). |
| `CAO_RUNNER_PLACEMENT_TIMEOUT` | `60` | Seconds a new terminal waits for a runner with room (`503` after). |
| `CAO_RUNNER_EXPIRY` | `600` | Seconds a disconnected runner stays a member (see [Restart and reconnect](#restart-and-reconnect)). |
| `CAO_RUNNER_RESERVED_SESSIONS` | *(none)* | Optional. Session names only a claiming runner may host. |

### Runner (`cao-runner`)

| Env var | Default | Purpose |
|---|---|---|
| `CAO_RUNNER_SERVER_URL` | *(required)* | Hub URL: `wss://host:9891`, or `ws://` (see [Transport](#transport)). |
| `CAO_RUNNER_TOKEN_FILE` / `CAO_RUNNER_TOKEN` | *(one required)* | As on the server; the file wins. |
| `CAO_RUNNER_CA_FILE` | system trust store | CA bundle the hub's certificate is verified against (`wss://` only). |
| `CAO_RUNNER_ALLOW_INSECURE` | `0` | `1` allows `ws://` to a host other than loopback. For a local test cluster only. |
| `CAO_RUNNER_ID` | hostname | Stable id, `[a-z0-9-]`, at most 63 characters. |
| `CAO_RUNNER_MAX_TERMINALS` | `1` | Terminals it hosts at most. |
| `CAO_RUNNER_ACCEPTS_UNCLAIMED` | `1` | Whether it takes terminals of sessions nobody claims. |
| `CAO_RUNNER_CLAIMS` | *(none)* | Optional. Session names only this runner may host. |
| `CAO_RUNNER_READY_FILE` | `CAO_HOME_DIR/runner-ready` | Exists exactly while the runner is connected to the hub; `''` disables it. Point an exec `readinessProbe` at it. |

## Placement

A claimed session (in a runner's `CAO_RUNNER_CLAIMS` or the server's
`CAO_RUNNER_RESERVED_SESSIONS`) goes to its claimant and nowhere else, even while
the claimant is not connected. Any other new terminal goes to a runner already
hosting its session if that runner has room, otherwise to the least loaded
runner that takes unclaimed work and has room. With no runner able to take it
within `CAO_RUNNER_PLACEMENT_TIMEOUT`, the create fails with `503`. Claims are an
optional placement setting; a pool of identical runners needs none.

## Failure outcomes

A backend call on the remote backend has four possible results, and the HTTP
routes that reach the backend keep them apart, including when a service
re-raised the runner's error as its own:

| Status | Class | Meaning | Safe to retry |
|---|---|---|---|
| `503` | `RunnerUnavailable` | The runner is not connected, or no runner had room within `CAO_RUNNER_PLACEMENT_TIMEOUT`. Nothing was sent. | yes |
| `504` | `RunnerOutcomeUnknown` | Sent, but no reply within `CAO_RUNNER_CALL_TIMEOUT`, or the connection dropped first. It may or may not have run. | only if repeating it is harmless |
| `502` | `RunnerReportedError` | The runner ran it and reported a failure; the detail carries the runner's reason. | depends on the reason |
| status `unknown` | | The terminal's status cannot be trusted right now: its runner is out of reach, or its output stream has a gap. `GET /terminals/{id}` reports `unknown` instead of the last status parsed before. It clears once the runner reports the terminal again and a fresh snapshot of the pane is processed. | |

Otherwise each route answers as it does locally: a window that is gone is
still `404`, and a working directory the runner rejects gets the same status
the route gives a rejected directory locally. The server never resends a call on its
own. Where it gave up on a terminal it may have left running (a create whose
reply was lost, a kill the runner did not confirm, a terminal whose runner
expired), the terminal goes into the **reap set**, persisted in
`CAO_HOME_DIR/runner-hub.json`, and is killed the next time its runner reports
it. Teardown of such a terminal reports `unknown`, not `deleted`.

Every window-scoped call carries the terminal id its route was resolved for,
and the runner refuses it as "window not found" (`404`) when that window now
belongs to a different terminal; a kill refused that way kills nothing. A late
or retried call can therefore never land on a newer terminal that reused the
window name.

## Ownership

A terminal's owner is the runner it was created on, recorded on its row
(`terminals.runner_id`) when the create succeeds. Every `hello` and periodic
`inventory` a runner sends lists the terminals it hosts, but a report is
accepted only from the terminal's owner, or from the runner a pending placement
chose. A report from any other runner (with the shared token, any holder can
connect under a new id) moves nothing: the route, the row and the status stay
as they are, and the reported pane is reaped on the runner that reported it.
Ownership is checked before any route is adopted and before any status or
reachability change. Rows with no runner (local terminals) are never claimable,
and no report writes a row.

## Restart and reconnect

1. **Server restart.** Every row with a runner is seeded back as a route before
   the hub accepts a connection, and its runner becomes a member that has not
   reported yet. The server has no output reader or runner pipe for any of them
   any more, so each terminal reads `unknown` until its owner reconnects. Then
   the server recreates the terminal's reader, re-arms the pipe on the runner,
   processes a fresh snapshot of the pane, and the terminal's status and inbox
   delivery resume.
2. **Runner reconnect.** A runner that reconnects (same process, after a
   network blip) keeps its pipes and re-sends the output it buffered meanwhile,
   up to 4 MiB. Beyond that the stream has a gap: the terminal reads `unknown`
   until a resync (reader ensured, pipe re-armed, fresh snapshot) succeeds. A
   gap is never filled with a guess from the output that survived.
3. **Runner restart.** A restarted runner process whose tmux server survived
   re-adopts the windows tagged with its own id (`@cao_runner_id`, next to
   `@cao_terminal_id`); their status reads `unknown` until they are resynced
   the same way. Windows tagged by any other runner, or not tagged (a local CAO
   sharing the tmux server), are never adopted.
4. **Gone from its runner.** A terminal its owner no longer reports (in a
   `hello`, or in two consecutive inventories) loses its route and reads
   `unknown`. The row is left to ordinary teardown.
5. **No row.** A terminal reported by a runner with no committed row and no
   pending placement is unowned: it is reaped after two consecutive reports,
   never within 60 s of a placement, and never while the registry cannot be
   read.
6. **Expiry.** A runner silent for `CAO_RUNNER_EXPIRY` is dropped from
   membership; its terminals are declared lost (reaped if it returns) and stay
   `unknown`.

A teardown and a stream restore never interleave: deleting a terminal waits for
a restore in progress, and no restore starts once a teardown has begun.

A newcomer presenting the id of a runner that is still connected is refused,
not swapped in. A genuine runner whose old connection is half-open is therefore
refused until the hub's ping times that connection out, up to `PING_INTERVAL +
PING_TIMEOUT` (40 s).

## Providers

Under the remote backend a provider's launch must do no execution-host work on
the server's host: spawn no process there (a `which` or `--help` probe, a
version check), read no server identity to decide a flag, and read or write no
provider home or provider execution area there. What the CLI needs from its own
host is asked of the terminal's runner. `claude_code` merges its settings into
the runner's `~/.claude/settings.json` and asks the runner whether panes run as
root.

`test/runner/test_provider_conformance.py` checks this for every provider. The
providers in the remote allowlist (`claude_code`, `mock_cli`) must complete their
launch without touching the server's host. For every other provider it pins what
its launch does there up to the point where the test's sandbox stops it (a
refused write, a wait that never sees the pane ready), and where that is; later
steps of such a launch are not observed. A provider joins the allowlist only
once its launch completes in that test with nothing recorded.

`CAO_TERMINAL_REMOTE_PROVIDERS` (`terminal.remote_providers`) narrows or widens
the list the server will launch. Widening it to a provider outside the default
bypasses that check: the provider launches with the server-host dependencies
listed for it in the test, and reports the server's state as the runner's.

## Security

- **Transport.** `ws://` is cleartext: the runner token and every frame (agent
  input, pane output) cross the network unencrypted. A runner refuses a `ws://`
  URL to anything but a loopback address unless `CAO_RUNNER_ALLOW_INSECURE=1`
  is set; trust is never inferred from a cluster-looking hostname. The hub
  logs a warning when it serves cleartext on an address other than loopback.
  Serve `wss://` (`CAO_RUNNER_TLS_CERT` / `_KEY`, runners verifying it with
  `CAO_RUNNER_CA_FILE`), or terminate TLS in a service mesh in front of the
  hub.
- **Hub port.** Keep `9891` inside the cluster: a ClusterIP Service, no
  Ingress, LoadBalancer or NodePort, and a NetworkPolicy that admits only
  runner pods (which also reach `9889` for their agents' MCP servers).
- **Runner token.** The hub refuses to start without a token, compares it in
  constant time, and rejects a wrong one before the WebSocket is accepted
  (`401`). It is read once, from `CAO_RUNNER_TOKEN_FILE` if set (an unreadable
  file means no token, never a fallback to `CAO_RUNNER_TOKEN`), and both
  variables are then removed from the process environment; prefer the file.
  The runner token variables and `CAO_RUNNER_TLS_KEY` never reach a pane's
  environment. The token authenticates a runner, not an agent, and is not an
  API credential; neither is accepted in place of the other.
- **One trust domain per runner.** Agents run as the runner's user and can read
  whatever it can, including a mounted token file. With the token, an agent
  could register as a new runner and be given future unclaimed work (it cannot
  take over an existing terminal; see [Ownership](#ownership)). Do not mix
  tenants. Per-runner credentials are the follow-up.
- **The runner trusts the server completely:** the server can type anything
  into any pane it hosts.
- **API authentication is not supported yet.** With `terminal.backend=remote`,
  `cao-server` refuses to start when API authentication is configured
  (`CAO_AUTH_LOCAL_TOKEN` or an identity provider): agents on runners would
  need a credential for their orchestration calls, and the only one available
  is the full API token, which every pane on the runner could read. So in this
  phase remote mode allows no authenticated API use at all, external clients
  included. Keep the API reachable only from inside the cluster. Terminal-scoped
  credentials are the follow-up.
- **No user code on the server's host.** Python workflow scripts and flow
  pre-scripts would run as subprocesses on the server's host, beside its
  database. Under the remote backend they are refused (`409`) where they would
  start; storing a definition is still allowed. Installed event plugins
  (`cao.plugins`) and deciders are server extensions and still load.

## Known limits

- **Providers:** only `claude_code` and `mock_cli` (see [Providers](#providers)).
- **Refused operations:** interactive attach from the server host (`cao launch`
  attach, the browser terminal), git worktrees (`use_worktree`), relative or
  server-validated working directories, Python workflow scripts and flow
  pre-scripts.
- **API authentication:** not supported with remote runners in this phase.
- **Restart window:** every runner id ever seen is a member until it expires,
  and an unrouted terminal or session can be proven absent only once every
  member has reported. Runners named after their pods get new ids on each
  rollout, so for `CAO_RUNNER_EXPIRY` after one, a create or delete that must
  prove a session absent answers `503`. Give runners stable ids or a shorter
  expiry where that matters.
- **Session-scoped calls** (`kill_session`, `session_exists`) are keyed by
  session name only; CAO's generated session names make reuse unlikely, but
  they carry no terminal guard.
- **Scale:** all pane bytes stream back to the one server, which parses them
  for status as it would for local terminals. Tens of terminals are fine;
  hundreds need measuring.
- **One server:** its SQLite state, locks and FIFOs live on one volume. Run it
  as a single replica (the example uses a one-replica StatefulSet). Live agent
  processes do not survive the loss of their runner.
