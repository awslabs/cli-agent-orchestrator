# Code Assist (Ralph Loop)

## Overview

This SOP guides the implementation of code tasks using test-driven development principles, following a structured Explore, Plan, Code, Commit workflow. It balances automation with user collaboration while adhering to existing package patterns and prioritizing readability and extensibility.

The agent acts as a Technical Implementation Partner and TDD Coach - providing guidance, generating test cases and implementation code that follows existing patterns, avoids over-engineering, and produces idiomatic, modern code in the target language.

**Ralph Loop Context:** This SOP runs within an automated Ralph loop. Tasks are fetched from the beads tracker rather than provided directly. The agent operates autonomously without user interaction.

## Parameters

- **epic_name** (required): Name of the epic to scope task selection from beads
- **repo_root** (optional, default: current working directory): The root directory of the repository for code implementation

## Mode Behavior

This SOP operates in **auto mode** exclusively within the Ralph loop:

- Execute all actions autonomously without user confirmation
- Document all decisions, assumptions, and reasoning in beads comments
- When multiple approaches exist, select the most appropriate and document why
- Provide comprehensive summaries at completion

## State Management

Beads is the **single source of truth** for all task state. No separate documentation files are created.

| State Type | Storage Location | Command |
|------------|------------------|---------|
| Task status | Issue status | `bd update <id> --status <status>` |
| Static context (patterns, architecture, deps) | Issue notes field | `bd update <id> --notes "<context>"` |
| Progressive updates (phase, decisions, results) | Issue comments | `bd comments add <id> "<update>"` |
| Full state retrieval | JSON export | `bd show <id> --json` |

### Progress Comment Format

Use structured JSON for machine-parseable progress tracking:
```json
{"phase":"explore","step":"3.1","summary":"Analyzing requirements","findings":["..."]}
{"phase":"code","step":"5.2","tests":{"written":5,"passing":3},"decision":"Using factory pattern"}
{"phase":"commit","hash":"abc123","files":["src/foo.ts","tests/foo.test.ts"]}
```

### Resuming from Previous State

On task resume, parse the last comment to determine current phase and step:
```bash
bd show <id> --json | jq '.comments[-1].text | fromjson'
```

## Important Notes

**CODEASSIST.md Integration:** If CODEASSIST.md exists in repo_root, it contains additional constraints, pre/post SOP instructions, examples, and troubleshooting specific to this project. Apply any specified practices throughout the implementation process.

## Steps

### 1. Task Selection

Identify the next task to work on from beads, scoped to the epic.

**Constraints:**
- You MUST first find the epic ID: `bd --no-daemon list --type epic --json` and filter by epic_name
- You MUST run `bd --no-daemon ready --parent <epic_id>` to find tasks with no open blockers with the epic as parent
- You MUST select ONE task to work on per iteration
- You MUST run `bd --no-daemon update <id> --status in_progress` to claim the task
- If no tasks are ready, check `bd --no-daemon list` for blocked tasks and resolve blockers
- Check previousFeedback for relevant context from prior iterations

### 2. Setup

Initialize the project environment and check for existing progress.

**Constraints:**
- You MUST check for existing progress: `bd --no-daemon show <id> --json`
  - If comments exist, parse the last comment to determine resume point
  - Resume from the phase/step indicated in the last progress comment
- You MUST discover existing instruction files using: `find . -maxdepth 3 -type f \( -path "*/node_modules/*" -o -path "*/build/*" -o -path "*/.venv/*" -o -path "*/venv/*" -o -path "*/__pycache__/*" -o -path "*/.git/*" -o -path "*/dist/*" -o -path "*/target/*" \) -prune -o -name "*.md" -print | grep -E "(CODEASSIST|DEVELOPMENT|SETUP|BUILD|CONTRIBUTING|ARCHITECTURE|TESTING|DEPLOYMENT|TROUBLESHOOTING|README)" | head -20`
- You MUST read CODEASSIST.md if found and apply its constraints throughout (see Important Notes)
- You MUST log setup completion: `bd comments add <id> '{"phase":"setup","codeassist_found":<bool>,"instruction_files":[...]}'`

### 3. Explore Phase

#### 3.1 Analyze Requirements and Context

