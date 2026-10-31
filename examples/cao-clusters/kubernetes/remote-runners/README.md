# One cao-server, execution-only runners (#745, experimental)

One central `cao-server` serves every agent. The supervisor and the workers run
in their own pods beside a `cao-runner`, which runs **no `cao-server`**. Each
runner dials out to the server, runs the server's terminal operations against
its own tmux, and streams the pane output back.

| Component | Kind | Runs | Storage |
|---|---|---|---|
| `cao-server` | StatefulSet, 1 replica | API, database, orchestration, status detection | RWO claim (SQLite, FIFOs) |
| `cao-supervisor-runner` | Deployment, 1 replica | `cao-runner` + tmux + the supervisor CLI | shared workspace |
| `cao-pool-runner` | Deployment, N replicas | `cao-runner` + tmux + one worker CLI | shared workspace |

With 1 or 10 pool runners there is still exactly one `cao-server` process.

## How it works

`cao-server --terminal remote` (or `CAO_TERMINAL_BACKEND=remote`) selects
`RemoteBackend`. Every `TerminalBackend` call is routed to the runner that hosts
the terminal, over a WebSocket the runner opened to the server's runner hub
(port 9891, token mounted from the `cao-runner-token` Secret as a file via
`CAO_RUNNER_TOKEN_FILE`). Nothing dials in to a runner, so runners have no
Service and no ingress. A runner pod is Ready only while its connection to the
hub is up (an exec `readinessProbe` on `CAO_RUNNER_READY_FILE`).
[Remote runners](../../../../docs/remote-runners.md) is the reference for
configuration, failure outcomes and restart semantics.

- **Output and status are unchanged.** When the server arms a pane, the runner
  streams the pane's bytes back, and the server writes them into the same local
  FIFO a local tmux pane would feed. The FIFO reader, status monitor, log
  writer and inbox delivery run exactly as they do locally.
- **Placement.** A runner declares the session names it claims, whether it
  takes unclaimed work, and how many terminals it can host. A claimed session
  goes to its claimant and nowhere else. A new window goes to a runner already
  hosting its session if that runner has room, and otherwise to a pool runner
  with room. The supervisor runner claims `cao-supervisor` and holds one
  terminal, so the agents it `assign`s land on pool runners in other pods.
- **Provider files.** The per-terminal files a provider writes under
  `CAO_HOME_DIR/tmp` (system prompt, MCP config) are staged into the runner
  before any input reaches the pane. No provider is aware it is remote.
- **Agents reach the server.** The agent's in-session `cao-mcp-server` is the
  ordinary stdio server inside the runner pod. It calls the central API at
  `CAO_RUNNER_AGENT_API_HOST:PORT`, which the server writes into the agent's MCP
  config. Callbacks (`send_message`) are inbox messages on that one server.
  The MCP command stays the bare `cao-mcp-server`, resolved on the runner's
  own `PATH`, so a runner image only has to put it there.

## Deploy on kind

```bash
mkdir -p ~/cao-kind/workspace
sed "s|<host-workspace-dir>|$HOME/cao-kind/workspace|" \
  examples/cao-clusters/kubernetes/remote-runners/kind-config.yaml > /tmp/kind.yaml
kind create cluster --config /tmp/kind.yaml
examples/cao-clusters/kubernetes/remote-runners/deploy-kind.sh cao-k8s-cluster
```

`deploy-kind.sh` builds the image from this checkout and loads it into kind. It
mints the runner token once, and on every run it refreshes the
`cao-provider-credentials` Secret from `aws configure export-credentials`. Only
runner pods get those credentials; the server pod runs no agent and holds no
model credentials.

All model tiers are pinned to Claude Haiku 4.5 on Bedrock to keep the example
cheap. Change the `ANTHROPIC_*` variables in `runners.yaml` to use other models.

## Try it

```bash
kubectl -n cao-remote exec cao-server-0 -- curl -s localhost:9889/runners
```

Create the supervisor in its claimed session and give it work to delegate:

```bash
kubectl -n cao-remote exec -i cao-server-0 -- python - <<'PY'
import requests
task = ("Call the assign tool twice (agent_profile='developer'). Each worker runs "
        "`hostname`, writes it to /home/cao/workspace/demo/<alpha|beta>.txt and reports "
        "it back. End your turn after both are accepted; results arrive in your inbox.")
r = requests.post("http://localhost:9889/sessions",
    params={"agent_profile": "code_supervisor", "provider": "claude_code",
            "session_name": "cao-supervisor", "working_directory": "/home/cao/workspace"},
    json={"initial_message": task}, timeout=60)
print(r.status_code, r.json()["id"])
PY
```

