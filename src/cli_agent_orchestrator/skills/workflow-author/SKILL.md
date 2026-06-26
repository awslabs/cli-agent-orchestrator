---
name: workflow-author
description: Author a CAO workflow spec in-band, mid-session. Produces a YAML WorkflowSpec
  file on disk — the SAME artifact a human writes by hand — validated by the same
  `cao workflow validate` surface. Use when the user asks you to create, draft, or extend
  a multi-agent workflow. Authoring ends at a validated spec file; running it is separate.
---

# Authoring a CAO Workflow Spec

A CAO workflow is a YAML file describing a DAG of agent steps. You author it the
**same way a human does** — there is no agent-only grammar, no agent-only validation
path, and no agent-only file format. The artifact you produce is indistinguishable
from a hand-written one: same model, same file location, same validation outcomes.

> Your job ends at a **validated spec file on disk**. Authoring does NOT run the
> workflow — the `run` verb is not part of this skill. Never claim a spec you authored
> will execute or has executed.

## 1. Scout for existing specs first

Before authoring, see what already exists so you extend rather than duplicate. Use the
`workflow-scout` agent profile (read-only) or run:

```
cao workflow list
cao workflow get <name>
```

## 2. Write the YAML spec

Write a file to the workflow spec directory (default `~/.aws/cli-agent-orchestrator/workflows/<name>.yaml`).
A spec has a `name`, an optional `description`, a `mode`, optional typed `inputs`, and a
list of `steps`. Each step has `id`, `provider`, `agent`, `prompt`, and an optional
`output_schema` (JSON-Schema, Draft 2020-12).

```yaml
name: review-pipeline
description: Implement a change, then review it.
mode: sequential
steps:
  - id: implement
    provider: claude_code
    agent: developer
    prompt: Implement the feature described in the ticket.
    output_schema:
      type: object
      properties:
        files_changed:
          type: array
          items:
            type: string
      required: [files_changed]
  - id: review
    provider: claude_code
    agent: reviewer
    prompt: Review the implementation.
```

**Honesty discipline:** `mode: parallel`, `mode: pipeline`, `mode: loop`, `when:`, and
the loop guards are **reserved** — they validate but do not run yet. If you use one,
`validate` will report it as "reserved (not built yet)". Do NOT present a reserved
construct as something that will execute.

## 3. Validate

Validate the spec with the same verb a human uses:

```
cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/review-pipeline.yaml
```

- `valid` (exit 0) — the spec is structurally sound.
- `valid` with `note: construct X is reserved (not built yet)` — sound, but uses a
  reserved construct that will not run yet.
- `invalid` (exit 1) — fix the reported errors and re-validate.

Iterate until the spec validates. The authored artifact is now ready for a human (or,
in a later Bolt, the run engine) to run.
