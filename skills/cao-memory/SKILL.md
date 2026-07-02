---
name: cao-memory
description: Store, recall, and forget durable facts with CAO memory — user preferences,
  project conventions, decisions, and corrections that should persist across sessions and
  agents. Use proactively to check memory before asking the user, and to save anything
  worth remembering. Distinct from any provider-native memory.
---

# CAO Memory

CAO gives every agent a shared, persistent memory. A fact you store in one session is
available to a brand-new agent in a later session — even on a different provider. Use it so
the user never has to repeat themselves.

These are CAO's cross-provider memory tools (`memory_store`, `memory_recall`,
`memory_forget`), exposed by the CAO MCP server. They are **distinct from any
provider-native memory** the CLI tool may have.

## Scopes and types

Every memory has a **scope** (where it applies) and a **type** (what kind of fact it is).

| Scope | Applies to | Use for |
|-------|-----------|---------|
| `project` (default) | This repo / working directory | Conventions, architecture, build rules |
| `global` | Every project | User identity, durable cross-project preferences |
| `federated` | Every project on this machine | Reusable, repo-independent lessons worth sharing across all your work (rejects credentials) |
| `session` | This run only | Short-lived task context |
| `agent` | This agent role | Role-specific working notes |

**Types:** `project` (default), `user` (who the user is / preferences), `feedback`
(corrections and how-to-work guidance), `reference` (pointers to docs, tickets, URLs).

## Recall — check memory BEFORE asking the user

At the start of a task, and whenever you're about to ask the user something they may have
already told you, search memory first.

```
memory_recall(query="database widgets endpoint testing")
```

Omit `scope` to search all scopes (results follow precedence session → project → global → agent → federated).
Filter with `scope=` or `memory_type=` when you know where to look. Recall is for searching
*beyond* what was auto-injected (see below) — don't re-recall what's already in front of you.

## Store — save anything worth remembering, immediately

Store the moment you learn something durable. Don't wait until the end of the session.
**Store conclusions, not transcript.** Keep each memory to 1–2 sentences.

Store when you hit any of these:
- **A correction** — "No, we use DynamoDB here, not SQL." → store it so no agent makes that
  mistake again.
- **A decided convention** — "Every endpoint must have a pytest test before merge."
- **A user preference** — how they like work done, tools they prefer.
- **A non-obvious project constraint** — something you couldn't infer from the code.

```
memory_store(
    content="Use DynamoDB for widgets-api; never SQL.",
    scope="project",
    memory_type="project",
    key="widgets-database",          # optional; auto-slugged from content if omitted
)
```

Same `key` + `scope` upserts (updates in place) rather than duplicating.

### Share across all your projects — `scope="federated"`

When a lesson is durable **and not specific to this repo** — a reusable library gotcha, a
debugging trick, a tooling preference that holds everywhere — store it with
`scope="federated"` so it follows you into every project on this machine, not just this one.

```
memory_store(
    content="tmux paste-buffer needs `-p` or multi-line input loses bracketed-paste framing.",
    scope="federated",
    memory_type="reference",
)
```

Federated memories sit at the **lowest recall precedence** — a project-local fact with the
same key always wins — so federating is safe: it only adds a fallback, never overrides what's
true here. To un-share, `memory_forget(key=..., scope="federated")`.

- **Never federate secrets.** Tokens, keys, and passwords are **rejected automatically** on a
  federated write — and they'd be exposed to every project anyway. Keep credentials out of
  memory entirely.
- **When in doubt, use `project`.** Federate only what you're confident is reusable everywhere.

## Forget — remove what's wrong or superseded

```
memory_forget(key="widgets-database", scope="project")
```

Use this when a stored fact becomes outdated or was wrong. Prefer correcting (re-store with
the same key) over leaving stale facts in memory.

## Auto-injection (already happening)

On launch, CAO writes the most relevant memories for this working directory into the file
your CLI reads on startup (Claude Code: `.claude/CLAUDE.md`; Codex: `AGENTS.md`; Kiro:
`.kiro/steering/cao-memory.md`). So you usually begin a task already knowing the project's
key facts — `memory_recall` is for digging up anything that wasn't injected.

## Habits

1. **Recall before asking.** The answer may already be stored.
2. **Store the instant you learn something durable** — corrections, conventions,
   preferences, constraints.
3. **One fact per memory, 1–2 sentences.** Conclusions, not conversation.
4. **Pick the right scope:** user-wide → `global`; this repo → `project`.
