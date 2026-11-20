# CAO feature map

## Why this exists

Coding agents copy what they see and guess what things are called. CAO has many
moving parts: 13 real providers plus a mock one, three terminal backends, sessions,
terminals, an inbox, workflows and memory. The facts about how to check each part are
spread across `docs/`, test files, skills and PR threads. Some of the most useful facts
are the traps: a status that reads one thing while the screen shows another.

This map gives agents (and the people who steer them) shared names and a runnable check
for each area. It also lets a review ask "show me the evidence from the Drive step"
instead of reading a large diff and trusting it.

## How to read it

Each area is one short file, and each file answers the same four questions:

1. **What exists:** the pieces of the area, by the names everyone should use.
2. **How a user reaches it:** the CLI command, API route, UI screen or setting.
3. **How to drive it:** commands an agent can run to see it working, and the output to
   expect.
4. **What usually lies:** known traps, flaky spots and misleading signals, each written as
   symptom, then cause, then fix.

Open only the file for the area you changed. Each file links to the existing doc for
depth instead of copying it.

## Conventions

- Commands assume a fresh install and the default server at `http://localhost:9889`.
- Output is pasted from a real run on CAO 2.5.4, shortened with `…`. Ids such as
  `cao-<id>` and `<terminal-id>` stand for the random ids CAO prints.
- A "what usually lies" item cites the code or doc that backs it (`path:line` on the
  commit the entry was last checked against). An item that could not be checked in code
  says "seen on <date>, not re-verified".
- To drive an area without touching your real CAO, use a scratch server. It needs a
  home of its own, because the server lock is per home, not per port (see
  [server-and-health](server-and-health.md)):

  ```bash
  export CAO_HOME_DIR="$(mktemp -d)" CAO_API_PORT=19889
  cao init && cao-server --port 19889
  ```

  Every `cao` command run with the same two variables talks to that server. Stop it with
  Ctrl+C and delete the folder when you're done. A scratch `CAO_HOME_DIR` doesn't
  isolate tmux or herdr; see [backends](backends.md).

## The update rule

A PR that changes an area's behaviour updates that area's entry in the same PR.
Otherwise the map goes stale and starts to lie, which is worse than no map.

The map describes the product as it is today. It shares no data with a status page or
a roadmap.

## Sweep order

To answer "did we break anything broadly?", walk the areas in this order: the basics
first, then sessions and delegation, then the rest, and finally an end-to-end journey.

| # | Area | Covers | File |
|---|---|---|---|
| 1 | server-and-health | `cao-server`, `cao init`, `cao info`, `GET /health` | [server-and-health.md](server-and-health.md) |
| 2 | backends | tmux, herdr and herdr-app | [backends.md](backends.md) |
| 3 | providers | the `ProviderType` values and `mock_cli` | [providers.md](providers.md) |
| 4 | sessions-and-terminals | `cao launch`, `cao session`, `cao terminal`, `/sessions`, `/terminals/{id}` | to come |
| 5 | delegation-and-inbox | handoff, assign, `send_message`, inbox delivery | to come |
| 6 | profiles-and-install | `cao profile`, `cao install` | to come |
| 7 | tool-restrictions-and-security | | to come |
| 8 | workflows | `cao workflow` | to come |
| 9 | flows-and-schedule | `cao schedule` | to come |
| 10 | memory-and-graph | `cao memory`, the vault | to come |
| 11 | ephemeral-agents | | to come |
| 12 | agent-plugins-and-skills | `cao skills` | to come |
| 13 | fleet-and-remote | `cao fleet`, `cao worker` | to come |
| 14 | web-ui-tui-and-ag-ui | | to come |
| 15 | decisions | `cao decisions` | to come |
| 16 | mcp-server-and-apps | `cao mcp-server` | to come |
| 17 | journeys | end-to-end paths across areas: launch, delegate, get the result back | to come |

## Adding an entry

Copy the four headings from an existing file. Write the Drive step by running it, and
paste the real output. For each "what usually lies" item, cite the code or doc that
backs it, or mark it "seen on <date>, not re-verified". Then add the file to the table
above.