To verify, check the files the workers wrote and where each one ran. Don't rely
on the reported status alone:

```bash
kubectl -n cao-remote exec deploy/cao-supervisor-runner -- sh -c 'cat /home/cao/workspace/demo/*.txt'
kubectl -n cao-remote exec cao-server-0 -- curl -s localhost:9889/runners
```

Each file names a different `cao-pool-runner-*` pod.

## Supported scope and limits

This is the first remote-execution slice of #745. Everything outside this list
is refused explicitly rather than misrouted.

- **Providers:** `claude_code` (verified end to end on Bedrock) and `mock_cli`.
  Others are refused at launch because they read or probe the server's
  filesystem (Kiro's capability probe, the agent directories of OpenCode and
  Copilot, provider homes outside `CAO_HOME_DIR/tmp`). Widen the list with
  `terminal.remote_providers` only after tracing that provider's launch.
- **Unsupported operations:**
  - Interactive attach from the server host: `cao launch` attach and the
    browser terminal.
  - Git worktrees (`use_worktree`).
  - Relative working directories. An absolute path is validated by the runner.
- **Runner credential:** runners share one bearer token, read from a mounted
  file and removed (with the file's path) from the runner's environment before
  any pane starts. Panes still run as the same OS user as the runner, so an
  agent can read the mounted token file. With it, the agent could register as a new
  runner and be given future unclaimed work. The hub never lets a newcomer
  displace a runner id that is currently connected, but that does not close
  this gap. Treat a runner pod and every agent in it as one trust domain. Per-runner
  credentials (e.g. a projected ServiceAccount token verified with
  TokenReview, binding the runner id to its pod) or a separate OS user for
  panes are required before mixing tenants. This is planned for the broker
  phase of #745.
- **Identity:** the in-session MCP server still identifies its caller by
  `CAO_TERMINAL_ID`, and any runner pod can reach the full central API. The
  NetworkPolicy keeps everything else out, but within the namespace this is not
  per-caller authorization. A shared, authenticated HTTP MCP endpoint is the
  next phase of #745.
- **Failure semantics** (over HTTP: `503` / `504` / `502`, see the reference):
  - A call to a disconnected runner fails as "runner unavailable" (`503`).
  - A call whose reply was lost fails as "outcome unknown" (`504`) and is never
    resubmitted.
  - A failure the runner reports (e.g. tmux could not be read) is `502`.
  - While a terminal's runner is out of reach, its status reads `unknown`
    instead of the last status parsed before it went away.
  - A reconnecting runner re-announces its terminals, and its buffered output
    (4 MiB) is replayed.
  - Overflow beyond the buffer is logged on the server as a sequence gap.
  - Each terminal's runner is recorded on its registry row
    (`terminals.runner_id`). A restarted server seeds its routes from those
    rows before any runner reconnects, then reconciles them with what each
    runner reports (the runner's report wins; a reported terminal with no row
    is reaped). As with a local `cao-server` restart today, live status
    streaming is not re-armed for terminals created before a SERVER restart,
    so their status reads `unknown`. A restarted RUNNER process whose tmux
    survived re-adopts its own windows and has their output re-armed.
  - Runner membership and the terminals awaiting a reap are persisted in
    `runner-hub.json` on the server's state volume. An unrouted terminal
    counts as absent only once every member has reported to the current
    server process. A member silent for longer than `CAO_RUNNER_EXPIRY`
    (default 600s) expires; its terminals are declared lost and killed if it
    ever returns.
  - A create whose reply was lost, or a kill the runner never confirmed, is
    never reported as done. The terminal is queued and reaped the next time
    its runner reports it.
  - Live agent processes do not survive a runner pod's loss.
- **Transport:** this example uses plain `ws://` for in-cluster traffic. The
  hub can serve `wss://` itself (`CAO_RUNNER_TLS_CERT` / `CAO_RUNNER_TLS_KEY`,
  runners verifying it with `CAO_RUNNER_CA_FILE`), or TLS can be terminated in
  front of it. Use one of them before crossing any untrusted network; the token
  is a bearer credential.
- **Not yet migrated:** Python workflows and flow pre-scripts still execute in
  the server pod, and the EKS example's broker still creates full CAO worker
  nodes. Both come later in #745.
