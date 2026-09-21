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

One **persistent outbound WebSocket per runtime**, dialed from the runtime to the
cao-server Service. No inbound connection to workers, no per-worker Service, no message
broker.

The contract admits any runtime, and the supervisor is one of them: `CAO_NODE_MODE=bridge`
execs `cao-bridge` instead of `cao-server`, and the shipped supervisor manifest publishes
no `ports:` and no Service, so nothing reaches the pod inbound. That is what closes the
acceptance criterion "the supervisor can delegate without its own CAO server or inbound
API Service" — the delegation tool runs in the server pod over the shared MCP endpoint
(where the broker credentials are), and the worker's answer arrives as an inbox message
the server types into the supervisor's pane over this channel. Confirmed on the cluster,
including the part that only a cluster finds: an earlier attempt queued the answer
correctly and typed it into the *server's* own tmux, where the supervisor's session does
not exist.

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

  Single ownership is enforced in code, not left to the deployment shape. The server
  takes an exclusive `flock` on its state directory at startup
  (`services/server_owner.py`) and refuses to serve while another process holds it, so
  an operator who scales the StatefulSet past one replica gets a refusal naming the
  holder rather than a second writer. `replicas: 1` alone would not have been enough: a
  rolling update overlaps pods by design, which is why the deployed StatefulSet is
  `updateStrategy: OnDelete` and replacement is a documented non-overlapping procedure
  (see the EKS example's README).

  The lock needs no stale-entry expiry — the kernel releases an `flock` when the holder
  dies, including on SIGKILL — and it is re-entrant within one process, because the fd
  is shared and reference-counted; an in-process server started twice (TestClient, an
  embedded server) would otherwise deadlock against itself. `CAO_SERVER_OWNER_LOCK=0`
  opts out for local development against one state directory, and is documented as a
  footgun rather than a supported topology.

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
  today); stdio forwarding shim with **no shared child process** (per #745's review
  findings).

  As built, the shim is one child process per agent — the provider's own stdio
  subprocess, unchanged — and it does carry the caller identity in a header. That is
  header-*carried*, not header-*trusted*: the header is only read from a request that
  already presented the shared runtime token, and the shim takes the value from the
  `CAO_TERMINAL_ID` the provider injected into its child env rather than from anything
  the agent can choose. A request without the token is refused over the wire, so a
  spoofed header needs the token first — which is the same boundary every other
  runtime-authenticated call rests on, and which #774 narrows to per-runtime
  credentials.
- Broker/EKS example: execution-only worker Deployments, no per-worker Service, central
  StatefulSet preserved; client/operation preservation matrix.

### 5.1 Ownership of deferred work (criterion 14)

Centralizing the server made a latent problem explicit. Locally, "whose work is
this" was answerable by inspection: one user, one machine, one process. In this
topology the work that matters most is *deferred* — a schedule fires from a
daemon, a queued message is typed in by a status event, a delegated result comes
back from another pod — and at each of those moments the request that started it
has returned. The only identity still in scope is the server's own, which is
precisely the anonymisation the criterion forbids.

So the owner is recorded at the last moment it is known and read back at the
moment work would begin:

- **Form.** A `Principal` is `issuer#subject`, taken from the verified token's
  `iss`/`sub`. A token with no `sub` is refused rather than owned by a default:
  a request that cannot say who it is for must not get one assigned. With auth
  disabled the owner is the *named* local principal (`cao:local#local`), not
  `None` — "the local user" is an answer, absence of one is not, and the two must
  not be spelled alike.
- **Storage.** `flows.owner` and `terminals.owner`, each its own column added by
  an idempotent PRAGMA-gated `ALTER`. Deliberately **not** a key in
  `terminals.metadata`: the `update_metadata` tool lets the running agent replace
  that dict wholesale, so an owner stored there is an owner the agent chooses.
  `NULL` reads as *unknown*, and unknown is not revoked, so upgrading a live
  install strands no schedule.
- **Direction of travel.** Server → database → server. The owner is **not** in
  the `LAUNCH` payload sent to a runtime. The executor pod is the least-trusted
  party here; an identity handed to it is an identity it can re-present, which is
  the same reason `assign_elastic`'s callback target is resolved from the
  recorded caller rather than from what the worker claims. This is the concrete
  form of the rule the rest of this document rests on: agent-supplied ids are not
  authorization.
