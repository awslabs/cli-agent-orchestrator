---
name: aidlc-portfolio
description: Coordinate multiple AI-DLC workflows across repositories and Git worktrees using an evidence-backed portfolio catalog and deterministic workspace tooling. Use when initializing an AI-DLC portfolio workspace, discovering organization or business context, registering projects and dependencies, creating child intents and worktrees, validating dispatch readiness, monitoring parallel AI-DLC sessions, or synthesizing cross-project outcomes.
---

# AI-DLC Portfolio

Maintain a thin control plane around existing AI-DLC workflows. Never replace
or modify the child workflow engine.

Resolve every relative resource path against the directory containing this
`SKILL.md`. In the commands below, set `SKILL_DIR` to that absolute directory:

```bash
SKILL_DIR=/absolute/path/to/skills/aidlc-portfolio
```

## Operating Rules

1. Run `"$SKILL_DIR/scripts/portfolio.ts"` for every portfolio mutation.
2. Keep portfolio state at `<root>/portfolio` and project checkouts at
   `<root>/repositories`.
3. Give each active child intent its own path under `<root>/worktrees`.
4. Dispatch one `aidlc_runner` per child intent with CAO `assign`.
5. Treat child AI-DLC state as opaque. Never edit `aidlc-state.md` or derive its
   next stage.
6. Require evidence and confidence for discovered catalog relationships.
7. Ask the human about missing or uncertain business facts before verification.
8. Run dispatch validation before starting or resuming a child runner.
9. Treat the portfolio supervisor as the sole shared-memory approval authority.
10. Never merge worktree-local `project.md` or `team.md` changes.
11. The supervisor owns bootstrap; the operator should not have to prepare the
    workspace, repository checkout, intent files, worktrees, or child harness.
12. Never dispatch until portfolio discovery has explicit human confirmation.
13. Never answer a child AI-DLC question without human input.
14. Stage, project, and verify child harnesses only through `harness` commands.
15. Advance parent work only through the persisted portfolio lifecycle.
16. Require one structured result per child and pass convergence before Learn.

## Bootstrap From Work Items

The normal entry point is an operator naming a repository and one or more work
items. Perform setup on their behalf:

1. Derive a stable portfolio ID from the repository and sorted work-item IDs.
2. Use the terminal's launch directory as the portfolio root unless the
   operator explicitly supplies another root. Never create a nested root.
3. Run `init` against that exact root; never hand-create portfolio state.
4. Create a fresh canonical checkout under `<root>/repositories`. Do not reuse,
   clean, reset, or remove an existing user checkout or worktree.
5. Inspect the work items and register the project, dependencies, and one
   portfolio intent per independent work item using templates and utility
   commands.
6. Create each worktree through `worktree create`.
7. Locate the Claude AI-DLC distribution at
   `$HOME/Project/aidlc-workflows/dist/claude`. Run `harness stage`, `harness
   sync`, and `harness verify` to project one manifest-pinned Opus 5 runtime
   into every worktree.
8. Run `doctor` and `dispatch check`, then assign all dependency-ready
   `aidlc_runner` sessions concurrently with CAO `working_directory` set to
   each absolute worktree.
9. Report the generated root, intent IDs, worktree paths, and terminal IDs.

The operator owns creation of the empty parent/root directory and launches the
supervisor from it. Ask them only when a required repository, work item,
credential, or AI-DLC distribution cannot be discovered. Do not turn routine
bootstrap inside the root into operator instructions.

## Initialize

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" init \
  --root /absolute/workspace \
  --id portfolio-id \
  --name "Portfolio Name"
```

Then run:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" doctor --root /absolute/workspace
```

Initialization is explicit and idempotent. Never use a hook to create a
workspace.

For a workspace created before lifecycle state schemaVersion 2, migrate once:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" lifecycle migrate --root <root>
```

Read [portfolio-lifecycle.md](references/portfolio-lifecycle.md). After every
startup or callback, inspect the durable phase and exact next action:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" lifecycle status --root <root>
```

## Discover And Register

Read [catalog-model.md](references/catalog-model.md) before discovering or
changing projects, components, capabilities, or dependencies. Read
[coordination-rules.md](references/coordination-rules.md) before creating child
intents, worktrees, or dispatches.

Inspect technical evidence before asking questions. Use the templates under
`assets/templates`, then register validated files:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" project register --root <root> --file <project.yaml>
bun "$SKILL_DIR/scripts/portfolio.ts" dependency add --root <root> --file <dependency.yaml>
bun "$SKILL_DIR/scripts/portfolio.ts" intent create --root <root> --file <intent.yaml>
```

Record unresolved questions under `<root>/portfolio/questions`. Support
`Unknown`, `Defer`, and referral to another stakeholder. Do not upgrade a fact
from `discovered` or `proposed` to `verified` without human approval.

After catalog registration, render `assets/templates/discovery-decision.yaml`.
Record the human's decision for organization, business outcomes, business
capabilities, and the complete dependency list:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" discovery confirm \
  --root <root> --file <discovery-decision.yaml>
```

For every `unknown` or `deferred` disposition, the corresponding fact name must
appear in `acceptance.unknowns` or `acceptance.deferrals`. The utility stores a
catalog revision; any later catalog change makes confirmation stale and blocks
dispatch until the human reviews it again.

Project registration records a canonical checkout; it does not clone one.
Before worktree creation, ensure the registered path exists under
`<root>/repositories` and is a Git repository.

## Harness Lifecycle

Read [harness-lifecycle.md](references/harness-lifecycle.md) before staging or
updating child runtimes. Stage the source distribution after registering
intents and creating their worktrees:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" harness stage \
  --root <root> \
  --source "$HOME/Project/aidlc-workflows/dist/claude"

