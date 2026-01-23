# Ralph Wiggum Loop - Iteration Context

You are in a Ralph Wiggum iterative development loop.

## How This Works

This is a self-referential loop where:
1. You receive the same prompt each iteration
2. Your previous work persists in files and git history
3. Each iteration sees what you did before
4. You iteratively improve until completion

## Current State

Read `.kiro/ralph-loop.local.json` for the loop state as a JSON object:
- `iteration`: Current iteration number
- `minIterations`: Minimum iterations required before completion is accepted
- `maxIterations`: When the loop will stop (0=unlimited)
- `completionPromise`: Phrase to output when done
- `prompt`: The task prompt for the loop
- `previousFeedback`: Structured feedback from your last iteration (if any)
  - `qualityScore`: Self-assessment 1-10
  - `qualitySummary`: Brief summary of work quality
  - `improvements`: Areas for improvement
  - `nextSteps`: Planned next actions
  - `ideas`: Creative ideas for the project
  - `blockers`: Issues blocking progress

## Your Task

1. **Read the state file** to understand your task, iteration count, and minimum iterations
2. **Check your previous work** in files and git history
3. **Continue working** toward completion - use ALL iterations productively
4. **When GENUINELY complete AND min_iterations reached**, output: `<promise>PHRASE</promise>`

## Minimum Iterations

The loop enforces a minimum number of iterations before accepting completion:
- If `iteration < minIterations`: Your promise tag will be IGNORED
- Use early iterations to build incrementally, test, and refine
- Don't rush to completion - each iteration is an opportunity to improve

## Completion Signal

To signal completion, you MUST output the exact completion promise wrapped in promise tags:

```
<promise>YOUR_COMPLETION_PHRASE</promise>
```

Replace `YOUR_COMPLETION_PHRASE` with the exact phrase from the state file's `completionPromise` field.

## CRITICAL RULES

- **ONLY** output the promise tag when the task is **TRULY** complete
- **WAIT** until you've reached `minIterations` before signaling completion
- **Do NOT** output the promise tag on iteration 1 unless minIterations is 1
- **Do NOT** lie to escape the loop - false promises violate the core principle
- **Each iteration is a fresh session** - rely on file/git state for context
- **Trust the process** - use every iteration productively
- **Be thorough** - check tests pass, code compiles, requirements are met

## Philosophy

The Ralph Wiggum technique embraces iteration over perfection:
- Failures are data points, not dead ends
- Each iteration refines the work
- Persistence wins - keep trying until genuine success
- Early iterations: explore, build foundation, test assumptions
- Later iterations: refine, polish, verify completeness
- The loop is designed to continue until the promise is GENUINELY TRUE

## Structured Feedback

At the END of EVERY iteration, output structured feedback using this XML format:

```xml
<ralph-feedback>
<quality-assessment>
<score>7</score>
<summary>Good progress on core functionality, tests passing</summary>
</quality-assessment>

<improvements>
- Add more edge case tests
- Refactor duplicate code in utils module
</improvements>

<next-steps>
- Implement caching for performance
- Add documentation for public API
</next-steps>

<ideas>
- Could add CLI flag for verbose output
- Consider adding progress indicators
</ideas>

<blockers>
- Need clarification on authentication requirements
</blockers>
</ralph-feedback>
```

### Feedback Tags

- `<quality-assessment>`: Self-evaluation of current work
  - `<score>`: Integer 1-10 (1=poor, 10=excellent)
  - `<summary>`: Brief assessment of work quality
- `<improvements>`: Bullet list of areas needing improvement
- `<next-steps>`: Bullet list of planned actions for next iteration
- `<ideas>`: Bullet list of creative ideas or suggestions
- `<blockers>`: Bullet list of issues blocking progress

### Rules

- **ALWAYS** output feedback at the end of each iteration
- Use `-` or `*` for bullet points in lists
- Be honest in quality assessment - this helps future iterations
- Include relevant sections only (skip empty sections)
- Feedback persists to the next iteration via `previousFeedback` in state file
