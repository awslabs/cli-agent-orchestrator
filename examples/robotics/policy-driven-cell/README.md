# A work cell where the robots' own controllers move them

In this example, a team of CAO agents runs a simulated robot work cell with two
robots: a Unitree G1 humanoid and an SO-101 arm. The agents plan the job and
delegate it, as in [`cross-zone-transport`](../cross-zone-transport/README.md).
The difference is how the robots move:

- The **G1 walks with a learned locomotion policy**,
  [Holosoma](https://github.com/amazon-far/holosoma) from Amazon FAR. The policy
  reads the robot's joints and IMU and commands all 29 joints at 50 Hz. The
  example only says where to go, as a velocity in the robot's frame.
- The **SO-101 moves with inverse-kinematics motion primitives**: reach, grip,
  lift, carry, lower, release.

Both run through [Strands Robots](https://github.com/strands-labs/robots), the
open-source AWS library that runs the same robot code in MuJoCo or on hardware.
Nothing in this example writes a joint angle or teleports a body.

## The job and the interlock

The cell has three floor locations (`bay`, `inspection`, `dock`), two parts on
the arm's table, and two bins (`pass_bin`, `rework_bin`).

The cell enforces one safety rule: **the arm does not move while a humanoid is
inside its 0.8 m exclusion radius**, judged from the humanoid's measured pelvis
position, not from anything an agent says. The G1 starts at `bay`, next to the
arm. So a supervisor that asks the arm to sort first gets a refusal, and has to
plan: move the humanoid out, then sort, then finish where the request says.

## The agents

| Agent | Can | Cannot |
| --- | --- | --- |
| `cell_supervisor` | Read the cell; delegate with CAO `handoff` | Move a robot |
| `humanoid_operator` | Read the cell; `walk_to` with **its own** robot | Command the arm, or another humanoid |
| `arm_operator` | Read the cell; `pick_and_place` with **its own** arm | Command a humanoid |
| `cell_checker` | Read the cell | Anything else |

Each agent has its own credential. The cell's MCP server checks the scopes and
the robot claim of the credential, so a tool argument or a prompt cannot widen
them. The human operator holds the only credential for `stop_cell`.

`walk_to` and `pick_and_place` return `accepted` at once. The agent then calls
`observe` until the command is `succeeded` or `failed`, the same contract as
`start_task` / `get_task_status` in Strands Robots. A command succeeds only when
the measured state says so:

- a walk succeeds when the measured pelvis is within 0.25 m of the goal, and
  fails if the pelvis drops below 0.55 m (the robot fell) or the time budget runs
  out;
- a pick succeeds when the measured part rests in the requested bin.

## Be honest about the simulator

- **Grasp assist.** In MuJoCo, the SO-101's friction grasp does not lift a part
  (see the Strands Robots example `18_so101_pick_and_lift.py`). The arm holds the
  part with a weld constraint while it carries it. Every pick reports
  `grasp_assist: true`, and the supervisor's report says so.
- **One world per robot.** Each robot has its own MuJoCo world, placed in the
  shared cell frame. The humanoid's world advances only while its policy runs: a
  G1 with no balance controller running falls over, and a real G1 holds its
  standing controller between tasks. The interlock compares measured positions
  in the cell frame, so the arm's world does not physically contain the G1.

## Run it

Python 3.12 or later (Strands Robots needs it), CAO, and a supported provider
CLI. No GPU, no display, no robot hardware.

```bash
cd examples/robotics/policy-driven-cell
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e . pytest
```

**1. Rehearse without CAO.** This runs the job with the same calls the agents
make, on the real simulator. The first run downloads the robot models and the
Holosoma weights (Apache-2.0, pinned, checked by sha256).

```bash
python demo.py rehearse
```

You see the interlock refuse the early pick, then four succeeded commands and
the measured final state, in about 20 seconds.

**2. Run it with CAO agents.**

```bash
python demo.py prepare --run-dir runs/demo --provider claude_code \
  --request "Sort the parts: part_a goes to pass_bin and part_b goes to rework_bin. When the arm is done, the humanoid g1 must finish at the dock. Report what you did with measured evidence."
```

`prepare` prints one `cao install` command per agent and the `cao launch`
command. In a second terminal, start the cell's controller and leave it running:

```bash
python demo.py serve --run-dir runs/demo --allow-motion
```

Start `cao-server` if it is not running, run the printed `cao install` commands,
then the printed `cao launch` command. The controller logs each tool call, for
example `arm called pick_and_place(...)`. Read the supervisor's report with
`cao session status <session-name>`. When you are done:

```bash
python demo.py stop --run-dir runs/demo     # operator stop: refuses all further commands
cao shutdown --session <session-name>
```

Then stop `serve` with Ctrl+C. It writes `runs/demo/last-state.json`.

## A recorded run

A live run with Claude Code agents on 21 October 2026 took about two minutes:

1. The supervisor observed that the G1 was inside the exclusion radius, and sent
   it straight to `dock`, which is outside the radius. The walk succeeded, 0.17 m
   from the dock, upright at 0.77 m.
2. The arm operator moved `part_a` to `pass_bin`, then `part_b` to
   `rework_bin`. Both succeeded on the measured position.
3. The checker passed every check. The supervisor's report flagged that both
   picks used the grasp assist, and that the request's order was ambiguous.

## Tests

```bash
pytest -m "not sim"   # cell logic and MCP scopes, with a fake backend: no MuJoCo, seconds
pytest -m sim         # the job on the real simulator, about 15 seconds
```

## Next steps

See [PLAN.md](PLAN.md): the driver path (a robot's vendor driver on a MuJoCo
twin, then on hardware), recorded video, and failure-injection scenarios.