bun "$SKILL_DIR/scripts/portfolio.ts" harness sync --root <root>
bun "$SKILL_DIR/scripts/portfolio.ts" harness verify --root <root>
```

The staged manifest records source revision, source and staged hashes, overlay
revision, and staging time. The default overlay pins the Claude `opus[1m]`
alias to `global.anthropic.claude-opus-5[1m]`. Use `--project` and `--intent`
to sync or verify a subset.

Never bypass a tracked-path refusal. Reconcile project-owned `.claude/` or
`aidlc/` content explicitly before retrying.

## Dispatch

Create or validate a worktree:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" worktree create \
  --root <root> --project <project> --intent <intent> \
  --branch <branch> --base <base>

bun "$SKILL_DIR/scripts/portfolio.ts" harness verify \
  --root <root> --project <project> --intent <intent>

bun "$SKILL_DIR/scripts/portfolio.ts" dispatch check \
  --root <root> --project <project> --intent <intent>
```

The worktree must pass harness verification and dispatch validation. Render
`assets/templates/runner-message.md`, then use CAO `assign` with
`agent_profile: aidlc_runner` and `working_directory` set to the validated
absolute worktree. The supervisor profile enables this CAO parameter through
`CAO_ENABLE_WORKING_DIRECTORY=true`. After assignment returns a terminal ID,
record it:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" session update \
  --root <root> --project <project> --intent <intent> \
  --status active --terminal <terminal-id>
```

If assignment fails, leave the session pending. Never mark a session active
before CAO returns a terminal ID.

## Parent Lifecycle

Advance only one phase at a time:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" lifecycle advance \
  --root <root> --to discover
```

Repeat for `confirm` and `plan`. Entering Dispatch requires explicit human plan
acceptance:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" lifecycle advance \
  --root <root> --to dispatch --accepted-by "<human decision maker>"
```

After all child sessions complete, advance to `integrate`, resolve every
reported blocker, then advance to `learn`. Finish only after learnings and
integration state are clean:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" lifecycle complete \
  --root <root> --actor "<portfolio supervisor>"
```

## Cross-Project Convergence

Read [convergence.md](references/convergence.md). Before a child reports
completion, it renders `assets/templates/child-result.yaml` and submits it:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" result submit \
  --root <root> --file <child-result.yaml>
```

After all expected results arrive, compute graph impact and integration status:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" convergence check \
  --root <root> [--intent <intent>]
```

Missing results and dependency-order risks require remediation. For an eligible
breaking, deferred, or unknown risk, obtain an explicit human decision and
bind it to the current risk revision:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" convergence decide \
  --root <root> --id <risk-id> --decision accepted \
  --accepted-by "<human decision maker>" --note "<decision rationale>"
```

Use `resolved` only when the human confirms the reported risk is resolved.
Re-run `convergence check`; changed evidence invalidates stale decisions.

## Human Question Relay

When a child AI-DLC stage asks for human judgment, the runner writes the
generated questions to Markdown using `[Answer]:` markers and submits the
unanswered file:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" question submit \
  --root <root> --id <packet-id> --project <project> --intent <intent> \
  --stage <stage> --file <questions.md>
```

Submission rejects any pre-filled answer and moves the session to `waiting`.
The runner reports the packet and stops. It must never invoke `question answer`.

The supervisor offers the user Guide, Edit Markdown, or Chat. Write each human
answer verbatim on its `[Answer]:` line without changing generated question
text, then resolve the packet:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" question answer \
  --root <root> --id <packet-id> --file <answered.md> \
  --mode guided --answered-by "<human decision maker>"
```

The command rejects changed question text, missing answers, invalid modes, and
repeat answers. It preserves the answered Markdown byte-for-byte, records the
human provenance, and returns the session to `active`. Use `question list` to
verify status before asking the runner to resume.

## Shared Memory

Read [shared-memory.md](references/shared-memory.md) before processing child
AI-DLC learnings. `dispatch check` returns canonical baseline revisions for
`project.md` and `team.md`; include them in the runner assignment.

Child runners submit structured proposals from
`assets/templates/learning-proposal.yaml`:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" learning propose \
  --root <root> --file <proposal.yaml>
```

The supervisor lists and reviews proposals. Approval performs the canonical
write under the portfolio lock:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" learning list \
  --root <root> --project <project> --status pending
bun "$SKILL_DIR/scripts/portfolio.ts" learning approve \
  --root <root> --id <proposal>
```

If approval reports a stale base, inspect the latest canonical rule set and
resolve duplicates or contradictions. Record that human judgment before
retrying:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" learning reconcile \
  --root <root> --id <proposal> --note "<review outcome>"
```

Use `memory refresh` at safe stage boundaries when the child needs current
canonical context. Before completing a child session, inspect and clean both
memory destinations. Both mutations require the exact worktree revision
returned by inspect:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" memory inspect \
  --root <root> --project <project> --intent <intent> --destination project
bun "$SKILL_DIR/scripts/portfolio.ts" memory clean \
  --root <root> --project <project> --intent <intent> --destination project \
  --expected-worktree-revision <sha256>
```

Repeat for `team`. `session update --status completed` refuses shared-memory
changes remaining in the feature worktree.

## Recover And Report

Run `status` after startup or callback delivery:

```bash
bun "$SKILL_DIR/scripts/portfolio.ts" status --root <root>
```

Reconcile terminal loss against durable child AI-DLC state. Relaunch a runner
against the same registered worktree and child intent; never create replacement
workflow state.
