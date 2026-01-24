# Code Task Generator (Beads)

## Overview

This SOP generates structured code tasks from PDD implementation plans and stores them as beads issues. It processes the implementation plan to create properly formatted beads with dependencies, enabling parallel agent coordination through the beads issue tracker.

**This SOP runs autonomously without user interaction.**

## Parameters
- **epic_name** (required): Name for the parent epic
- **project_dir** (required): Directory containing PDD artifacts

## Steps

### 1. Analyze Input

Parse and understand the PDD implementation plan.

**Constraints:**
- You MUST read the plan file and verify it follows PDD format with numbered steps
- You MUST read the design file for technical context
- You MUST parse implementation plan and extract steps/checklist status
- You MUST identify the core functionality being requested in each step
- You MUST extract any technical requirements, constraints, or preferences mentioned
- You MUST determine the appropriate complexity level (Low/Medium/High) for each task
- You MUST check for existing beads with `bd --no-daemon list` to avoid duplicates

### 2. Structure Requirements

Organize requirements and determine task breakdown.

**Constraints:**
- You MUST extract from each step:
  - Title (step title)
  - Description (objective/what needs to be implemented)
  - Demo requirements (for acceptance criteria)
  - Constraints and technical requirements
  - Integration notes with previous steps
- You MUST identify which specific research documents (if any) are directly relevant to each task
- You MUST create measurable acceptance criteria using Given-When-Then format
- You MUST identify dependencies between tasks (blocking relationships)

### 3. Create Epic

Create the parent epic for all tasks.

**Constraints:**
- You MUST create epic first: `bd --no-daemon create "<epic_name>" -t epic -p 2`
- You MUST capture the epic ID for use as parent

### 4. Generate Task Beads

Create beads issues for each identified task following the task format specification.

**Constraints:**
- You MUST analyze content to identify logical sub-tasks for implementation within each step
- You MUST break down steps into logical implementation phases focusing on functional components, NOT separate testing tasks
- You MUST use `bd --no-daemon create "<title>" -t task --parent <epic-id>` for each task
- You MUST include description using `-d` flag with:
  - Clear description of what needs to be implemented and why
  - Background context needed to understand the task
  - Reference to design document path
- You MUST use `bd --no-daemon edit <id> --acceptance "<criteria>"` with Given-When-Then format acceptance criteria
- You MUST include unit test requirements as part of the acceptance criteria for each implementation task
- You MUST NOT create separate tasks for "add unit tests" or "write tests"
- You MUST use `bd --no-daemon edit <id> --notes "<notes>"` with:
  - Technical requirements
  - Implementation approach
  - Dependencies on other components
  - Complexity assessment and required skills
- You MUST set dependencies using `bd --no-daemon dep add <child> <parent>` for blocking relationships
- You MUST NOT create duplicate issues for already-existing beads
- You MUST always use `--no-daemon` flag with bd commands

### 5. Report Results

Confirm issue creation and provide summary.

**Constraints:**
- You MUST run `bd --no-daemon list` to verify all issues were created
- You MUST display created issues with their IDs
- You MUST show dependency graph if dependencies were created
- You MUST list all generated beads with their titles
- You MUST provide step demo requirements for context
- You MUST suggest running ralph loop to implement tasks in sequence

## Task Format Specification

Each bead task MUST contain the following information distributed across beads fields:

### Description Field (-d flag)
```
[Clear description of what needs to be implemented and why]

## Background
[Relevant context and background information]

## Reference Documentation
Required: {project_dir}/design/detailed-design.md

Note: Read the detailed design document before beginning implementation.
```

### Acceptance Criteria Field (--acceptance)
```
1. [Criterion Name]
   - Given [precondition]
   - When [action]
   - Then [expected result]

2. [Another Criterion]
   - Given [precondition]
   - When [action]
   - Then [expected result]

Unit Test Requirements:
- [Test scenario 1]
- [Test scenario 2]

Demo: [Demo description from step]
```

### Notes Field (--notes)
```
## Technical Requirements
1. [First requirement]
2. [Second requirement]

## Dependencies
- [Component or task dependency]

## Implementation Approach
1. [First implementation step]
2. [Second implementation step]

## Metadata
- Complexity: [Low/Medium/High]
- Required Skills: [Skills needed]
```

## Field Mapping Reference

| Plan Field | Beads Field | CLI Method |
|------------|-------------|------------|
| Step title | Title | positional arg |
| Objective + Background | Description | `-d` flag |
| Test requirements + Demo | Acceptance criteria | `bd edit <id> --acceptance` |
| Technical requirements + Implementation approach | Notes | `bd edit <id> --notes` |
| Step dependencies | Blocking relationships | `bd dep add <child> <parent>` |

## Commands Reference

| Command | Purpose |
|---------|---------|
| `bd --no-daemon create "<title>" -t epic -p 2` | Create parent epic |
| `bd --no-daemon create "<title>" -t task --parent <epic-id>` | Create task under epic |
| `bd --no-daemon edit <id> --acceptance "<text>"` | Set acceptance criteria |
| `bd --no-daemon edit <id> --notes "<text>"` | Set implementation notes |
| `bd --no-daemon dep add <child> <parent>` | Add blocking dependency |
| `bd --no-daemon list` | View all beads |
| `bd --no-daemon show <id>` | View bead details |
| `bd --no-daemon ready` | List tasks with no open blockers |

## Example

### Input
```
epic_name: my-feature
project_dir: .sop/my-feature
```

### Expected Output
```
Reading plan and design files...

Creating epic: my-feature
- my-feature-a1b2: my-feature (epic)

Creating tasks for Step 1:
- my-feature-c3d4: Create data models (task)
  Dependencies: none
- my-feature-e5f6: Implement validation (task)
  Dependencies: my-feature-c3d4

Creating tasks for Step 2:
- my-feature-g7h8: Add API endpoints (task)
  Dependencies: my-feature-e5f6

Created 1 epic and 3 tasks.

Next steps: Run `ralph-orchestrator run` to implement tasks in sequence.

Step 1 demo: Working data models with validation
Step 2 demo: API endpoints accepting validated data
```

## Troubleshooting

### Plan File Not Found
If the specified plan file doesn't exist:
- Check if the path is a directory and look for plan.md within it
- Suggest common locations where PDD plans might be stored
- Validate the file path format and suggest corrections

### Invalid Plan Format
If the plan doesn't follow expected PDD format:
- Identify what sections are missing or malformed
- Attempt to extract what information is available
- Report specific parsing issues

### Duplicate Beads
If beads already exist for the epic:
- Check existing beads with `bd --no-daemon list`
- Skip creation of duplicate tasks
- Report which tasks were skipped