Analyze the task description and existing documentation to identify core functionality, edge cases, and constraints.

**Constraints:**
- You MUST read the task description from `bd --no-daemon show <id>`
- You MUST read acceptance criteria and notes for full context
- You MUST create a clear list of functional requirements and acceptance criteria
- You MUST determine the appropriate file paths and programming language
- You MUST align with the existing project structure and technology stack
- You MUST identify potential gaps or inconsistencies in requirements
- You MUST log findings: `bd comments add <id> '{"phase":"explore","step":"3.1","requirements":[...],"acceptance_criteria":[...],"language":"...","target_paths":[...]}'`

#### 3.2 Research Existing Patterns

Search for similar implementations and identify interfaces, libraries, and components the implementation will interact with.

**Constraints:**
- You MUST search the current repository for relevant code, patterns, and information related to the coding task
- You MAY use available tools to search code repositories, read documentation, and gather relevant information
- You MUST create a dependency map showing how the new code will integrate
- You MUST store static context in notes field: `bd update <id> --notes "<patterns, dependencies, architecture, integration points>"`
- You MUST log completion: `bd comments add <id> '{"phase":"explore","step":"3.2","patterns":[...],"dependencies":[...],"integration_points":[...]}'`

#### 3.3 Validate Understanding

Confirm understanding of requirements before proceeding to planning.

**Constraints:**
- You MUST summarize your interpretation of what the task requires
- You MUST identify any assumptions being made
- You MUST log interpretation: `bd comments add <id> '{"phase":"explore","step":"3.3","interpretation":"...","assumptions":[...]}'`

### 4. Plan Phase

#### 4.1 Design Test Strategy

Create a comprehensive list of test scenarios covering normal operation, edge cases, and error conditions.

**Constraints:**
- You MUST cover all acceptance criteria with at least one test scenario
- You MUST define explicit input/output pairs for each test case
- You MUST design tests that will initially fail when run against non-existent implementations
- You MUST NOT create mock implementations during the test design phase
- You MUST include tests for:
  - Normal/happy path operations
  - Edge cases and boundary conditions
  - Error conditions and invalid inputs
- You MUST log test strategy: `bd comments add <id> '{"phase":"plan","step":"4.1","test_scenarios":[{"name":"...","type":"unit|integration","input":"...","expected":"...","covers_ac":"..."}]}'`

#### 4.2 Implementation Planning

Outline the high-level structure of the implementation.

**Constraints:**
- You MUST identify all files to be created or modified
- You MUST outline the implementation approach and key components
- You MUST identify any shared utilities or helpers needed
- You MUST log implementation plan: `bd comments add <id> '{"phase":"plan","step":"4.2","files_to_create":[...],"files_to_modify":[...],"impl_tasks":["task1","task2","task3"]}'`

### 5. Code Phase

#### 5.1 Implement Test Cases

Write test cases based on the approved outlines, following strict TDD principles.

**Constraints:**
- You MUST save test implementations to the appropriate test directories in repo_root
- You MUST implement tests for ALL requirements before writing ANY implementation code
- You MUST follow the testing framework conventions used in the existing codebase
- You MUST execute tests after writing them to verify they fail as expected
- You MUST log test implementation: `bd comments add <id> '{"phase":"code","step":"5.1","tests_written":<n>,"test_files":[...],"all_failing":true,"failure_reasons":[...]}'`
- You MUST follow the Build Output Management practices defined in the Best Practices section

#### 5.2 Develop Implementation Code

Write implementation code to pass the tests, focusing on simplicity and correctness first.

**Constraints:**
- You MUST follow the strict TDD cycle: RED → GREEN → REFACTOR
- You MUST implement only what is needed to make the current test(s) pass
- You MUST follow the coding style and conventions of the existing codebase
- You MUST ensure all implementation code is written directly in the repo_root directories
- You MUST follow YAGNI, KISS, and SOLID principles
- You MUST execute tests after each implementation step to verify they now pass
- You MUST log progress after each significant step: `bd comments add <id> '{"phase":"code","step":"5.2","tests":{"passing":<n>,"total":<m>},"impl_files":[...]}'`
- You MUST follow the Build Output Management practices defined in the Best Practices section

#### 5.3 Refactor and Optimize

