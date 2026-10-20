"""Agent-facing help text for the ``cao`` CLI.

Plain string constants, stdlib only: ``cao --help`` and ``cao --skill`` must
work on a machine where nothing else in CAO can be imported or initialized.
Keep this module free of package imports.
"""

# ``\b`` stops Click from rewrapping the two lines into one paragraph.
FOOTER = (
    "\b\n"
    "AI agent? If a CAO skill is already in your context, skip this.\n"
    "Otherwise run: cao --skill"
)


AGENT_GUIDE = """\
---
name: cao
description: Operate CLI Agent Orchestrator (CAO) from outside it. Start agent sessions, delegate a task to a worker agent, check status and output, and shut down. Use when the user asks to use CAO or the cao CLI.
---

# CAO

CAO (CLI Agent Orchestrator) runs coding-agent CLIs (Kiro, Claude Code, Codex,
and others) as terminals inside tmux (or herdr, when `cao config get terminal.backend` says
`herdr`), behind a local HTTP server, `cao-server`. Agents in a session
delegate work to each other. You drive it with the `cao` CLI.

Check where you are first. If `CAO_TERMINAL_ID` is set, CAO launched you and
your injected CAO skills apply (`cao-worker-protocols`,
`cao-supervisor-protocols`). This guide is for an agent outside CAO.

## Pick an entry point

- One task, wait for the answer: `cao agent handoff`.
- A supervised multi-agent session you check on later: `cao launch --headless`,
  then `cao session`.
- A repeatable multi-step pipeline: `cao workflow`. Ask the user before
  running one.
- Your host speaks MCP: `cao-ops-mcp-server` exposes launch, send, status,
  output and shutdown tools.

## Preflight

1. Is the server up? `curl -sf http://localhost:9889/health`. If not, ask the
   user to run `cao-server` in its own terminal. It runs in the foreground on
   127.0.0.1:9889 (`CAO_API_HOST` and `CAO_API_PORT` override). `cao init` is
   optional; the server sets up its database and skills when it starts.
2. Pick a profile. `cao profile list` shows installed profiles and
   `cao profile find "<keywords>" --json` searches them. Built-ins include
   code_supervisor, developer and reviewer. Install one with
   `cao install <name|path|url>`. A local profile with the same name as a
   built-in wins.
3. Pick a provider only if the user asked for one. Precedence: `--provider`,
   then the profile's `provider:` field, then `kiro_cli`. `cao install --help`
   lists the provider IDs.

State lives under `~/.aws/cli-agent-orchestrator/` (`CAO_HOME_DIR` relocates it).

## Delegate one task

```
cao agent handoff <profile> "<task>" --working-directory '<abs path>' [--timeout 600] [--json]
```

This creates a worker, sends the task, blocks until the worker finishes,
prints its output, and deletes the worker. It works outside CAO by creating a
fresh session. The worker's terminal ID goes to stderr as soon as the worker
exists, so an interrupted handoff still leaves you a handle. Exit codes: 0
done, 1 failed or timed out, 130 interrupted.

To avoid blocking, add `--no-wait`, then poll with `cao agent status <terminal_id>`,
read the output with `cao agent result <terminal_id>`, and free the worker with
`cao agent cancel --delete <terminal_id>`. `cao agent assign` needs
`CAO_TERMINAL_ID`, so it does not work from outside CAO.

## Run a session

```
cao launch --agents <profile> --headless --auto-approve --session-name <name> --working-directory '<abs path>' "<task>"
```

- `--headless` is required. Without it CAO attaches the session to your terminal.
- `--auto-approve` skips the confirmation prompt and leaves the tool policy as
  it is. Whether that policy is enforced depends on the provider; read the
  `Enforcement:` line that launch prints. `--yolo` removes every restriction;
  use it only when the user asks for it.
- With a task, launch waits for the provider to start and confirm delivery,
  then up to 300 s for the result, and prints it. `--async` returns once the
  server confirms delivery.
- Sessions are stored as `cao-<name>`; CAO adds the prefix if you leave it out.
  Use the prefixed name in later commands.

Then use:

- `cao session list`
- `cao session status cao-<name> [--workers] [--json]` (the conductor first;
  `--json` gives terminal IDs)
- `cao session send cao-<name> "<message>" [--async] [--timeout 300]` (send
  follow-ups to the conductor, not to its workers)

Terminal statuses: `unknown`, `idle`, `processing`, `completed`,
`waiting_user_answer`, `error`. Status is read from the screen, so check it
against the output tail; if they disagree, trust the output. A timeout is not a
failure: check the status before doing anything else, and never re-send the
same task.

## How delegation behaves

- A handoff worker is deleted on success, so looking it up afterwards returns
  not-found. That is expected.
- An assigned worker keeps running until it is deleted, and reports back
  through the inbox.
- Inbox messages are delivered only when the target is `idle` or `completed`.
- `cao agent handoff --use-worktree` gives a worker its own git worktree under
  `<repo>/.cao/worktrees/`. Deleting the terminal discards uncommitted
  changes in it.

## Side effects and safety

- Whenever CAO creates a terminal (launch or handoff), it may write CAO memory
  into the working directory: `.claude/CLAUDE.md`, `AGENTS.md`, or
  `.kiro/steering/`, depending on the provider.
- Shut down only what you started: `cao shutdown --session cao-<name>`. Never
  run `cao shutdown --all`. Do not send to or stop a session you did not
  launch without asking the user.
- These act immediately, with no confirmation prompt: `cao shutdown`,
  `cao agent cancel --delete`, `cao worker release`, `cao schedule remove`,
  `cao skills remove`, `cao memory compact`, `cao memory import --conflict replace`,
  `cao update`.
- Keep your own note of each session you launch (name, profile, directory,
  purpose). CAO does not record the purpose.

## Syntax and more

The installed CLI is the authority for syntax: run `cao <group> --help`. Never
probe a mutating command by leaving out its arguments.

- `cao agent`: delegate to and inspect workers
- `cao session`, `cao terminal`: drive sessions and individual terminals
- `cao profile`, `cao install`: find and install agent profiles
- `cao workflow`: multi-step pipelines
- `cao schedule`: scheduled flows (needs the server)
- `cao config`, `cao env`: settings and environment variables
- `cao memory`, `cao skills`: agent memory and the skill store
- `cao fleet`, `cao worker`: remote clusters
"""
