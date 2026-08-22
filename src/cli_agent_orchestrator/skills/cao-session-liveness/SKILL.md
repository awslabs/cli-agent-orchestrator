---
name: cao-session-liveness
description: Verify whether a CAO session is actually alive and what it really
  said, before reporting progress or completion to a user. Use alongside
  cao-session-management whenever you launch, poll, or report on a CAO session —
  especially when a session appears stalled, a send times out, or a status value
  looks inconsistent with the output.
---

# CAO Session Liveness

Companion to `cao-session-management`, which covers the mechanics of launching
and messaging sessions. This skill covers a single question that mechanics alone
cannot answer: **is the session actually alive, and is the status telling me the
truth?**

## Why this matters

Every CAO provider infers agent state by pattern-matching the rendered terminal
screen. There is no structured protocol between CAO and the provider CLI. A
provider that has exited, crashed, or stalled on an unanswerable dialog can
leave a screen that still matches an `idle` or `processing` pattern.

The consequence is specific and it is the failure this skill exists to prevent:
**reporting progress on a session that is already dead.**

## The two-signal rule

Never report readiness, progress, or completion from a status value alone.
Always corroborate with output before you tell a user anything:

1. Read the status (`get_terminal_status`, or `cao session status SESSION`).
2. Read the output tail (`read_session_output` / `get_terminal_output`, or
   `cao session status SESSION --json` and inspect `last_output`).
3. If the two disagree, **the output wins.**

A status of `idle` with an output tail showing a shell prompt means the CLI
exited. Report the session as dead, not as ready.

## Dead-session discriminators

Treat any of the following in the output tail as proof the provider is no longer
running, regardless of the reported status:

| Signal | Means |
|---|---|
| `Session ended.` / `Resume with: <cli> --resume-id ...` | The CLI exited on its own |
| `error: Conflicting options:` or a usage/help banner | The CLI rejected its launch flags and never started |
| `API Error (...)`, `400`, or a model/auth failure | The provider started but cannot reach a model |
| A bare shell prompt with a directory and timestamp, no agent chrome | The pane fell back to the shell |
| An output read that fails with an extraction error | No response boundary on screen; corroborate before trusting |

A session parked in `waiting_user_answer` that never advances is usually stalled
on a dialog nothing will answer. Treat it as dead weight, report it to the user,
and do not silently kill it.

### Not a dead session: a finished handoff worker

A blocking `handoff` tears its worker down once it returns. The worker terminal
ID the conductor reports was valid **during** the call and is gone afterwards, so
querying it later is expected to fail:

- `get_terminal_status` / `GET /terminals/<id>` returns not-found
- `cao session status SESSION --workers` lists no workers

Neither is evidence the conductor invented the delegation. Confirm a handoff from
the **conductor's own transcript** — a full-mode output read showing the
`handoff` tool call, its `agent_profile`, and the returned output — not from the
terminal registry. Only a non-blocking `assign` leaves a worker alive to query.

Do not accuse a conductor of fabricating a delegation on the strength of a
missing terminal alone.

## Verify a provider before depending on it

Provider reliability varies, is version-sensitive, and changes as upstream CLIs
release new dialogs and flags. Do not assume; verify once per environment:

1. Launch a throwaway session in a scratch directory.
2. Apply the two-signal rule.
3. Send a trivial task with a short timeout and confirm output returns.
4. Shut the session down.

Known reliability characteristics, as context for interpreting what you see:

| Provider | Detection basis | What to watch for |
|---|---|---|
| `kiro_cli` | Version-specific prompt, credits, and separator patterns | New startup dialogs that default to a decline option; flag combinations the installed CLI rejects |
| `hermes` | Idle timer stable across repeated polls | Custom themes break prompt matching; slowest to confirm completion. Patterns are overridable by environment variable |
| `opencode_cli` | Alt-screen TUI completion marker | Scrollback is roughly one viewport; a long single response can lose its own top and fail extraction |
| `claude_code`, `codex` | Rendered-screen detection | Generally stable headless; still apply the two-signal rule |

If a provider fails to launch headlessly, report the exact signature to the user
and offer a different provider. Do not retry the same launch repeatedly — a flag
rejection or a declining dialog will fail identically every time.

## Interpreting a send that does not return

- A **timeout is not a failure.** The agent is still working; the caller stopped
  waiting. Say so, and check again later.
- **Never re-send a task after a timeout.** The original may still be running,
  and a duplicate risks conflicting work in the same directory.
- A **busy terminal refuses input.** Wait for `idle` or `completed`; do not force.
- An **async send returns nothing by design.** Poll afterwards, applying the
  two-signal rule.

## Record what each session is for

CAO stores a session's name, not its purpose. An inventory of live sessions
cannot tell you which is safe to touch.

Keep a short registry outside CAO — one line per session you launch: name,
provider, working directory, purpose, date. Update it on launch and on
shutdown, and read it before answering any question about what a session is
doing or before acting on one.

## Do not act on sessions you did not launch

Long-running sessions may hold real, unrecoverable work. Reads are always safe.
Before sending to or shutting down a session you did not start yourself, ask the
user first. Never issue a shutdown that targets all sessions at once.

## Related

- [cao-session-management](../cao-session-management/SKILL.md) — launching,
  messaging, and worker communication mechanics
