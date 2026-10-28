# AI-DLC Child Assignment

Parent portfolio: `{{portfolio_id}}`
Portfolio root: `{{absolute_portfolio_root}}`
Parent intent: `{{parent_intent_id}}`
Project: `{{project_id}}`
Child AI-DLC intent: `{{child_intent_id}}`
AI-DLC space: `{{aidlc_space}}`
Worktree: `{{absolute_worktree_path}}`
Branch: `{{branch}}`
Harness manifest revision: `{{harness_manifest_revision}}`

## Objective

{{objective}}

## Relevant Context

{{business_component_and_dependency_slice}}

{{promoted_learnings}}

## Contract Inputs

{{contract_inputs}}

## Expected Reporting

Run the existing AI-DLC workflow in the assigned worktree. Report approval
requests, blockers, contract-change proposals, current stage, changed files,
verification, and final outcomes to terminal `{{callback_terminal_id}}`.

Before reporting completion, render `child-result.yaml` and submit it with
`result submit`. Include every changed component and business capability,
every relevant dependency assumption, all contract compatibility findings,
and concrete verification evidence. Report the stored result path to the
portfolio supervisor.

Do not operate outside the assigned worktree or edit AI-DLC state directly.
Treat worktree-local `project.md`/`team.md` as scratch. Submit durable
learnings as portfolio proposals (`learning propose`); only the supervisor
reviews and promotes a proposal into CAO's native memory. Promoted portfolio
learnings reach you in Relevant Context above, not through your own
`memory_recall`, which reads your repository's project scope.

For human decision questions, submit the unchanged unanswered Markdown as a
portfolio question packet, report its ID and path, and wait. Never answer a
generated question or invoke `question answer`.
