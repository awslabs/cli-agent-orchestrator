# AI-DLC Child Assignment

Parent portfolio: `{{portfolio_id}}`
Portfolio root: `{{absolute_portfolio_root}}`
Parent intent: `{{parent_intent_id}}`
Project: `{{project_id}}`
Child AI-DLC intent: `{{child_intent_id}}`
AI-DLC space: `{{aidlc_space}}`
Worktree: `{{absolute_worktree_path}}`
Branch: `{{branch}}`
Canonical project memory revision: `{{project_memory_revision}}`
Canonical team memory revision: `{{team_memory_revision}}`
Harness manifest revision: `{{harness_manifest_revision}}`

## Objective

{{objective}}

## Relevant Context

{{business_component_and_dependency_slice}}

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
Submit durable shared-memory learnings as portfolio proposals. Do not approve
or directly merge `project.md` or `team.md` changes.

For human decision questions, submit the unchanged unanswered Markdown as a
portfolio question packet, report its ID and path, and wait. Never answer a
generated question or invoke `question answer`.