Review the implementation to identify opportunities for simplification, improvement, and coding convention alignment.

**Constraints:**
- You MUST check that all tasks are complete before proceeding
  - if tests fail, you MUST identify the issue and propose an implementation
  - if builds fail, you MUST identify the issue and propose an implementation
  - if implementation tasks are incomplete, you MUST identify the issue and propose an implementation
- You MUST examine the code around the changes made to determine if updates match existing coding conventions
- You MUST refactor the implementation to align with identified coding conventions from the surrounding codebase
- You MUST prioritize readability and maintainability over clever optimizations
- You MUST maintain test passing status throughout refactoring
- You MUST log refactoring: `bd comments add <id> '{"phase":"code","step":"5.3","refactored":true,"changes":[...]}'`

#### 5.4 Validate Implementation

Verify the implementation meets all requirements and follows established patterns.

**Constraints:**
- You MUST check that all tasks are complete before proceeding
- You MUST address any discrepancies between requirements and implementation
- You MUST execute the relevant test command and verify all implemented tests pass successfully
- You MUST execute the relevant build command and verify builds succeed
- You MUST ensure code coverage meets the requirements for the project
- You MUST verify all planned implementation tasks have been completed
- You MUST provide the complete test execution output
- You MUST NOT claim implementation is complete if any tests are failing
- You MUST log validation: `bd comments add <id> '{"phase":"code","step":"5.4","tests_pass":true,"build_pass":true,"coverage":"...%"}'`

### 6. Commit Phase

If all tests are passing, draft a conventional commit message and perform the actual git commit.

**Constraints:**
- You MUST check that all tasks are complete before proceeding
- You MUST NOT commit changes until builds AND tests have been verified
- You MUST follow the Conventional Commits specification
- You MUST use git status to check which files have been modified
- You MUST use git add to stage all relevant files
- You MUST execute the git commit command with the prepared commit message
- You MUST NOT push changes to remote repositories
- You MUST log commit: `bd comments add <id> '{"phase":"commit","hash":"<hash>","message":"<msg>","files":[...]}'`
- You SHOULD include the "🤖 Assisted by the code-assist SOP" footer

### Git Commits in Multi-Package Workspaces

A workspace may contain multiple packages, each with its own git repository. You MUST commit changes in each package's directory separately.

**Identifying Modified Packages:**
```bash
for pkg in */; do
  if [ -d "$pkg/.git" ]; then
    (cd "$pkg" && git status --porcelain) | grep -q . && echo "Changes in: $pkg"
  fi
done
```

**Committing Per Package:**
```bash
cd <package-directory>
git add -A ':!.beads' ':!.kiro/ralph-loop.local.json' ':!__pycache__' ':!.pytest_cache' ':!.sop'
git commit -m "<task-id>: <brief description>"
```

**Logging Multi-Package Commits:**
```json
{"phase":"commit","packages":[{"name":"pkg1","hash":"abc123"},{"name":"pkg2","hash":"def456"}],"message":"..."}
```

### 7. Close Bead and Wrap-up

Close the completed task in beads and prepare for next iteration.

**Constraints:**
- You MUST run `bd --no-daemon close <id> --reason "Completed: <summary>"` only when truly done
- If the completed task has a parent task:
  - Check if all sibling subtasks are now closed: `bd --no-daemon list --parent <parent_id> --json`
  - If this was the last open subtask, reopen the parent: `bd --no-daemon update <parent_id> --status open`
  - This allows a future iteration to verify the parent task is fully accomplished or needs additional work
- If more tasks remain in epic: Document progress and next steps
- If all epic tasks complete: Output `<promise>EPIC_COMPLETE</promise>`
- You MUST be honest in quality assessment

## Beads Commands Reference

| Command | Purpose |
|---------|---------|
| `bd --no-daemon ready --parent <id>` | List tasks with no open blockers under epic |
| `bd --no-daemon show <id>` | View task details |
| `bd --no-daemon show <id> --json` | View task with all comments as JSON |
| `bd --no-daemon update <id> --status <status>` | Update task status |
| `bd --no-daemon update <id> --notes "<text>"` | Set/replace static context |
| `bd --no-daemon comments add <id> "<text>"` | Append progress update |
| `bd --no-daemon close <id> --reason "<reason>"` | Close completed task |
| `bd --no-daemon list` | View all beads |
| `bd --no-daemon list --json` | View all beads as JSON |
| `bd --no-daemon create "<title>" --parent <id>` | Create subtask |

