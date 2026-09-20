# Design: Remote execution boundary and runtime channel contract

**Issues:** #745 (centralize cao-server) and #776 (carry terminal output/input/status across the pod boundary)
**Parent:** #777 (CAO 3.0), branch `dev-3.0release`
**Status:** Draft — this document is #745's incremental-delivery step 1 ("define the smallest
remote execution boundary … record what stays in `cao-server` and what must run in an
execution workload"). It is the shared contract both issues implement; once agreed, #776
(the channel/stream layer) and #745 (the bridge, topology, scripts, MCP identity, broker
and client work) proceed as separate workstreams against it.

---

## 1. How the two issues split

#777 calls #745 and #776 "one piece of work, not two" — true for the *release*: #745's
"no tmux on the server" acceptance is only satisfiable once #776's stream exists. But as
**workstreams** they separate cleanly along one seam: the persistent outbound runtime
channel. This document pins that seam so the two sides can be built, reviewed and merged
independently.

| Workstream | Owns | Depends on |
| --- | --- | --- |
| **#776 — runtime channel** | Channel transport, message envelope, output stream sequencing/replay/gap semantics, worker-side status derivation, server-side republish onto the existing bus, browser WS relay, native CLI attach relay | This contract only. Can be developed **additively against today's topology**: a worker that still runs a full cao-server can also dial the central server and stream, so the channel is testable before any server is removed |
| **#745 — bridge & topology** | Execution-only bridge entrypoint (`cao-bridge`), launch/provider-files/cleanup over the channel's control path, command correlation and retained results, Python workflow / flow pre-script relocation, shared MCP hosting and per-request identity, broker/EKS example changes, client/operation preservation matrix | The channel contract (§4) and, for its vertical slice, the #776 implementation |

Integration order: **contract (this doc) → #776 stream layer → #745 removes the per-worker
server.** Each #776 PR is useful on its own (e.g. central live view of a worker terminal);
#745's first vertical slice is the first PR that *requires* the channel to exist.

## 2. Current local pipeline (what must be preserved)

```
tmux session ──pipe-pane──► FIFO (CAO_HOME/fifos) ──FifoManager thread──► event_bus.publish
                                                                              │
                     terminal.{id}.output ◄───────────────────────────────────┘
                     terminal.{id}.status ◄── StatusMonitor (also probes tmux directly:
                                               provider detection, capture-pane, stale-pane)
Browser: /terminals/{id}/ws ──backend.prepare_web_attach()──► local PTY subprocess ◄─► bytes
CLI:     cao launch ──get_backend().attach_session()──► client-local tmux attach
Input:   API/agent_step ──backend.send_keys/send_special_key──► local tmux socket
Scripts: script_runner._drive_process / flow_service pre-script ──► subprocess on server host
MCP:     per-agent stdio process, caller identity = process-global CAO_TERMINAL_ID env
```

Key facts the contract is built on:

- `EventBus` (`services/event_bus.py`) is in-process, lossy (queue-full drops with a
  rate-limited warning, no gap marker to consumers), and single-loop. Its **topic
  contract** (`terminal.{id}.output`, `terminal.{id}.status`) is what server-side
  consumers depend on — not its delivery guarantees.
- `StatusMonitor` is not a pure output consumer: provider status detection and stale-pane
  checks shell out to tmux. Status therefore **cannot** be derived server-side in remote
  mode; the worker derives it and reports it (issue #776's ownership decision).
- `TerminalBackend` (`backends/base.py`) is the execution abstraction: session/window
  lifecycle, `send_keys`/`send_special_key`, `get_history`, `pipe_pane`,
  `attach_session`, `prepare_web_attach`. Everything behind this ABC runs where the tmux
  socket is.
- `run_agent_step` (`services/agent_step.py`) is the shared step sequence (create → ready
  → prompt → completion wait → extract → teardown). It stays on the server; the backend
  operations it invokes are what cross the boundary.

## 3. The execution boundary

The rule from #776, adopted verbatim: **anything that touches the tmux socket (or the
provider process, its PTY, or its config files) runs in the worker runtime.** The central
server relays interactive traffic, maintains routing/state, and republishes events for
its existing consumers.