- **Where the gate goes.** At dispatch, not at enqueue. `execute_flow` checks
  before running the pre-script, because that script is the owner's code too and
  running it is starting work on their behalf; the schedule still advances, so a
  held flow does not re-attempt every minute forever. Inbox delivery resolves the
  **sender's** owner (the receiver's pane is where work lands, not whose work it
  is) and leaves held messages `PENDING` — `FAILED` would assert a delivery
  attempt that never happened, and a reinstated owner's message should still
  arrive. The lookup sits behind an `any_revoked()` fast path so an installation
  with nothing revoked pays nothing per delivery.
- **Asymmetry.** `may_start_work` can refuse; `may_stop_work` never does.
  Revocation that also withdrew the authority to kill, disable or delete would
  leave a removed member's agent running with nobody able to stop it — worse than
  the access it withdrew.
- **Idempotency.** `owner` is hashed into the `create_terminal` request
  fingerprint. Two principals who pick the same key (`retry`, `job-1`) must
  collide loudly rather than be handed each other's live agent.

Confirmed on the cluster (`cao-745b`), not only in tests: both migrations ran
against the live PVC database and left all 18 pre-existing terminal rows at
`owner = None`; a launch and a `POST /flows` each persisted `cao:local#local` in
the new column with the terminal's `metadata` still holding only `runtime_id`;
with the principal revoked the flow was not dispatched and no `cao-flow-%`
terminal appeared while its schedule advanced, the sender's inbox message stayed
`pending`, and `disable` still returned success; and after the revocation was
removed the same message was **delivered**, which is the property `PENDING`
rather than `FAILED` exists to preserve.

What this does **not** supply is the trusted record it reads from. There is no
tenant model, no membership store and no removal workflow here: #774
(per-runtime delegated credentials), #778 and #779 own those. Until they land,
the revocation list is operator-supplied (`CAO_REVOKED_PRINCIPALS`), the issuer
is trusted transitively through the token the server already verifies, and a
`tenant` key in the principal document is tolerated on read while nothing writes
one. The criterion's *behaviour* is therefore testable and tested; its source of
truth is still owed.

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
(`us-east-1`), twice and in two shapes.

The first stack was assembled ad hoc to exercise the channel: one central
`cao-server` (Deployment/`Recreate` + Service, DB on a gp2 RWO PVC) and two
`cao-bridge` workers dialing `ws://cao-server:9889/runtime/channel`.

The second is the **shipped example itself** — `kubectl kustomize
examples/cao-clusters/kubernetes/eks` rendered into a second namespace with
only the substitutions this cluster forces (no gp3, no EFS, Pod Identity
injection broken so a projected web-identity token stands in, Bedrock
inference profiles) — so the topology under test is the one a reader deploys,
not one written for the test: `cao-server` as a StatefulSet with
`updateStrategy: OnDelete`, and `cao-supervisor` as a `cao-bridge` pod with no
`ports:`, a portless headless Service and an `emptyDir` state volume.