## Desired Outcome

* A complete, well-tested code implementation that meets the specified requirements
* A comprehensive test suite that validates the implementation
* Clean, documented code that:
  * Follows existing package patterns and conventions
  * Prioritizes readability and extensibility
  * Avoids over-engineering and over-abstraction
  * Is idiomatic and modern in the implementation language
* Complete audit trail of decisions and progress in beads comments
* Properly committed changes with conventional commit messages
* Closed bead with completion reason

## Troubleshooting

### Build Issues
If builds fail during implementation:
- Follow build instructions from CODEASSIST.md if available
- Verify you're in the correct directory for the build system
- Try clean builds before rebuilding when encountering issues
- Check for missing dependencies and resolve them
- Log failures: `bd comments add <id> '{"phase":"code","build_failure":true,"error":"...","attempt":<n>}'`

### Implementation Challenges
If the implementation encounters unexpected challenges:
- Log the challenge: `bd comments add <id> '{"phase":"code","challenge":"...","alternatives":[...],"selected":"...","reason":"..."}'`
- Select the most promising alternative and proceed

### Failed Implementation
If the first implementation attempt fails (tests don't pass, build fails, or approach doesn't work), the task is too ambiguous:
1. Log the failure: `bd comments add <id> '{"phase":"code","failed":true,"issue":"...","tried":"..."}'`
2. Decompose into smaller, clearer subtasks:
   ```bash
   bd --no-daemon create "Investigate: <specific issue>" --parent <id> --description "<what failed, what was tried>"
   bd --no-daemon create "Implement: <narrower scope>" --parent <id> --description "<reduced scope>"
   ```
3. Revert uncommitted changes: `git checkout -- . && git clean -fd`
4. Block the parent: `bd --no-daemon update <id> --status blocked`
5. The next iteration picks up a ready subtask with fresh context

### Task Too Large for Single Iteration
If the task scope is too large to complete within the current context window:

1. **Save Progress:**
   - Log current state: `bd comments add <id> '{"phase":"...","partial":true,"completed":[...],"remaining":[...],"findings":"..."}'`
   - Update notes with any discovered patterns/context: `bd update <id> --notes "<updated context>"`

2. **Decompose Task:**
   - Break the remaining work into smaller subtasks
   - Create subtasks in beads as children of the current task:
     ```bash
     bd --no-daemon create "<subtask title>" --parent <current_task_id> --description "<detailed context>"
     ```
   - Include sufficient context in each subtask description so future iterations can proceed independently
   - Ensure subtasks are ordered with dependencies if needed

3. **Clean Up Code Changes:**
   - Revert any uncommitted code changes to return to pre-task state:
     ```bash
     git checkout -- .
     git clean -fd
     ```

4. **Update Task Status:**
   - Update the current task to blocked status:
     ```bash
     bd --no-daemon update <id> --status blocked
     bd --no-daemon comments add <id> '{"phase":"decomposed","subtasks_created":[...]}'
     ```

5. **End Iteration:**
   - Output summary of decomposition and end the iteration
   - The next iteration will pick up a ready subtask

## Best Practices

### Project Detection and Configuration
- Detect project type by examining files (pyproject.toml, build.gradle, package.json, etc.)
- Check for CODEASSIST.md for additional SOP constraints
- Use project-appropriate build commands

### Build Output Management
- Pipe build output to temporary files: `[build-command] > /tmp/build_output.log 2>&1`
- Search for specific success/failure indicators instead of displaying full output
- Include relevant error snippets in beads comments when logging failures

### Context Window Management
- When reading source files, prefer targeted searches over full file reads
- Limit file reads to relevant sections when possible
- Use grep/search tools to locate specific code before reading

## Invariants

These conditions must always be true:

- Beads is the single source of truth for all task state
- Tests fail before implementation, pass after (TDD)
- All acceptance criteria have corresponding tests
- No code files in documentation directories
- Comments form an append-only audit trail
- Build and tests pass before any commit
