# Transport across ownership zones

Existing CAO CLI agents coordinate a tote transport across independently owned
zones in one persistent MuJoCo world. A supervisor interprets the request,
zone-operation agents select capable robots and execute bounded legs, and an
independent read-only agent checks the result.

This is the first example for [#845](https://github.com/awslabs/cli-agent-orchestrator/issues/845),
inspired by Strands Robots'
[Transport across ownership zones](https://github.com/strands-labs/robots/blob/5180ecc43eb478d84aabf9451bec742b4c2febc6/examples/fleet/02_cross_zone_transport.py).
It retains explicit zone ownership, a shared dock, a common transport operation,
and receiving-side custody acceptance. It does **not** import Strands, LangGraph,
Zenoh, a mock robot policy, or another agent loop. The implementation and scene
geometry here are original; no upstream robot assets or code are bundled.

**Simulation only, with deliberate idealizations.** The robots are kinematic
cart proxies. MuJoCo mocap positions are advanced along straight segments;
the tote is carried by directly updating its position with the cart. There
is no wheel dynamics, grasp, collision avoidance, or real-world safety claim.
Unlike advancing custody after a mock-policy reply, arrival is checked against
MuJoCo's measured body positions. The example has no hardware driver, robot
network discovery, or configurable hardware endpoint. No display, GPU, or
external robot-model download is needed.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| CAO supervisor CLI agent | Interpret the goal, plan legs, delegate through existing `handoff`, reconcile results |
| CLI agent for each participating zone | Assess its capabilities, select a robot, execute and measure its leg, offer or accept custody |
| CAO checker CLI agent | Independently compare fresh observations with the original goal; cannot move, transfer custody, stop, or delegate |
| `simulation.py` | Shared MuJoCo state, exact ownership/capability checks, bounded motion, command history, custody transitions |
| `transport_mcp.py` | Existing FastMCP authentication and scoped tool access; no language interpretation or task planning |
| `demo.py` | Operator setup, persistent server, stdio MCP connection, and independent stop |

There is one shared zone-operation prompt and one implementation of each
operation, not separate logic for the demo's west and east zones. Generated
profiles follow the scene's zones; the supervisor chooses which workers a
request actually needs. The scene contains data, not natural-language routing
rules.

## Setup and run

Prerequisites: [CAO installed from this checkout](../../../README.md),
`uv`, Python 3.10+, tmux, and an authenticated Copilot CLI or Claude Code.
Use the included workflow-permission fix: older CAO installations allowed
restricted installed profiles to delegate through workflow tools.
Robotics dependencies have their **own** project and lockfile; CAO's normal
installation is unchanged.

From the repository root:

```bash
cd examples/robotics/cross-zone-transport
uv sync --locked
export RUN_DIR=/tmp/cao-transport-demo
REQUEST="Move tote from stock to etch. Coordinate the ownership handoff and have an independent checker verify delivery."
uv run --locked python demo.py prepare \
  --run-dir "$RUN_DIR" --request "$REQUEST"
```

`prepare` requires a nonblank `--request` and refuses an existing directory.
It creates private, per-run credentials, unique profile IDs, and a copy of the
scene, then prints installation commands and a complete launch command carrying
that request unchanged as one shell-quoted message.
Pass `--provider claude_code` if using Claude Code instead. Keep this
environment installed at the same path until cleanup: the generated profiles
invoke its exact Python interpreter and the stdio connection script.

In a separate terminal, from the same example directory, start the controller:

```bash
uv run --locked python demo.py serve \
  --run-dir /tmp/cao-transport-demo --allow-motion
```

The explicit `--allow-motion` is operator approval for bounded operations in
this configured simulation. Without it, movement and custody changes are
refused. It is not a bypass of Strands Robots' approval mechanism: that
framework and its robot mesh are not used. The controller binds only
`127.0.0.1`; its MCP connections ignore environment proxies and HTTP redirects.

Start `cao-server` in another terminal if it is not already running. In the
original example terminal, install this run's profiles and launch:

```bash
for profile in "$RUN_DIR"/profiles/*.md; do
  cao install "$profile"
done

SUPERVISOR=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["profiles"]["supervisor"])' \
  "$RUN_DIR/run.json")

cao launch --agents "$SUPERVISOR" --headless --async --auto-approve \
  --session-name transport-demo --working-directory "$(pwd)" \
  -- "$REQUEST"
```

`--async` returns after message delivery, not task completion. Follow the existing
session and measured state below; several sequential handoffs can exceed the
synchronous launch client's wait budget. A client timeout does not cancel the
run and is not a reason to relaunch or replay transport commands.

Use **`--auto-approve`, not `--yolo`**: the former preserves the profile's tool
restrictions. Do not override the generated allowlists, add unrelated MCP
servers, or substitute a prompt-only-enforcement provider. The zone workers
and checker have no CAO delegation grant and no native shell or filesystem
grant. Only the supervisor can delegate. The supervisor and checker receive
observation-only simulator credentials.

The private run directory and local user account are trusted. This is not an
OS sandbox or production multi-tenant authorization system: a process with
unrestricted access to the operator's account could read the credentials.
FastMCP's static-token verifier is used only for this short-lived local demo.
Tokens are neither printed nor embedded in profile text or process arguments;
each stdio connection reads only its own credential file.

## Follow the agents and check the result

```bash
cao session status cao-transport-demo --workers
uv run --locked python demo.py status --run-dir "$RUN_DIR"
```

Use CAO's existing terminal UI or the [tmux guide](../../../docs/tmux.md)
to watch the supervisor and workers. The controller terminal records actor,
command ID, operation, status, and refusal reason. Finished handoff workers
may be removed by CAO; their controller state and command records persist.

For the supplied `site.json`, the expected successful sequence is:

1. The west worker selects `cart-west`, carries `tote` from `stock` to `dock`,
   and checks the finished command and measured dock position.
2. It offers custody to east. **West remains the owner** until acceptance.
3. The east worker independently observes the dock, accepts the exact offer
   with `cart-east`, then carries the tote to `etch`.
4. The checker observes a finished receiving leg, `tote` at `[2, 0]` metres
   within `arrival_tolerance_m`, owner `east`, and no pending offer.

Observations include `run_id`, UTC `observed_at`, simulated time, units,
capabilities, poses, ownership, offers, and command records. Several distinct
CLI agents must visibly contribute; a successful direct tool call or the
deterministic test suite alone does **not** prove a multi-agent run.

## Acceptance cases

Use a fresh prepared run and CAO session for each case. Stop the previous
controller before reusing its port, or prepare the next run with a different
`--port`.

| Case | Request or operator action | Required evidence |
| --- | --- | --- |
| Successful cross-zone request | Ask to move `tote` from `stock` to `etch` | Both zone agents contribute; measured dock arrival precedes custody acceptance; independent final check |
| Unavailable destination | Ask to deliver the tote to `cleanroom` | Explicit unavailability/refusal, no invented location or alternative delivery; payload stays at its last confirmed location |
| Refused capability or owner | Request a leg using another zone's robot, or prepare a scene whose tote exceeds its robot's capacity | Controller returns `rejected` with a reason and does not move the tote, even if a caller asks anyway |
| Interrupted run | Issue the operator stop below while a move is `accepted` or `running` | Active command becomes `interrupted`; actual position and current owner remain recorded; further actions are rejected |
| Different robot setup and request | Prepare with `--scene return-site.json`; ask to return `sample-tray` from `rack` to `inspection` | Different names, coordinates, direction, fixture, capacities and speeds; stores-to-assembly transfer and final pose `[1, -1]` |
| Same-zone request | Ask to take `tote` from `stock` to `dock` only | A checked single-zone leg, with no unnecessary custody transfer |

For a human-paced interruption, copy `site.json`, set `cart-west.speed_m_s` to
`0.1` and `action_timeout_seconds` to `30`, and prepare using that copy. This
makes the first two-metre leg take about twenty seconds; stop after observing
`running`. Stopping an already finished leg must not be reported as an
interrupted transport.

Identifiers use lowercase letters, digits, `_` and `-`, start with a letter,
and are at most 64 characters (compatible with CAO identifiers). Scene values
must be finite; zones are convex axis-aligned rectangles and declared
locations must lie in every owning zone. Distinct location arrival regions
cannot overlap. Robots have exact location/fixture membership, positive
payload limits and speeds. Masses are kilograms; positions and tolerance are
metres; steps and timeouts are seconds. The initial robots must already be
positioned for pickup and receiving. Repositioning empty robots, obstacle
navigation, and physical docking are outside this example.

## Stop, failure, and cleanup

The operator does not need to reach a busy agent or its inbox:

```bash
uv run --locked python demo.py stop --run-dir "$RUN_DIR"
```

Require `stopped: true` in the returned current state. This stops advancement,
marks active commands `interrupted`, preserves the actual intermediate
positions and current custody, and permanently locks out this run. A tote
between named locations has `at: null`, not a guessed completed destination.
The controller remains reachable for observation until you press Ctrl-C in
its terminal.

An accepted move continues to its bounded result even if its worker exits or
the MCP connection closes. Every move has the scene's wall-clock action
timeout. A lost reply is **unknown**, not permission to issue another move:
reconnect and inspect the original `(actor, command_id)`. Repeating that exact
call returns its recorded state without moving again; reusing the ID for a
different operation is rejected. On timeout, interruption or failure, the
agent reports the state rather than automatically retrying.

Ctrl-C also closes the controller and records `last-state.json`. A crash or
forced process termination may prevent that final observation; an old file
does not confirm the current state or stopping. Never restart the same run:
`serve` consumes `started.json` once so old credentials and command IDs cannot
silently address a reset world. Prepare a new run instead.

After a confirmed stop:

```bash
cao shutdown --session cao-transport-demo
# Press Ctrl-C in the controller terminal.
```

Confirm the named CAO session is gone. Retain `last-state.json` if needed, then
remove only this run's generated profile copies at the paths printed by
`cao install` (the shared agent-context copy and, for Copilot, its `.agent.md`
copy). Profile IDs are listed in `run.json`; do not remove other profiles or
shared CAO/Copilot directories. Finally remove the specific
`/tmp/cao-transport-demo` directory you created. The credentials are private
temporary artifacts, not material to commit or attach to a PR.

## Tests and scope

```bash
uv run --locked pytest
```

The dedicated **Robotics transport example** workflow runs the real headless
MuJoCo and authenticated loopback MCP tests on Python 3.10 and 3.12. They check
measured arrival, premature/stale handoffs, ownership, capacity boundaries,
read-only credentials, duplicate/concurrent calls, disconnects, timeouts,
independent stopping, profile schemas, and alternative scenes. These require
no provider credentials. Running the CAO demonstration additionally requires
an authenticated provider and is a separate acceptance step.

Concurrent stop snapshots are atomically replaced through separate temporary
files; a failed write preserves the last complete snapshot and surfaces its error.

The numerical controller is not another coordinator. Goal interpretation,
route decomposition, robot selection, delegation, and semantic evaluation
remain in the existing CLI agents. The only core change closes the existing
workflow-delegation allowlist bypass using CAO's shared permission guard; no
robotics logic enters CAO core. There is no new driver, provider, workflow
engine, or normal-install dependency.