| Capability | Where | Status |
| --- | --- | --- |
| `runtime_channel/{protocol,replay_buffer}.py` frame contract + bounded replay | server & bridge | 26 unit tests |
| `runtime_channel/registry.py` op_id correlation, ack, routing, UNKNOWN-on-disconnect | server | 14 tests, incl. 4 with **two runtimes connected at once** (input/cancel reach only the owning runtime; one runtime dying leaves the other's status and channel intact; positions are per terminal) |
| `PROTOCOL_VERSION` mismatch rejected at hello on both sides; reconnect re-delivers unacked results and resumes each stream from the server's position or emits an explicit gap | server & bridge | 8 tests |
| `runtime_channel/api.py` — WS endpoint (token fail-closed), `POST /runtimes/{id}/terminals`, `GET /runtimes` | server | done |
| `runtime_channel/bridge.py` — `cao-bridge` reusing FIFO/StatusMonitor/LogWriter/TerminalBackend | worker | done |
| `is_remote` routing seam on input / key / output / delete / status / browser-WS | `api/main.py`, `terminal_service.get_terminal` | done |
| Queued-cancellation recheck at the drive boundary | `api/main.py` `_run_in_background` | 4 tests |
| `CAO_NODE_MODE=bridge` in the EKS entrypoint | `examples/.../entrypoint.sh` | done |
| Shared MCP HTTP hosting (`CAO_MCP_TRANSPORT=http`) + per-request caller identity replacing process-global `CAO_TERMINAL_ID`; shared-token gate fails closed; stdio unchanged | `mcp_server/{caller_context,http_hosting}.py`, `mcp_server/server.py`, `utils/orchestration.py` | 9 tests (incl. concurrent-task identity isolation) |
| MCP compatibility driven by the official SDK client over a real socket, not asserted against the spec: version negotiation, two concurrent sessions with distinct identities, no identity inherited by a sequential call, a bad or absent token refused over the wire | `test/mcp_server/test_shared_endpoint_roundtrip.py` | done; the prior test called the middleware in-process with a bare `object()` as its context |
| **stdio→HTTP forwarding shim** so a stdio-only provider reaches the shared endpoint: `cao-mcp-stdio-bridge` proxies to the endpoint's own tool surface and turns the `CAO_TERMINAL_ID` a provider already injects into the per-request caller header — no provider changes. Registers no tools, holds no state, reads no database; a missing token is fatal rather than a quiet fallback to running tools in the agent's pod | `mcp_server/stdio_bridge.py`, `utils/mcp_resolution.py` | done; the same round-trip assertions hold through the shim as a child process |
| **Single active server owner, enforced**: exclusive `flock` on the state directory before `init_db`; a second server refuses to serve and names the holder. Deployed as `updateStrategy: OnDelete` with a documented non-overlapping replacement procedure | `services/server_owner.py`, `examples/.../server.yaml` + README | 1 test module; mutation-checked (removing the guard and swallowing a conflict are both caught) |
| Bridge readiness for a pod with no HTTP: a marker written after the hello is accepted and withdrawn on disconnect, on fatal rejection and at startup; the shipped manifest's exec probe is pinned to the configured path by test | `runtime_channel/bridge.py`, `examples/.../supervisor.yaml` | 10 tests |
| **Status survives a server restart**: `HelloFrame.statuses` carries the runtime's verdict per live terminal and the server seeds its cache from the snapshot it already uses to rebuild routing — status is pushed on change, so an idle terminal would otherwise read UNKNOWN indefinitely. A runtime omits what it cannot read rather than claiming UNKNOWN; a hello cannot set status for another runtime's terminal; a disconnected runtime still reports UNKNOWN | `runtime_channel/{protocol,bridge,api}.py` | 8 tests, all three non-behaviours mutation-checked; **found in live EKS validation, not by a test** |
| CLI→HTTP flow registration preserves `engine` + conditional pre-script (was silently dropped → unconditional launch); rejects arbitrary server paths | `api/main.py` `CreateFlowRequest` | 2 tests |
| **Python workflow scripts execute in the runtime** (`CAO_SCRIPT_RUNTIME`), not the server host: `RUN_SCRIPT`/`CANCEL_SCRIPT` commands; server keeps record/journal/generation/cancel; outcome flows through the shared `_finalize`; `CAO_API_BASE_URL` rewritten to the advertised URL for callbacks; disconnect → explicit failure | `runtime_channel/{protocol,bridge}.py`, `services/script_runner.py` | 13 tests + **EKS-validated** |
| **Flow pre-scripts too** — the other user-code path the issue names by file and line. `RUN_SCRIPT` gained `mode: executable` (the file's shebang picks its interpreter; `docs/flows.md`'s example is bash) and `PROTOCOL_VERSION` went to `2` so a v1 bridge is refused at hello rather than running a bash script through `sys.executable`. The remote env is constructed, not the server's own; timeout, non-zero exit, unparseable JSON and a disconnect all raise instead of reading as "skip" | `services/flow_service.py`, `runtime_channel/{protocol,bridge}.py` | 17 tests (11 new + 6 bridge-mode); the mode field and the env construction each fail a test if reverted |
| **A scheduled flow's agent is placed too** — the second half of the same criterion. `CAO_FLOW_RUNTIME` launches the flow's session in a runtime through the one shared launch path the HTTP route uses (extracted, not copied, so the registry row, the runtime binding and the reported status cannot drift between the two callers), and recycling follows it: the previous session is torn down over the channel, a busy remote conductor still blocks, an unconfirmed teardown defers the run, and local leftovers from a placement change are still cleaned. A disconnected runtime fails the run rather than falling back into the server container; a non-default `engine`, which `LAUNCH` cannot carry, is refused rather than silently downgraded | `services/flow_service.py`, `runtime_channel/api.py` | 9 tests |
| **Owner carried through queues, schedules and cross-pod callbacks** (criterion 14, in part): `flows.owner` + `terminals.owner` written server-side at registration/launch; dispatch gated at `execute_flow` (above the pre-script, schedule still advances) and at inbox delivery (sender's owner; held messages stay `PENDING`); revocation withdraws *start* authority only | `security/principal.py`, `security/auth.py`, `services/{flow,inbox,session,terminal}_service.py`, `runtime_channel/api.py`, `clients/database.py` | 68 tests (58 in five new files, 10 appended to `test/security/test_auth.py`); the two trust decisions (owner absent from the `LAUNCH` payload; owner not in agent-writable `metadata`) are pinned by tests that fail if either is undone |

**EKS-verified**: server container has no tmux binary; tmux sessions live only
in worker pods; two workers execute concurrently with correct input/output
routing; status is derived worker-side and surfaced centrally; a server restart
preserves the DB row (PVC) and rebuilds terminal→runtime routing from the hello
snapshot with output retained; a killed worker yields an explicit
`503 runtime … not connected` (never false success); teardown removes the
worker's tmux session and the central row.

**EKS-verified on the shipped example** (second namespace, both stacks running
side by side): the supervisor pod serves nothing — no listener on `9889` from
inside it or from the server, and its Service publishes no port at all; its
readiness comes from the marker `cao-bridge` writes (`pid 1`, i.e. the bridge
*is* the container's process, which is why the manifest ships no liveness
probe); `GET /runtimes` on the server lists the supervisor as a connected
runtime; a second `cao-server` started inside the same pod refused with
`ServerOwnershipError` naming the holder; a terminal launched through
`POST /runtimes/cao-supervisor-0/terminals` ran Claude Code on Bedrock in the
supervisor pod and its answer came back through the server, with the DB row on
the server's PVC pointing at a tmux session that exists only in the supervisor;
and deleting `cao-server-0` withdrew the readiness marker within 3s, restored
it on reconnect, rebuilt routing from the hello snapshot, and the pre-existing
terminal answered a fresh prompt.

**Criterion 6, end to end on the cluster, including the version gate.** Rolling
the server to `PROTOCOL_VERSION 2` while thirteen v1 bridges were connected
refused all thirteen at hello (`protocol version mismatch: server 2, bridge 1`
in each bridge's log, `GET /runtimes` → `[]`); they withdrew their readiness
markers, were replaced, and reconnected. With `CAO_SCRIPT_RUNTIME=cao-scale-3`
and `CAO_FLOW_RUNTIME=cao-scale-7`, a flow whose pre-script is a `#!/bin/bash`
file rendered the prompt `pre-script ran on cao-scale-3 as cao;
api=http://cao-server…:9889; flow=placement-check; runtime token seen by the
script=absent` — the shebang honoured, the script in its runtime, the callback
rewritten to the advertised Service, and the channel token absent from the
constructed env. The agent's tmux session existed only in `cao-scale-7`;
`cao-scale-3` ran no tmux server at all and neither did `cao-server-0`. A second
run recycled the session in its runtime (one row, session recreated); moving
`CAO_FLOW_RUNTIME` to `cao-scale-0` moved the session with it; and pointing it
at a runtime that does not exist failed the run
(`remote launch on runtime 'cao-scale-99' failed: … not connected`) with no
session in the server container and no orphan row.

That last run is also where the status-on-reconnect gap surfaced: routing came
back correctly but `GET /terminals/{id}` read `unknown`, because status is
pushed on change and the restarted server had never seen a frame for a terminal
that had gone quiet. `HelloFrame.statuses` closes it — the runtime states its
verdict in the same snapshot that rebuilds routing. Re-run on the fixed image
with the agent quiescent and **no interaction at all**: `unknown` while the
channel was down (correct — the server cannot know), then `completed` the
moment the bridge reconnected.

The broker's bridge cycle was exercised in the same namespace: a lease carrying
`mode: bridge` and a `runtime_id`, **no per-worker Service created**, the
runtime connected to the central server in ~20s, a central launch onto the
leased worker answering with its own response line, then
`DELETE /workers/{id}` → `200 {"released":true,"workload_present":false}` with
the runtime dropped from `GET /runtimes`. Run first against a pre-fix broker
image, the same teardown reproduced the flat-15s release bug as an HTTP 500
while the pod was already `Terminating` — so the derived
`WORKER_TERMINATION_GRACE_SECONDS + 15` wait is checked against the failure it
exists for, not only asserted.

The server-count criterion was then measured rather than reasoned about: ten
extra `cao-bridge` pods were added beside the three supervisors, all 13 runtimes
connected to the same `cao-server-0`, and a scan of `/proc/*/cmdline` in every
pod of the namespace found exactly **one** `cao-server` process cluster-wide
(pid 1 in the server pod). Routing stayed per runtime at that width: terminals
launched on two of the ten bound only to their own runtimes, and each input
probe appeared in exactly one pod's pane. Note that `ps | grep cao-server` is
*not* a valid count — a supervisor's tmux command line carries
`CAO_API_HOST=cao-server…` as pane env and matches.

**Also delivered on this branch (the issue's steps 4–5):**

- **Broker → execution-only worker Deployments** (`CAO_ELASTIC_WORKER_MODE=bridge`):
  the broker mints bridge workers (no per-worker Service or cao-server), the
  lease carries `mode`/`runtime_id`/`provider`, and `assign_elastic` routes a
  bridge lease through the central `POST /runtimes/{id}/terminals`
  (`orchestration._assign_bridge`). Readiness = "runtime connected", observed
  via `GET /runtimes` by the reaper's never-usable verdict, the caller's
  connected-wait, and `GATE_ON_READY`. The `cao worker` operator proxy keeps
  its allowlist but answers from the central server, scoped per runtime.
  Worker pods point `CAO_API_HOST`/`CAO_API_PORT`/`CAO_MEMORY_API_URL` at the
  central server (providers forward them into MCP subprocess env), and
  `store_lesson` now writes through the memory gateway so no pod-local store
  diverges silently. Server mode is byte-for-byte unchanged.
  Coverage: 6 unit tests + a bridge section in the standalone broker suite
  (all sections green in both modes) + **EKS-validated** end-to-end with
  `mock_cli` (lease → connect → central launch → task delivered → scoped
  proxy → complete → teardown; foreign-terminal proxying refused 404).
- **CLI client matrix against a shared server** (`CAO_API_BASE_URL` opt-in):
  `cao schedule` (add/list/remove/enable/disable/run) and `cao memory`
  (list/show/delete/clear/export) go over HTTP; engine/pre-script survive the
  CLI→HTTP flow add; `cao launch` works remotely both headless and interactive
  (with `--runtime <id>` placing the terminal on a named execution runtime —
  EKS-validated laptop→server→worker-pod round trip) and never sends the client
  cwd implicitly; operations that genuinely need the server's own filesystem
  (`terminal restore`,
  `memory repair/lint/heal/compact/promote/import/relationships`) raise explicit
  errors instead of silently touching client-local state. 18 tests. Local mode
  (env unset) is byte-for-byte unchanged (598 CLI tests green).

- **Interactive attach relay, browser and native CLI** (§4.4's last bullet):
  `ATTACH_OPEN`/`ATTACH_DATA`/`RESIZE`/`ATTACH_CLOSE` commands plus a dedicated
  `attach` stream. The PTY subprocess is spawned in the runtime, beside the tmux
  socket, from the same `backend.prepare_web_attach()` the local path uses;
  attach-stream frames are routed to a per-terminal sink in the registry rather
  than republished on the bus (interactive bytes are not history), and an empty
  frame is the runtime PTY's EOF. `/terminals/{id}/ws` keeps its client-facing
  protocol byte-for-byte — binary down, `{"type": "input"|"resize"}` JSON up —
  so the browser terminal is unchanged and cannot tell local from remote.
  Native `cao launch` against a shared server attaches over that same endpoint
  (`utils/remote_attach.py`: raw-mode TTY, SIGWINCH→resize, termios restored in
  `finally`) instead of requiring a client-local tmux. A runtime that is not
  connected still closes `4010`, so an unreachable terminal fails loudly.
  Coverage: 4 attach tests + 2 CLI relay tests.
- **Provider-agnostic transport** is enforced, not asserted: a static check
  that no `runtime_channel` module imports the providers package or names a
  provider id, plus one recorded `LAUNCH` contract fixture per shipped provider
  proving the identifier is forwarded opaquely into the shared
  `terminal_service.create_terminal` seam. Paid providers are covered without
  live credentials. Coverage: 14 tests.

  The fixture patches `create_terminal`, so it proves the identifier travels
  opaquely — not that an agent ran. `mock_cli` and `claude_code` were additionally
  driven live **by hand** (the EKS runs below); there is no automated non-mock
  gate in the suite, and the `live_provider` marker registered in `pyproject.toml`
  is currently unused.

- **One routing chokepoint for input, so delegation survives the boundary.**
  `POST /terminals/{id}/input` was remote-aware from slice 1, but the server's
  *synchronous* senders were not: inbox delivery of a delegated result, agent
  steps, memory recall and handoff approval all called
  `terminal_service.send_input` directly, which paste-buffers into the local tmux
  socket. On EKS that dropped a completed worker's answer with
  `Session 'cao-2b412d60' not found` while the supervisor's session was alive in
  its own pod, and marked the message `FAILED`. The remote branch now sits in
  `send_input` itself, above the provider lookup, the memory injection, the
  status gate and the paste — everything below it is about a pane on this host,
  and for a remote terminal the paste is not merely useless but wrong, since a
  local session sharing the name would receive another agent's message. In a
  runtime process the registry is empty, so the bridge's own INPUT handler takes
  the local path and cannot recurse. The inbox additionally reads readiness from
  the runtime rather than the local detector (which would probe a tmux socket the
  server does not own) and treats a disconnected runtime as transient: back to
  `PENDING` for the reconcile sweep, not `FAILED`. Because delivery runs on a
  worker thread (`asyncio.to_thread`), the registry captures the channel's loop
  at `register()` and hands off with `run_coroutine_threadsafe`; a call made from
  the loop thread is refused rather than deadlocking on `Future.result()`.
  The per-operation timeouts moved from `runtime_channel/api.py` to the registry
  so the service layer can read `INPUT_TIMEOUT` without importing the FastAPI
  endpoint module. Coverage: 19 tests.

- **Graceful shutdown and resource limits, the last half of the placement
  criterion.** Both were already true of the manifests (`requests`/`limits` on
  every workload including the broker-minted worker, `terminationGracePeriodSeconds:
  30` on the server and supervisor, and the broker's deletion wait derived from
  that same constant rather than a flat 15s) but neither was written down, and one
  of the two was not actually orderly: `Bridge.stop()` set its event while `_serve`
  stayed parked on an idle socket, so a terminating executor kept announcing
  readiness until the kubelet's SIGKILL and the server learned of it from a
  connection that died with the process. `stop()` now closes the live channel, which
  runs the reconnect loop's `finally` — marker withdrawn, clean close, runtime out
  of `GET /runtimes` while the pod is still shutting down. The server side was
  already orderly (`release_server_ownership()` in the shutdown path frees the state
  directory before exit, so an incoming server does not wait on the outgoing one
  being reaped). Coverage: 2 tests, mutation-checked — dropping the close turns the
  test into the 10s timeout it is meant to catch. Documented in the EKS README
  (*Resource limits and graceful shutdown*), and **measured on the cluster** with
  the grace period set to the shipped 30s: deleting an executor on the pre-fix
  image left it in `GET /runtimes` for 33s (the full grace period, then SIGKILL);
  on the fixed image the same delete removed it in ~2s, with the bridge process
  logging its exit 1.5s after the signal.

- **A replaced executor must not wedge the schedule that used it.** Found by
  re-running the placement flow after rolling the executor StatefulSet: the
  previous run's central row survived on the server's volume, its runtime came
  back with an empty state directory, and the recycle step read the runtime's
  `deleted: false` as a cleanup that might have left an agent running. Correct
  posture, wrong input — the flow deferred, and would have deferred on every later
  run forever. `TEARDOWN` now separates the two meanings of "not deleted": a
  runtime with no row for the terminal reports `absent`, which is the goal state
  already holding, while a row that is still there stays a failure. The server
  settles the central row and routing on `absent` (an id only its own runtime
  could confirm is otherwise untearable for good) and still raises on a timeout,
  where the outcome is genuinely unknown. Coverage: 6 tests, both halves
  mutation-checked; confirmed on the cluster by the run that had been wedged.

- **The server stops deriving a second status opinion for panes it cannot see.**
  Every remote launch logged `Error in StatusMonitor: Terminal … not found in
  database` tracebacks, visible in the cluster log for each of the runs above.
  The cause is structural rather than cosmetic: the server republishes an
  executor's output onto its own bus, so its `StatusMonitor` was regex-scanning
  bytes from other pods to reach a verdict the registry already holds from the
  runtime that owns the pane — and for the first chunks of a launch the central
  row does not exist yet, which surfaced as a logged exception per chunk. The run
  loop now skips terminals the registry calls remote (in a bridge process the
  registry is empty, so a runtime keeps deriving status for its own panes exactly
  as before), and a chunk for an id with no row is ignored rather than raised:
  bytes legitimately arrive on both sides of a row's life. Coverage: 4 tests, both
  guards mutation-checked, and **measured on the cluster** as an A/B on one server
  pod against the same executor: one remote launch plus one turn produced 6 such
  tracebacks on the pre-fix image and 0 on the fixed one (no `ERROR` line at all),
  with the terminal still reaching `completed` — the runtime's own verdict, which
  is the only one that was ever informed.

- **An incompatible runtime backs off instead of hot-looping.** The compatibility
  gate itself held up in the field: two executors left on an older image while the
  server moved to `PROTOCOL_VERSION` 2 stayed `0/1`, never appeared in
  `GET /runtimes`, and were never dispatched work. What the same observation
  exposed is that the reconnect backoff was reset as soon as
  `websockets.connect` returned — and every failure that is *permanent* happens
  after that point, so a mismatched runtime retried at a flat 1s indefinitely
  (6346 log lines from one pod). The reset now requires a completed hello, the
  same condition the readiness marker already used, for the same reason: a socket
  that opened is not a runtime that works. A mismatch settles at the 30s ceiling;
  a runtime that lost a channel it had been serving still returns in about a
  second. Coverage: 2 tests, each mutation-checked in both directions — resetting
  at connect fails the growth test, never resetting fails the recovery test — and
  **measured on the cluster** by dialing the live server from an executor pod with
  a deliberately bumped version: 60 attempts in 60s (every gap 1.01s) before the
  fix, 7 attempts in 90s with gaps `1, 2, 4, 8, 16, 30` after.

- **Session enumeration stops answering "none" for agents that exist.** Found by
  driving the status/interaction surface from a laptop against the cluster:
  `GET /sessions` and `cao session list` reported no active sessions while five
  agents were running on it. `session_service.list_sessions` was built entirely
  from `backend.list_sessions()` — the *server's own* tmux, which on a shared
  `cao-server` does not exist at all (`error connecting to /tmp/tmux-1000/default`).
  Everything else session-scoped reads the database and was already correct
  remotely (`session status`, `session send`, `GET /sessions/{name}/terminals`
  were each exercised from the laptop); only the enumeration was blind, and it
  failed in the shape this design specifically rules out — a confident wrong
  answer instead of an explicit one. The listing now unions the backend's
  sessions with those the registry's terminal→runtime bindings place in a
  runtime, tagged with the runtime ids executing them. Liveness comes from the
  bindings rather than the rows, because a row outlives its runtime and a binding
  does not; a session the backend already reports is not listed twice, so a
  hybrid host is unaffected; and the pane-cwd fallback is skipped for a remote
  terminal, which on a hybrid host would otherwise report a local directory as a
  remote agent's cwd. `status` stays `detached` rather than a new `remote` value
  so callers parsing rows into `models.session.Session` keep validating.
  Coverage: 16 tests, 7 mutations checked (including both directions on the cwd
  guard).

**Deferred to its own workstream:**

- **Per-runtime delegated credentials** — explicitly #774's scope. Both the
  runtime channel and the shared MCP endpoint authenticate with a shared
  `CAO_RUNTIME_TOKEN` (fail-closed) until #774 supplies per-caller credentials
  whose verified subject replaces the token + caller header.

**Compatibility gate**: the runtime channel rejects a `PROTOCOL_VERSION`
mismatch at hello, before any command is accepted — the "unsupported
combinations must fail before accepting new work" requirement for the channel.
