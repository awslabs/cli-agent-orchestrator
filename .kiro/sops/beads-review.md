# Beads Review

## Overview

This SOP guides interactive review and refinement of beads issues. It ensures beads are clear, complete, and properly organized before work begins.

## Parameters

- **epic_name** (required): Name of the epic to scope review
- **project_dir** (required): Directory containing PDD artifacts and implementation plan
- **scope** (optional, default: "all"): Review scope
  - "all": Review all open beads in epic
  - "ready": Review only ready tasks
  - "blocked": Review blocked tasks and dependencies
- **focus** (optional): Specific bead ID to focus review on

## Steps

### 1. Gather Project Context

Read all files in the project directory to understand the project before reviewing beads.

**Constraints:**
- You MUST read all files in `project_dir` before proceeding
- You MUST use this context to compare beads against project requirements and design

### 2. Inventory Current State

Get overview of all beads in the epic and their status.

**Constraints:**
- You MUST run `bd list --json` and filter to epic's children
- You MUST identify beads by status (open, in_progress, blocked)
- You MUST note any beads without descriptions or acceptance criteria
- You MUST present summary to user before proceeding
- You MUST show epic context: "Reviewing beads for epic: <epic_name>"

### 3. Review Individual Beads

Examine each bead in the epic for clarity and completeness.

**Constraints:**
- You MUST run `bd show <id>` for each bead being reviewed
- You MUST check that each bead has:
  - Clear, actionable title
  - Description with context
  - Acceptance criteria (if applicable)
  - Notes with implementation guidance
  - Appropriate priority (P0-P4)
- You MUST flag beads that need improvement
- You MUST ask user for input on unclear beads

### 4. Refine Beads

Modify beads to improve clarity and completeness.

**Constraints:**
- You MUST use `bd edit <id>` to update bead content
- You MUST use `bd edit <id> --acceptance` for acceptance criteria
- You MUST use `bd edit <id> --notes` for implementation notes
- You MUST NOT change bead scope without user approval
- You MUST preserve existing context when editing
- You SHOULD add acceptance criteria to beads that lack them

### 5. Verify Dependencies

Ensure dependency graph is correct and complete within the epic.

**Constraints:**
- You MUST check that blocking relationships are accurate
- You MUST identify circular dependencies and resolve them
- You MUST use `bd dep add <child> <parent>` to add missing dependencies
- You MUST verify `bd ready` shows expected available tasks

### 6. Report Summary

Provide summary of review and changes made.

**Constraints:**
- You MUST list all changes made during review
- You MUST show updated bead list for the epic
- You MUST highlight any remaining issues
- You MUST suggest next steps (e.g., run `ralph-orchestrator run` to start work)

## Commands Reference

| Command | Purpose |
|---------|---------|
| `bd list` | View all beads |
| `bd list --json` | View all beads as JSON |
| `bd show <id>` | View bead details |
| `bd edit <id>` | Edit a bead |
| `bd edit <id> --acceptance "<text>"` | Edit acceptance criteria |
| `bd edit <id> --notes "<text>"` | Edit implementation notes |
| `bd close <id>` | Close a bead |
| `bd create "<title>" -t task --parent <epic-id>` | Create a new task |
| `bd dep add <child> <parent>` | Add dependency |
| `bd ready` | List tasks with no blockers |

## Troubleshooting

### Circular Dependencies
- Identify the cycle using `bd show` on involved beads
- Determine which dependency should be removed
- Use `bd dep remove <child> <parent>` to break the cycle

### Missing Context
- Ask user for clarification on unclear beads
- Reference design documents for architectural context
- Add context to bead description via `bd edit`
