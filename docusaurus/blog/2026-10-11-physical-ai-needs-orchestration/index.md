---
title: "Why physical AI needs multi-agent orchestration"
authors: [haofeif]
tags: [deep-dive, orchestration-patterns, physical-ai]
description: "CAO's first physical AI example: a supervisor, two zone workers, and an independent checker move a parcel across a simulated factory floor, and custody changes only on measured positions."
---

Physical AI is AI that acts in the physical world, for example when it moves robots. On a real site, the robots seldom belong to one team. A factory, a warehouse, or a laboratory divides its floor into areas, and a different team or vendor operates the robots in each area. A job that crosses two areas needs more than one capable agent. It needs a plan and a separate delegation to each area. It needs a handoff of the item that both areas confirm. And it needs a check that does not trust the reports of the workers.

These are orchestration problems. This post shows how CLI Agent Orchestrator (CAO) handles them in its first physical AI example, [cross-zone transport](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/robotics/cross-zone-transport). Four CAO agents move a parcel across a simulated factory floor in [MuJoCo](https://mujoco.org/). A supervisor plans the job and hands each step to a zone worker. Each zone worker can move only the robots of its own zone. An independent checker verifies the result. The parcel changes owner only when the measured positions show it at the shared dock.

The example controls no real robots. It simulates the coordination layer above the robots, and the last part of this post maps that layer to a real site.

{/* truncate */}

The runs in this post used CAO `2.5.3` from `main` (commit `88f3af0c`) with the `claude_code` provider, on 2026-10-11. CAO 2.5.3 is the first release with the permission fix for workflow delegation that the example uses.

## One floor, many owners

Robots are no longer rare on a factory floor. The International Federation of Robotics (IFR) [reports](https://ifr.org/ifr-press-releases/news/five-million-robots-now-operate-in-factories-globally) that "the global operational stock of industrial robots surged 9% to a record 5 million units in 2025." Mobile robots grow too. IFR [counts](https://ifr.org/ifr-press-releases/news/global-sales-of-professional-service-robots-surge-24-percent) almost 250,000 professional service robots shipped in 2025, and transportation and logistics is the largest application, with 117,500 units. Gartner lists both physical AI and multiagent systems in its [top strategic technology trends for 2026](https://www.gartner.com/en/newsroom/press-releases/2025-10-20-gartner-identifies-the-top-strategic-technology-trends-for-2026).

Large sites seldom buy all their robots from one vendor. The [Open-RMF book](https://osrf.github.io/ros2multirobotbook/intro.html) puts it this way: "multi-vendor, multi-robot systems remain an open problem, and we expect that multi-vendor robot deployments will be the norm in all large buildings in the future."

Standards help the robots talk to their fleet controls. [VDA 5050](https://github.com/VDA5050/VDA5050) defines the interface between a fleet control and its mobile robots, "to enable the coordinated operation of heterogeneous mobile robot fleets from different manufacturers within a shared physical environment." But the standard leaves the decisions to others. It states that it "does not allocate responsibilities among operators, system integrators, vehicle manufacturers, or fleet control providers", and it does not include traffic management logic.

So when a job crosses from one area to another, some questions stay open:

- Who plans the route, and which area does each part?
- Who can move which robot?
- When does the responsibility for the item pass from one area to the next?
- Who checks that the job is done, and on what evidence?

Most organizations are still early with these questions. A [Capgemini Research Institute survey](https://www.capgemini.com/us-en/news/press-releases/two-thirds-of-organizations-rate-physical-ai-as-a-high-priority-for-the-next-three-to-five-years/) of 1,678 executives found that "79% of organizations are already engaging with physical AI", but "only 4% say they are already operating at scale."

## Why one agent is not enough

One agent that holds the credentials of every area could plan the job and move every robot. That design has two problems.

The first problem is the blast radius. OWASP lists [excessive agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/) as a top risk for LLM applications: "the vulnerability that enables damaging actions to be performed in response to unexpected, ambiguous or manipulated outputs from an LLM." With machines, a damaging action can be physical. The OWASP advice applies directly: "Implement authorization in downstream systems rather than relying on an LLM to decide if an action is allowed or not."

The second problem is scale. Research on LLM planners for robot teams finds that one planner struggles as the team grows. [Chen et al.](https://arxiv.org/abs/2309.15943) (ICRA 2024) report that "long-horizon, heterogeneous multi-robot planning introduces new challenges of coordination while also pushing up against the limits of context window length." In their tests, "hybrid approaches with both central and local LLM planners produce the most successful plans and scale best to large number of agents."

More agents do not fix everything, though. The authors of [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) found that "many MAS failures arise from the challenges in organizational design and agent coordination rather than the limitations of individual agents." They also found that verification failures "appear frequently even in successful runs." And a language model alone does not know the physical state. The [SayCan](https://arxiv.org/abs/2204.01691) authors note that language models "lack real-world experience, which makes it difficult to leverage them for decision making within a given embodiment."

These findings point to five design rules for a team of agents that operates robots:

1. Give each agent one clear role.
2. Give each agent only the access that its role needs, and enforce that access outside the model.
3. Let one agent plan, and let local agents act.
4. Use measured state, not the text of a message, as proof.
5. Let an independent agent check the result.

The cross-zone transport example applies all five.

## The example: four agents, two zones, one parcel

The default scene has two zones, `west` and `east`. The parcel starts at `stock` in the west zone and must go to `etch` in the east zone. The two zones meet at a shared `dock`. No robot can do the full trip, because each cart stays in its own zone.

![The simulated floor at the start of a run. The west zone is light blue and the east zone is light green. The pink parcel is on the blue west cart at stock. The green east cart waits at the amber shared dock.](../../../examples/robotics/cross-zone-transport/images/run-1-start.png)

Four CAO agents do the work. Each agent is a provider CLI, for example Claude Code or GitHub Copilot CLI, in its own CAO terminal:

- The **supervisor** reads the request and the scene, plans the legs, and delegates each leg with the CAO [`handoff`](/docs/patterns/handoff) tool. Its simulator credential can only observe.
- The **west zone worker** carries the parcel to the dock with `cart-west`, and offers custody to the east zone.
- The **east zone worker** accepts custody at the dock, and carries the parcel to `etch` with `cart-east`.
- The **checker** reads the final state and compares it with the request. Its credential can only observe.

The MuJoCo world runs in a separate local process, the controller. The agents reach the world only through the MCP tools of the controller. The controller does no planning. The supervisor finds the parcel, selects the route, and selects the worker for each leg.

![The orchestration of the example. A user request goes to the supervisor. The supervisor hands off to the west zone worker, then to the east zone worker, then to the checker, and it calls observe on the MuJoCo controller between the steps. The west zone worker calls observe, move, and offer_handoff. The east zone worker calls observe, accept_handoff, and move. The checker calls observe. The operator calls status and stop.](./orchestration.svg)

## How the example keeps each agent in its role

### A credential for each agent, checked by the simulator

`demo.py prepare` writes one private credential file for each agent. The credential sets the scope and the zone of the agent, and the controller checks it on every call:

| Tool | Scope | Used by |
| --- | --- | --- |
| `observe` | `observe` | All agents |
| `move`, `offer_handoff`, `accept_handoff` | `act`, in the zone of the caller | Zone workers |
| `stop_simulation` | `operate` | The operator, with `demo.py stop` |

The zone of a worker comes from its credential, not from the text of a prompt or from a tool argument. If the west zone worker asks to move `cart-east`, the controller rejects the call with `not_robot_owner`.

### Only the supervisor can delegate

Each agent has a CAO [agent profile](/docs/features/profiles) with a tool allowlist. The supervisor profile grants `@cao-mcp-server`. The worker and checker profiles grant only `@transport-sim`, the simulator server. CAO checks the grant of the caller before it runs a delegation tool, so a worker that tries `assign` gets this result:

```text
'assign' is not permitted: the calling terminal's allowed tools do not include '@cao-mcp-server'
```

With a provider that enforces the allowlist, for example Claude Code, a worker also cannot use a shell or read files. So a zone worker cannot read the credential file of another zone. `prepare` refuses the providers that cannot give each agent its own simulator server. For the details of each provider, see [tool restrictions](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md).

### `handoff`, because each leg needs the leg before it

CAO has two ways to delegate. [`assign`](/docs/patterns/assign) returns immediately, and the worker sends its result later. `handoff` waits until the worker finishes, and returns its last response. In this job, the east zone worker can accept custody only after the west zone worker offers it at the dock. The checker can check only after the last leg. Parallel work gives no benefit here, so the supervisor uses `handoff` for every step.

### Custody changes on measured positions

The custody handoff has two steps. The sending zone calls `offer_handoff`, and the receiving zone calls `accept_handoff`. The controller accepts the offer only when the measured position of the parcel is at the dock. It accepts the acceptance only when the parcel and the robot of the receiving zone are both at the dock. Until then, the sending zone keeps custody. A completion message from another agent cannot change custody.

Industrial automation uses the same rule. In a semiconductor factory, a transport vehicle and a production tool use the [SEMI E84](https://www.peergroup.com/definition-of-standard/semi-e84/) signals: "a series of optical signals are exchanged to confirm the tool is ready for carrier delivery or removal, then to track handoff progress and completion." VDA 5050 says that the free-text description of an order is "human-readable information only for visualization purposes; this may not be used for any logical processes."

### An independent checker

The checker has its own terminal and a read-only credential. It reads fresh state and compares it with the original request: the measured position, the owner, any pending offer, and the command records. It cannot move a robot or change custody, and its result does not depend on the reports of the workers. The [NIST AI Risk Management Framework](https://doi.org/10.6028/NIST.AI.100-1) recommends a similar split for the people who work on AI systems, with "those building and using the models separated from those verifying and validating the models."

![Sequence diagram of a successful run. The user asks the supervisor to move the parcel from stock to etch. The supervisor observes, then hands off leg 1 to the west zone worker. The west zone worker moves cart-west with the parcel to the dock, observes until the move is finished, offers custody to east, and returns the command ID, the offer ID, the measured position, and the owner west. The supervisor observes again and hands off leg 2 to the east zone worker. The east zone worker observes the parcel and cart-east at the dock, accepts the offer, moves cart-east with the parcel to etch, observes until the move is finished, and returns its evidence. The supervisor hands off the check to the checker, which observes and reports the parcel at 2, 0 metres, owner east, with no pending offer. The supervisor sends the final report to the user.](./sequence.svg)

## What a run looks like

A run takes approximately 3 to 5 minutes. The controller writes one line for each tool call, with the agent that made the call. It also writes one line for each status change of a command. These are the agent and command lines of the log of the run on 2026-10-11, without the times and the run ID. The controller also writes HTTP lines and MCP library lines between them:

```text
transport supervisor called observe()
transport west zone worker called observe()
transport west zone worker called move(command_id=west-parcel-stock-to-dock, robot=cart-west, payload=parcel, destination=dock)
transport run=<run_id> actor=west command=west-parcel-stock-to-dock operation=move status=accepted reason=None
transport run=<run_id> actor=west command=west-parcel-stock-to-dock operation=move status=running reason=None
transport west zone worker called observe()
transport run=<run_id> actor=west command=west-parcel-stock-to-dock operation=move status=finished reason=None
transport west zone worker called observe()
transport west zone worker called offer_handoff(command_id=west-offer-parcel-to-east, payload=parcel, receiver_zone=east)
transport run=<run_id> actor=west command=west-offer-parcel-to-east operation=offer status=finished reason=None
transport west zone worker called observe()
transport supervisor called observe()
transport east zone worker called observe()
transport east zone worker called accept_handoff(command_id=east-accept-parcel-from-west, payload=parcel, robot=cart-east, offer_id=west-offer-parcel-to-east)
transport run=<run_id> actor=east command=east-accept-parcel-from-west operation=accept status=finished reason=None
transport east zone worker called move(command_id=east-parcel-dock-to-etch, robot=cart-east, payload=parcel, destination=etch)
transport run=<run_id> actor=east command=east-parcel-dock-to-etch operation=move status=accepted reason=None
transport run=<run_id> actor=east command=east-parcel-dock-to-etch operation=move status=running reason=None
transport east zone worker called observe()
transport run=<run_id> actor=east command=east-parcel-dock-to-etch operation=move status=finished reason=None
transport east zone worker called observe()
transport supervisor called observe()
transport checker called observe()
```

The controller can also save pictures of the MuJoCo world, with `demo.py serve --record`. These pictures come from a run of the example:

| The west leg | The custody handoff at the dock | Delivered |
| --- | --- | --- |
| ![The blue west cart carries the pink parcel from stock toward the amber dock.](../../../examples/robotics/cross-zone-transport/images/run-2-west-leg.png) | ![The parcel is at the dock, above the two carts. West offers custody to east, and east accepts.](../../../examples/robotics/cross-zone-transport/images/run-3-dock.png) | ![The green east cart has carried the parcel to etch. The blue west cart stays at the dock.](../../../examples/robotics/cross-zone-transport/images/run-5-delivered.png) |

<details>
<summary>Animation of the run (it repeats until you close it)</summary>

![Animation of the run: the blue west cart carries the pink parcel from stock to the amber dock. Then the green east cart carries the parcel from the dock to etch.](../../../examples/robotics/cross-zone-transport/images/run-animation.png)

</details>

The final answer of the supervisor names each worker, each leg, the custody handoff, and the evidence of the checker. This excerpt comes from the same run as the log, with the lines wrapped:

```text
The parcel was delivered to etch, and east now owns it. The independent checker
confirmed this, and its result matches my own observation.
[... the three worker profiles, and a table of the two legs and their four
commands, all finished ...]
Custody transfer: West offered custody with west-offer-parcel-to-east at the
dock after the parcel arrived there. Before the east leg started, I checked that
the parcel was at the dock at [0, 0], west still owned it, and the offer was
pending. East then accepted that exact offer with east-accept-parcel-from-west
before it moved the parcel.
Checker's evidence: The parcel is at [2, 0] m, which is 0.000 m from etch (the
limit is 0.01 m). The owner is east, no offer is pending, all four commands
finished with no reason given, and the run is not stopped.
[...]
This is an assisted kinematic MuJoCo simulation, not real robot transport.
```

## The agents make the decisions

The controller only checks rules. The agents decide what to do. The README of the example has scenarios that show this. On 2026-10-11, we ran six of them again with Claude Code agents:

| Request | What the agents did |
| --- | --- |
| Move the parcel from `stock` to `dock` only | One west leg, and no custody offer. The owner stayed `west`. |
| Move the parcel to `cleanroom` | The supervisor found no `cleanroom` in the scene, and refused. Nothing moved. |
| Use `cart-east` to move the parcel from `stock` to `dock` | The supervisor saw that `cart-east` cannot reach `stock`, and refused. It did not select a different robot. |
| The cross-zone delivery, with a 6 kg parcel and a 5 kg limit on `cart-east` | The supervisor refused before any motion. |
| A different scene, with the zones `stores` and `assembly` | Both zone workers contributed. The tray stopped 0.004 m from `inspection`, owner `assembly`. |
| The cross-zone delivery, with a stop by the operator during the first leg | The move became `interrupted`. The parcel stayed between two locations, owner `west`, and the controller rejected all later actions. |

Two more scenarios give the supervisor more to decide. In the first, the request does not say where the parcel is. The supervisor finds the parcel in the east zone with `observe`, and it plans the job from east to west. In the second, the scene has a third zone, and the supervisor finds a route through two shared docks. Three zone workers move the parcel, and custody changes two times.

![The end of the three-zone run. The pink parcel is on the orange north cart at warehouse. The blue west cart is at the dock, and the green east cart is at north-dock.](../../../examples/robotics/cross-zone-transport/images/three-zones-delivered.png)

## What did not go smoothly

The test runs found problems, and some of them are still open.

- **CAO delivered the first request more than once.** In the runs of 2026-10-11, CAO logged `Delivery to <terminal> not accepted (paste dropped); re-delivering message` while Claude Code started. But the first paste had arrived, so some supervisors received the request two or three times. Each one found the command records of the first request with `observe`, and did not move the parcel again. That is the rule "do not repeat an action that has an uncertain result" in practice. The re-delivery itself is a CAO issue to fix.
- **Some providers cannot keep the agents apart.** Reviews found that OpenCode keeps the MCP servers of all agents in one shared configuration. Antigravity CLI reads the MCP servers of all CAO terminals from one shared file. With these providers, an agent could use the credential of another agent. Hermes gets no MCP servers from the CAO profile, and CAO starts Cursor CLI without the instructions of the profile. `prepare` refuses these four providers.
- **The status of an agent is not a record of the work.** `cao session status` reads the status from the terminal screen. In one run, it showed no last response after the supervisor had written its final report, because a repeated request had started a new turn. The example uses the controller state, not the status of an agent, as the record of what the robots did.

## From the simulation to real robots

On a real site, the CAO parts can stay the same. These parts are the supervisor, the zone workers, the checker, `handoff`, the agent profiles, and the tool allowlists. Only the MCP server behind `transport-sim` changes. Instead of the MuJoCo world, it calls the fleet control of each area.

| In the example | On a real site |
| --- | --- |
| A zone and its zone worker | An area, and the fleet control of the robots in that area |
| `move` | A transport order to the fleet control of the area, for example a VDA 5050 order |
| `observe` | The state that the fleet controls report: robot positions, item locations, and order status |
| `offer_handoff` and `accept_handoff` | The handoff at the transfer point, for example the SEMI E84 signals between a vehicle and a tool |
| The credential of each agent | A separate access key for the fleet control of each area |
| `demo.py stop` | A stop request in software. It does not replace the emergency stop of the robots. |

MCP servers that connect agents to robots exist. For example, [ROS-MCP-Server](https://github.com/robotmcp/ros-mcp-server) "connects large language models (such as Claude, GPT, and Gemini) to robots, enabling bidirectional communication with no changes to existing robot source code."

The agents do not make the robots safe. The robots and their fleet controls must stop for people and obstacles without the agents. Standards such as [ISO 3691-4:2023](https://www.iso.org/standard/83545.html) for driverless industrial trucks and [ISO 10218-1:2025](https://www.iso.org/standard/73933.html) for industrial robots still apply. The [AWS Physical AI Toolchain](https://aws.amazon.com/blogs/physical-ai/introducing-aws-physical-ai-toolchain/) announcement makes a similar point about the cloud: "The cloud coordinates training and fleet management, but it cannot be the control loop for safety-relevant physical tasks." Orchestration belongs in the coordination layer, above that control loop.

## Where the idea comes from

The job comes from the [cross-zone transport example](https://github.com/strands-labs/robots/blob/main/examples/fleet/README.md) of [Strands Robots](https://github.com/strands-labs/robots), an open-source project of [Strands Labs](https://aws.amazon.com/blogs/opensource/introducing-strands-labs-get-hands-on-today-with-state-of-the-art-experimental-approaches-to-agentic-development/). In that example, a fleet coordinator in Python splits the request into legs with fixed rules. Zone orchestrators run the legs over a mesh, after a person approves each leg. The CAO example keeps the job, the zones, the shared dock, and the gated custody handoff. It gives the planning and the check to CAO agents, so you can see a CAO supervisor decide and delegate.

## Try it

You need CAO 2.5.3 or later, tmux 3.3 or later, uv, and a provider CLI that you have signed in to. The example installs MuJoCo as a Python package. It needs no GPU, no display, and no robot hardware.

From the repository root, prepare a run:

```bash
cd examples/robotics/cross-zone-transport
uv sync --locked
export RUN_DIR=/tmp/cao-transport-demo
REQUEST="Move parcel from stock to etch. Coordinate the ownership handoff and have an independent checker verify delivery."
uv run --locked python demo.py prepare --run-dir "$RUN_DIR" --request "$REQUEST" --provider claude_code
```

Then follow the [README of the example](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/robotics/cross-zone-transport/README.md). It starts the controller, installs one run copy of a profile for each agent, launches the supervisor, and shows what to watch. It also has the cleanup steps and the scenarios above.

## What's next

The example is a starting point. These are some directions:

- Connect `transport-sim` to the fleet control of a test area, with people who monitor the robots.
- Let a robot drive empty to the item, so that the item can start anywhere.
- Add scenarios, for example a blocked dock, or a robot that fails during a leg.

Try the example, and tell us what you build in [GitHub Discussions](https://github.com/awslabs/cli-agent-orchestrator/discussions).
