---
name: cao-learning
description: Use when a delegated unit of work finishes and its outcome should be
  reported, when a workflow reaches a natural boundary where a retrospector should
  distill lessons, or when stored lessons should shape the next attempt. Report task
  outcomes and distill lessons so the team improves across runs — report_outcome after
  each unit of work, retrospector handoffs at natural boundaries, and recalling stored
  lessons. Also use in workflows that run repeatedly over similar work items. Requires
  memory.learning_enabled; degrade silently when the tools report disabled.
---

# CAO Self-Learning

CAO workflows can improve as they repeat: outcomes you report feed a
retrospector agent that distills durable lessons into memory, and those
lessons are stored in memory for later sessions to recall. Your job depends on your role.

All of this is opt-in infrastructure. **If `report_outcome` or a memory tool
returns `disabled: true`, skip it silently and continue your task** — learning
is off for this run (often deliberately, e.g. a control run) and that is
expected, not an error.

**A bare `error` with no `disabled` key is different: say so, don't skip it.**
That shape means the tool could not reach a verdict — cao-server is unreachable,
or its `settings.json` could not be read — so learning may well be ON while
nothing is being recorded. Mention it in your response and carry on with the
task.

## If you are a SUPERVISOR

### Report an outcome after each meaningful unit of work

One `report_outcome` call per completed step, delegated task, or work item —
after validation/review, not before:

```
report_outcome(
    task_label="convert package CustomerETL (iteration 2)",
    success=false,
    workflow_name="ssis-migration",
    agent_profile="transformer",           # who did the work (defaults to you)
    score=40,                              # optional 0-100 metric if you have one
    friction_notes="Lookup with partial cache emitted an invalid join; "
                   "improver patched the cache-mode mapping."
)
```

Rules for `friction_notes`:
- 1–3 sentences, **conclusions only** — the root cause, not the story.
- NEVER paste transcripts, logs, stack traces, file contents, or secrets.
- Empty string on a clean pass is fine; the success flag already carries signal.

Report failures faithfully — failed iterations are the most valuable learning
signal. Do not skip reporting because a step went badly.

### Dispatch the retrospector at natural boundaries

After each completed work item (a package, a feature, a review cycle) — not
after every step — hand off to the `retrospector` agent:

```
"Retrospect on session <session_name>, workflow <workflow_name>,
 item <item name>. Agents involved: <profiles>."
```

Wait for its one-line summary (outcomes read, lessons stored) and record it in
your run log. If no retrospector profile is available, skip this step.

### Pass lessons downstream

Lessons are stored in agent scope, and the `<cao-memory>` block injected into
a new session does not include agent scope, so it won't show them. If you
`memory_recall` lessons from previous runs and a lesson's `Applies when:`
clause matches the task you are delegating, include it in your handoff
message — a worker only sees its own agent-scope lessons if it recalls them
or has them promoted into its profile.

## If you are a WORKER

1. **Look up lessons first.** Lessons are not injected into your session.
   Before working, run `memory_recall` for the task and scan your own
   instructions for a `## Learned Patterns` section (present only after an
   operator has promoted lessons), looking for lessons whose `Applies when:`
   clause matches the current task. Apply them before falling back to first
   principles.
2. **Store new lessons immediately** when you discover something durable — a
   mapping that works, a trap that recurs, a tooling quirk:

   ```
   memory_store(
       content="Preserve a Lookup transform's cache mode instead of defaulting "
               "to a full-table read. Applies when: translating a Lookup whose "
               "CacheType is not full cache.",
       scope="agent",
       memory_type="feedback",
       key="honor-lookup-cache-mode"
   )
   ```

   Format contract: 1–2 sentence conclusion, then `Applies when: <trigger>`.
   The trigger clause is how future curators match your lesson to a task.
3. **Correct, don't accumulate.** If a stored lesson proves wrong, re-store
   the corrected text under the SAME key (or `memory_forget` it). Never store
   a contradicting lesson under a new key.

## If you are the RETROSPECTOR

Follow your profile (`retrospector.md`). Read outcomes with the
`list_outcomes` tool; store worker-craft lessons with
`store_lesson(target_agent_profile=..., content=...)` — NOT `memory_store`,
which files agent-scope lessons under YOUR profile, where the worker will
never see them. The quality bar, in brief: 0–3 lessons per retrospection,
each supported by a concrete outcome, actionable, general enough to recur,
under 400 characters, ending with `Applies when:`. "No lessons" is a valid
and often correct answer.

## What happens to lessons afterwards

- Lessons are ordinary agent-scope memories. They are not injected into new
  sessions; an agent sees them by calling `memory_recall`. Each recall that
  returns a lesson adds to its `access_count` (at most once a minute per
  lesson). They are also lint-checked for contradictions and audited.
  Lessons are filed under the profile name, so an ephemeral agent, whose name
  is generated for each launch, will not find lessons stored for an earlier one.
- Nothing is promoted on its own. Only when an operator runs
  `cao memory promote <agent> --apply` (and has turned on
  `instruction_promotion_enabled`) are lessons recalled at least 3 times copied
  into your profile's `## Learned Patterns` block — that block is
  CAO-maintained; treat its contents as instructions, and don't edit it
  by hand.
