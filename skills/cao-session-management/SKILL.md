---
name: cao-session-management
description: Find available CAO (CLI Agent Orchestrator) agent profiles, and talk to the
  conductor and worker terminals of a running CAO session, including unblocking a stuck
  worker. Use when choosing a profile to launch, messaging or unblocking workers, or
  diagnosing a stuck session. For preflight, launching, session commands, statuses,
  delegation and safety, run `cao --skill` first.
---

# CAO Session Management

## Operating CAO

Run `cao --skill` for the CAO operating guide: preflight, launch, session commands,
statuses, delegation, and safety. Run `cao <group> --help` for exact syntax.

Two rules apply to every launch from an agent:

- Use `--auto-approve`. It skips the confirmation prompt and leaves the profile's tool
  policy unchanged; whether that policy is enforced depends on the provider (read the
  `Enforcement:` line launch prints). `--yolo` also skips the prompt but removes all
  tool restrictions, so the agent can run any command, including `aws`, `rm` and
  `curl`. Use `--yolo` only when the user asks for it.
- If a `cao` command cannot reach the server, check it with
  `curl -sf http://localhost:9889/health`.

A reported status is inferred from the rendered terminal screen, so it can disagree
with reality. Before reporting readiness, progress, or completion to a user,
corroborate the status with an output read; see
[cao-session-liveness](../cao-session-liveness/SKILL.md).

## Discovering Available Profiles

Profiles are CAO-level entities, installed with `cao install` regardless of which CLI
provider runs them. To find available profiles:

| Source | Command |
|--------|---------|
| All available profiles, with the source each one resolved from | `cao profile list` (no server needed), or `curl -sf http://localhost:9889/agents/profiles` |
| Profiles matching a keyword | `cao profile find "<keywords>"` |
| Custom/local profile files only | `ls ~/.aws/cli-agent-orchestrator/agent-store/` |
| Profiles installed via `cao install <name>` | `ls ~/.aws/cli-agent-orchestrator/agent-context/` |
| Profile installation and keyword discovery | see [Agent profile installation](../../docs/agent-profile.md#installation) and [profile discovery](../../docs/agent-profile.md#profile-discovery) |
| Provider-native list (`kiro_cli` only) | `kiro-cli agent list`, useful because CAO mirrors profiles into `~/.kiro/agents/` |

`cao profile list` and the HTTP endpoint return the same list. Discovery scans the
local store (`agent-store/`) first, then provider-specific directories (including
`agent-context/`), then extra directories from settings, and the built-in packaged
store last. The first match wins, so a local copy of a profile shadows the built-in
one with the same name. Each entry has a `source` label showing where it came from.

A profile runs on its frontmatter `provider:` unless `--provider` overrides it; the
default is `kiro_cli`. Run `cao install --help` for the provider IDs.

If unsure which profile to use, ask the user rather than guessing.

## Worker Communication

Inside a session, the conductor talks to workers via two MCP tools, described below.

**Prefer communicating through the conductor** (`cao session send SESSION "msg"`) rather
than directly to worker terminals. Bypassing the conductor leaves it without state on
what was asked or answered, which causes confusion. Two exceptions: unblocking a stuck
worker, and follow-up questions to a persistent async worker (see below).

**handoff** (blocking) — conductor sends task and waits for the worker to reach
`COMPLETED` status, then reads the output. If it times out, the worker is still
running — the conductor just stopped waiting.

**assign** (non-blocking) — conductor sends task and returns immediately. The worker
is expected to call `send_message` back to the conductor's terminal ID when done.
Each terminal only knows its own ID via `$CAO_TERMINAL_ID`.

By default the conductor uses sync (handoff). You can override this by explicitly
asking it to use async or sync protocol when sending it a task.

Async workers (assign) stay alive after completing their task and can answer follow-up
questions — useful for ongoing investigation where you want to keep querying the same
worker. In this case, sending directly to the worker terminal is appropriate:
```bash
cao session send SESSION "<follow-up question>" --terminal <worker-terminal-id>
```

## Kiro SOP Workflows

For SOP-driven work on the `kiro_cli` provider, send `/prompts` to discover the
available SOPs, then the matched SOP name prefixed with `@` (for example
`@my-sop-name`), then the task. Send each as a separate message, and wait for
`completed` status between them.

## Common Mistakes

**Wrong working directory** — agents won't find files, builds fail with confusing errors.
Pass an absolute path in single quotes to `--working-directory`. A wrong path cannot be fixed
without shutting down and relaunching; ask the user if the path is unclear.

**Launching with `--yolo` to avoid prompts** — `--auto-approve` already skips the
confirmation prompt. `--yolo` additionally removes every tool restriction, which the
task almost never needs.

**Stuck conductor** — conductor is waiting on a worker that stopped responding. Check
the worker's status first, then decide: prompt it to continue and send results back,
or ask it to resend if it already finished. Never re-delegate work that may still be
running — it risks duplicate work.
```bash
cao session status SESSION --workers
cao session status SESSION --terminal <worker-terminal-id>
cao session send SESSION "continue your work, then send results to terminal <conductor-terminal-id>" --terminal <worker-terminal-id>
```
Get the conductor's terminal ID from `cao session status SESSION --json`.