| Operation | Remote mode: runs where | Mechanism |
| --- | --- | --- |
| Provider construction, prompt/config file materialization | Worker | Bridge executes locally at launch |
| Launch, readiness detection | Worker | `launch` control command |
| Raw output capture (`pipe-pane` → FIFO → `FifoManager`) | Worker | Reused unchanged in the bridge; chunks go up the channel instead of the local bus |
| Status derivation, provider detection, stale-pane probes | Worker | `StatusMonitor` logic runs bridge-side; emits `status` messages upstream |
| Input / key injection (`send_keys`, `send_special_key`) | Worker | `input` control command |
| Interactive attach (browser and native CLI), byte input, resize | Worker owns PTY; server relays | `attach.*` control commands + dedicated attach stream |
| Last-message extraction (`capture-pane`) | Worker | `extract` control command with retained result |
| Graceful exit, cancellation, teardown | Worker | `teardown`/`cancel` control commands; cancellation *requested* vs *stopped* kept distinct |
| Python workflow subprocess, flow pre-scripts (#745) | Worker (execution workload) | Script dispatch over the control path; journal/scheduling stay central |
| Republish onto `terminal.{id}.output` / `.status` | cao-server | Channel consumer publishes to the in-process bus; existing bus-only consumers unchanged |
| Status lookup / completion polling | cao-server | Reads worker-reported current status + reconnect snapshots; **never** calls a provider detector for a remote terminal |
| Scheduling, journal, orchestration state, routing, authz | cao-server | Unchanged location |

Local mode is byte-for-byte today's pipeline: no channel, no new dependency, same files,
same detectors. Remote is the same pipeline with a network hop where the co-located hop
used to be.

## 4. Runtime channel contract (the seam)

One **persistent outbound WebSocket per runtime** (worker or supervisor pod), dialed from
the runtime to the cao-server Service. No inbound connection to workers, no per-worker
Service, no message broker.

```
worker pod                                     cao-server pod
 bridge: tmux/FIFO/StatusMonitor  ──frames──►  RuntimeChannelRegistry ──► event_bus ──► consumers
                                  ◄──frames──  control (launch/input/resize/extract/cancel/teardown)
```

### 4.1 Envelope

Every frame is one JSON object:

```json
{
  "kind": "cmd | cmd_result | event | stream | ack | hello | heartbeat | gap",
  "op_id": "…",            // cmd/cmd_result/ack: server-assigned, unique per command
  "terminal_id": "…",      // absent on runtime-scoped frames (hello, heartbeat)
  "generation": 3,          // stream/gap/status: runtime-assignment generation (fencing)
  "stream": "capture | attach", // stream frames: which stream the position belongs to
  "pos": 18244,             // stream: monotonic byte position BEFORE any lossy queue
  "type": "…",             // cmd: launch|input|special_key|resize|attach_open|attach_data|attach_close|extract|history|cancel|teardown ; event: status|ready|exited
  "payload": { }
}
```

### 4.2 Command path (server → runtime) — #745's correlation/ack requirements

- Every command carries a server-assigned `op_id`. The runtime executes, then sends
  `cmd_result {op_id, outcome, payload}` and **retains the result** until the server acks
  it (`ack {op_id}`) or the retention bound expires.
- A lost response is resolved by **re-requesting the retained result by `op_id`** — never
  by blindly resubmitting the command. Commands are not assumed idempotent.
- `cancel` produces two observable states: `cancel_requested` (accepted) and a later
  terminal outcome (`stopped | failed | unknown`). Lease expiry or a dead channel never
  synthesizes `stopped`.
- Completion results (`extract`, exit status) are retained runtime-side until acked, so
  worker cleanup cannot outrun result delivery (#745 "retained results before cleanup").

### 4.3 Stream path (runtime → server) — #776's ordering/gap requirements

- Two distinct streams per terminal, never sharing positions: `capture` (FIFO/pipe-pane
  background output) and `attach` (interactive PTY bytes).
- Each stream has a `(generation, pos)` pair. `generation` increments when a new
  runtime/assignment takes over a terminal identity; `pos` is a monotonic byte offset
  assigned **at capture time, before any queue can drop**. A new generation cannot be
  mistaken for a continuation of the old one.
- The runtime keeps a **bounded replay buffer** per stream. On reconnect it sends
  `hello {streams: [{terminal_id, stream, generation, end_pos}]}`; the server replies
  with resume positions. Within the window → replay; outside it or across a lost
  generation → the runtime emits an explicit `gap {from_pos, to_pos | unknown}` frame,
  which the server republishes so consumers (and the browser) can render a visible gap
  instead of silence.
- `heartbeat` carries per-stream `end_pos` watermarks, so loss of a *final* chunk is
  detectable even when no later output ever arrives.
- Status events carry `generation`; a status/completion from a prior generation cannot
  settle the current assignment (fencing; distinct from #735's prompt-turn freshness,
  which this work does not claim to fix).

### 4.4 Connection behavior

- Runtime dials out with backoff; heartbeats both ways; half-open detection by heartbeat
  timeout.
- **Authentication from the first slice**: the runtime presents its worker-specific
  credential (today's `X-CAO-Release-Token` binding, extended per #774/#779 later). Auth
  failure is surfaced as an error, never retried as an anonymous connection. Revocation
  closes the channel; a previously accepted handshake is not continuing authorization.
- One active cao-server owner (single replica, non-overlapping rollout per #745). On
  server restart, runtimes reconnect and re-`hello`; the server rebuilds routing from
  persisted terminal→runtime association plus the hello snapshots.

### 4.5 Server-side pieces

- `RuntimeChannelRegistry`: terminal_id → live channel (+ persisted association for
  recovery). A request for one terminal can never be served by another runtime.
- Republisher: channel `stream`/`event` frames → existing bus topics. Bus-contract-only
  consumers keep working; tmux-touching consumers are the ones that moved worker-side.
- Attach relay: `/terminals/{id}/ws` keeps its client-facing protocol (input, resize,
  bytes) but, for a remote terminal, opens `attach_open` over the channel instead of
  spawning a local PTY. The native CLI gets the same relay through a new client-side
  attach that speaks to the server instead of requiring a client-local tmux.

## 5. What #745 builds on top (not in #776's scope)

- **`cao-bridge`** entrypoint: FifoManager + StatusMonitor + TerminalBackend + provider
  bootstrap + the channel client. Explicitly not a renamed cao-server: no HTTP API, no
  DB, no scheduler, no bus consumers.
- Launch path: `run_agent_step` / `terminal_service.create_terminal` gain a routing seam —
  local backend vs. channel commands — selected per terminal, not process-globally.
- Script execution: `script_runner` dispatches the prepared script + inputs to a runtime
  over the control path, preserving `build_env` allowlist, step/callback API, timeouts,
  cancellation (including the queued-launch cancellation recheck #745 calls out), and
  journal ownership central.
- Shared MCP hosting: HTTP transport with per-request authenticated caller context
  replacing process-global `CAO_TERMINAL_ID` (`mcp_server/server.py` reads it from env
  today); stdio forwarding shim with **no shared child process and no header-derived
  identity** (per #745's review findings).
- Broker/EKS example: execution-only worker Deployments, no per-worker Service, central
  StatefulSet preserved; client/operation preservation matrix.

## 6. Non-goals (inherited, restated)

No message broker; no at-least-once guarantee for raw terminal output (bounded replay +
visible gaps); no multi-replica cao-server; no automatic session restoration after pod
loss; no exactly-once external effects; `workflow_run_event` stays audit history, not
transport; sign-in/revocation policy lives in #774/#779.

## 7. Proposed PR slices

#776 branch(es):
1. Protocol models + envelope codec + replay buffer (pure, unit-tested, no wiring).
2. Runtime channel client + server endpoint + registry + republisher (capture stream +
   status up; heartbeat/reconnect/gap).
3. Control path down: input/special-key/resize + op_id/ack/retained results.
4. Browser attach relay + browser reconnect/resume cursor.
5. Native CLI attach relay.

#745 branch(es), consuming the above:
1. `cao-bridge` entrypoint + launch/teardown/extract over the channel; opt-in remote
   terminal end-to-end with one provider.
2. Script/flow pre-script execution in runtimes.
3. Shared MCP HTTP hosting + per-request identity + stdio shim.
4. Broker → execution-only workers; EKS example + docs; preservation matrix.

## 8. Slice-1 status (implemented)

Landed on this branch and validated locally plus on the EKS chaos-cluster
(`us-east-1`): one central `cao-server` (Deployment/`Recreate` + Service, DB on
a gp2 RWO PVC) and two `cao-bridge` workers dialing
`ws://cao-server:9889/runtime/channel`.

| Capability | Where | Status |
| --- | --- | --- |
| `runtime_channel/{protocol,replay_buffer}.py` frame contract + bounded replay | server & bridge | 26 unit tests |
| `runtime_channel/registry.py` op_id correlation, ack, routing, UNKNOWN-on-disconnect | server | done |
| `runtime_channel/api.py` — WS endpoint (token fail-closed), `POST /runtimes/{id}/terminals`, `GET /runtimes` | server | done |
| `runtime_channel/bridge.py` — `cao-bridge` reusing FIFO/StatusMonitor/LogWriter/TerminalBackend | worker | done |
| `is_remote` routing seam on input / key / output / delete / status / browser-WS | `api/main.py`, `terminal_service.get_terminal` | done |
| Queued-cancellation recheck at the drive boundary | `api/main.py` `_run_in_background` | 4 tests |
| `CAO_NODE_MODE=bridge` in the EKS entrypoint | `examples/.../entrypoint.sh` | done |
| Shared MCP HTTP hosting (`CAO_MCP_TRANSPORT=http`) + per-request caller identity replacing process-global `CAO_TERMINAL_ID`; shared-token gate fails closed; stdio unchanged | `mcp_server/{caller_context,http_hosting}.py`, `mcp_server/server.py`, `utils/orchestration.py` | 9 tests + live HTTP round-trip |
| CLI→HTTP flow registration preserves `engine` + conditional pre-script (was silently dropped → unconditional launch); rejects arbitrary server paths | `api/main.py` `CreateFlowRequest` | 2 tests |
| **Python workflow / flow pre-scripts execute in the runtime** (`CAO_SCRIPT_RUNTIME`), not the server host: `RUN_SCRIPT`/`CANCEL_SCRIPT` commands; server keeps record/journal/generation/cancel; outcome flows through the shared `_finalize`; `CAO_API_BASE_URL` rewritten to the advertised URL for callbacks; disconnect → explicit failure | `runtime_channel/{protocol,bridge}.py`, `services/script_runner.py` | 13 tests + **EKS-validated** |

**EKS-verified**: server container has no tmux binary; tmux sessions live only
in worker pods; two workers execute concurrently with correct input/output
routing; status is derived worker-side and surfaced centrally; a server restart
preserves the DB row (PVC) and rebuilds terminal→runtime routing from the hello
snapshot with output retained; a killed worker yields an explicit
`503 runtime … not connected` (never false success); teardown removes the
worker's tmux session and the central row.

**Deferred, each large enough to be its own PR (the issue's own delivery plan
splits these across steps 2–5):**

- **Interactive browser + native-CLI attach relay** — explicitly #776's scope
  ("#776 includes moving that path beside the remote agent"). The WS attach
  currently closes `4010` for a remote terminal.
- **Broker → execution-only worker Deployments** (#745 step 4, coupled with
  #776 step 5). The EKS broker still mints full per-worker `cao-server` +
  Service; converting it to bridge workers reshapes the lease/gateway/reaper
  model and its 1,497-line test suite. Validated the target topology
  standalone (this branch's throwaway manifests) ahead of that refactor.
- **Full CLI client matrix** — `cao launch`/`schedule`/`memory`/`worker`
  operating against a shared server with no local-state fallback. Overlaps
  #776's CLI attach relay.
- **Per-runtime delegated credentials** — explicitly #774's scope. Both the
  runtime channel and the shared MCP endpoint authenticate with a shared
  `CAO_RUNTIME_TOKEN` (fail-closed) until #774 supplies per-caller credentials
  whose verified subject replaces the token + caller header.

**Compatibility gate**: the runtime channel rejects a `PROTOCOL_VERSION`
mismatch at hello, before any command is accepted — the "unsupported
combinations must fail before accepting new work" requirement for the channel.
