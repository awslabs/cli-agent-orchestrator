# PRD: CAO Dashboard Bug Fixes and Enhancements

## Overview
This PRD covers 10 bug fixes and enhancements for the CAO (CLI Agent Orchestrator) React dashboard.

---

## Bug #1: Auto-Mode Immediate Bead Assignment

### Problem
When clicking "Auto" on a WAITING/IDLE agent with no bead assigned, nothing happens immediately. User must wait for next state change.

### Expected Behavior
1. Click "Auto" on WAITING_INPUT or IDLE session
2. Immediately find highest priority unassigned bead (OPEN or IN_PROGRESS status)
3. Assign bead to session via API
4. Send task prompt to agent via `api.sessions.input()`

### Technical Implementation
**File:** `web/src/components/AgentPanel.tsx`

- Extract bead assignment into `tryAssignBead(sessionId)` function
- Call `tryAssignBead()` immediately when Auto is toggled ON
- Existing useEffect continues monitoring for ongoing auto-mode

### Acceptance Criteria
- [ ] Clicking Auto immediately assigns highest priority bead
- [ ] Considers both OPEN and IN_PROGRESS beads
- [ ] Task prompt sent to agent immediately
- [ ] If no beads available, auto-mode stays enabled for future beads

---

## Bug #2: Bead Unassign on Session Close/Delete

### Problem
When an agent session is closed or deleted, assigned beads remain assigned to the non-existent session.

### Expected Behavior
1. Session is closed/deleted
2. Any bead assigned to that session becomes unassigned
3. Bead keeps its current status (e.g., stays IN_PROGRESS)

### Technical Implementation
**Files:** 
- `web/src/components/AgentPanel.tsx` - call unassign on close
- `src/cli_agent_orchestrator/api/web.py` - endpoint to unassign bead by session

- When closing session, find beads with `assigned_session === sessionId`
- Update each bead: `assigned_session = null` (keep status unchanged)
- Already have `clear_bead_position()` in v2.py - extend or use similar pattern

### Acceptance Criteria
- [ ] Closing session unassigns all beads from that session
- [ ] Bead status remains unchanged (IN_PROGRESS stays IN_PROGRESS)
- [ ] Deleting session also unassigns beads

---

## Bug #3: AI-Powered Bead Creation Textbox

### Problem
No way to quickly create multiple beads from a planning document or task list.

### Expected Behavior
1. Textbox above BeadsPanel
2. User types free-form text (requirements, task list, etc.)
3. Click "Generate Beads" button
4. Kiro AI parses text into structured beads with title, description, priority
5. Beads are created via API

### Technical Implementation
**File:** `web/src/components/BeadsPanel.tsx`

- Add textarea + "Generate Beads" button above bead list
- On submit, call Kiro CLI or use existing agent session to parse
- Parse response into bead objects: `{ title, description, priority, status: 'OPEN' }`
- Create each bead via `api.tasks.create()`

### Acceptance Criteria
- [ ] Textbox visible above beads section
- [ ] Submitting text generates structured beads via AI
- [ ] Each generated bead has title, description, priority
- [ ] Beads appear in list after generation

---

## Bug #4: Context Monitor on Agents

### Problem
No visibility into how much context an agent has consumed.

### Expected Behavior
1. Each session card shows context usage
2. Display format: "45k / 200k tokens" or similar
3. Pull from Kiro CLI if possible, otherwise estimate

### Technical Implementation
**File:** `web/src/components/AgentPanel.tsx`

- Try to read context from Kiro CLI session data (check SQLite or API)
- Fallback: estimate from terminal output length
- Display in session card UI

### Acceptance Criteria
- [ ] Context usage visible per session
- [ ] Shows used tokens/characters
- [ ] Updates as agent works

---

## Bug #5: Display Model Per Session

### Problem
No visibility into which AI model each agent session is using.

### Expected Behavior
1. Each session card shows model name (e.g., "claude-sonnet-4")
2. Pull from Kiro CLI configuration
3. Fallback to "Unknown" if not available

### Technical Implementation
**File:** `web/src/components/AgentPanel.tsx`

- Read model from Kiro CLI session/config
- Display in session card
- Fallback: "Unknown"

### Acceptance Criteria
- [ ] Model name visible per session
- [ ] Pulled from Kiro CLI
- [ ] Shows "Unknown" if unavailable

---

## Bug #6 & #7: Show Tmux Session Name

### Problem
Tmux session name (e.g., `cao-1c79d355`) not visible in UI.

### Expected Behavior
1. Session name visible in each session card in AgentPanel
2. Session name visible in assigned beads section (BeadsPanel)

### Technical Implementation
**Files:**
- `web/src/components/AgentPanel.tsx` - show session ID in card
- `web/src/components/BeadsPanel.tsx` - show session ID in assignment dropdown/display

### Acceptance Criteria
- [ ] Tmux session name visible in session cards
- [ ] Tmux session name visible in assigned beads display

---

## Bug #8: Remove Clock Emoji from Flows Panel

### Problem
Clock emoji appears in Flows panel, user wants it removed.

### Expected Behavior
Remove the clock emoji (🕐 or similar) from scheduled flows display.

### Technical Implementation
**File:** `web/src/components/FlowsPanel.tsx` (or wherever flows are rendered)

- Find and remove clock emoji from scheduled flow items

### Acceptance Criteria
- [ ] No clock emoji in Flows panel

---

## Bug #10: Agent Status Accuracy

### Problem
Agent status shows green (ready) when it should show yellow (processing). Status flip-flops incorrectly.

### Expected Behavior
- **Green**: IDLE, WAITING_INPUT, READY (agent available)
- **Yellow**: PROCESSING (agent thinking/generating)
- **Red**: ERROR

### Technical Implementation
**Files:**
- `web/src/components/AgentPanel.tsx` - status color logic
- `web/src/components/TerminalView.tsx` - WebSocket status parsing

- Review WebSocket events that update status
- Ensure PROCESSING state is detected when agent is actively working
- Only show green when truly idle/waiting

### Acceptance Criteria
- [ ] Yellow shown when agent is processing
- [ ] Green shown only when idle/waiting for input
- [ ] Red shown on errors
- [ ] No flip-flopping between states

---

## Bug #11: Session Close Modal with Commands

### Problem
Closing a session happens instantly with no feedback. User wants to see the process like the spawn modal.

### Expected Behavior
1. Click close/delete on session
2. Modal appears showing step-by-step:
   - "Killing tmux session cao-xxxxx..."
   - "Verifying session closed..."
   - "Unassigning beads..."
   - "Done"
3. Modal blocks until complete

### Technical Implementation
**File:** `web/src/components/AgentPanel.tsx`

- Add `closingSession` state and `closeLog` array (similar to spawn modal)
- Show modal with steps as they execute
- Block dismissal until complete

### Acceptance Criteria
- [ ] Modal appears when closing session
- [ ] Shows step-by-step progress
- [ ] Blocks until fully complete
- [ ] Confirms session is closed before dismissing

---

## Files to Modify Summary
| File | Bugs |
|------|------|
| `web/src/components/AgentPanel.tsx` | #1, #2, #4, #5, #6, #10, #11 |
| `web/src/components/BeadsPanel.tsx` | #3, #7 |
| `web/src/components/FlowsPanel.tsx` | #8 |
| `web/src/components/TerminalView.tsx` | #10 |
| `src/cli_agent_orchestrator/api/web.py` | #2, #12 |
| `web/src/api.ts` | #12 |

---

## Bug #12: Ralph Loop UI - Start Loop from Dashboard

### Problem
Users cannot start a Ralph loop from the UI. They must use the CLI command manually.

### Expected Behavior
1. RalphPanel shows a textarea to write/paste a PRD
2. Input fields for configuration:
   - Max iterations (default: 25)
   - Min iterations (default: 3)
   - Completion promise (default: "COMPLETE")
3. "Start Ralph Loop" button
4. On click, executes: `ralph-for-kiro loop "<PRD_CONTENT>" --max-iterations X --min-iterations Y --completion-promise "Z"`
5. Shows running status once started

### Technical Implementation
**File:** `web/src/components/RalphPanel.tsx`

When no active loop, show:
```
- Textarea for PRD content (large, resizable)
- Number input: Max Iterations (default 25)
- Number input: Min Iterations (default 3)  
- Text input: Completion Promise (default "COMPLETE")
- "Start Ralph Loop" button
```

**File:** `src/cli_agent_orchestrator/api/web.py`

Add endpoint:
```python
@router.post("/ralph/start")
async def start_ralph_loop(request: RalphStartRequest):
    # request.prompt = PRD content
    # request.max_iterations = int
    # request.min_iterations = int
    # request.completion_promise = str
    
    # Execute ralph-for-kiro loop command via subprocess
    cmd = [
        "ralph-for-kiro", "loop", request.prompt,
        "--max-iterations", str(request.max_iterations),
        "--min-iterations", str(request.min_iterations),
        "--completion-promise", request.completion_promise
    ]
    subprocess.Popen(cmd, cwd=os.getcwd())
    return {"status": "started"}
```

**File:** `web/src/api.ts`

Add to ralph API:
```typescript
ralph: {
  status: () => get('/ralph/status'),
  stop: () => post('/ralph/stop'),
  start: (prompt: string, maxIterations: number, minIterations: number, completionPromise: string) => 
    post('/ralph/start', { prompt, max_iterations: maxIterations, min_iterations: minIterations, completion_promise: completionPromise })
}
```

### Acceptance Criteria
- [ ] Textarea visible in RalphPanel when no loop active
- [ ] Configuration inputs for iterations and completion promise
- [ ] Start button executes ralph-for-kiro loop command
- [ ] UI updates to show running loop after start
- [ ] Can paste full PRD into textarea

---

## Testing Regime

### Pre-Test Setup
1. Kill any existing servers: `pkill -9 -f "uvicorn|vite"`
2. Start API: `cd ~/cao-enhanced && python -m uvicorn cli_agent_orchestrator.api.main:app --host 0.0.0.0 --port 8000 &`
3. Start Vite: `cd ~/cao-enhanced/web && npx vite --host 0.0.0.0 --port 5173 &`
4. Verify both running: `curl -s http://localhost:8000/health && curl -s http://localhost:5173 | head -1`
5. Open browser to http://localhost:5173

---

### Test #1: Auto-Mode Immediate Bead Assignment
**Setup:**
1. Create 3 beads via UI with priorities: HIGH, MEDIUM, LOW
2. Spawn 1 agent, wait for WAITING_INPUT status (green)
3. Ensure no beads are assigned

**Test Steps:**
1. Click "Auto" button on the agent
2. Observe immediately (within 1 second)

**Expected Results:**
- [ ] HIGH priority bead is immediately assigned to agent
- [ ] Task prompt is sent to agent (terminal shows activity)
- [ ] Agent status changes to PROCESSING (yellow)
- [ ] Bead shows as assigned in BeadsPanel

**Failure Criteria:** Bead not assigned within 2 seconds of clicking Auto

---

### Test #2: Bead Unassign on Session Close
**Setup:**
1. Have an agent with a bead assigned (status IN_PROGRESS)
2. Note the bead ID and current status

**Test Steps:**
1. Click close/delete on the agent session
2. Wait for close modal to complete
3. Check BeadsPanel

**Expected Results:**
- [ ] Bead is now unassigned (no session shown)
- [ ] Bead status remains IN_PROGRESS (not reset to OPEN)
- [ ] Bead is available for reassignment

**Failure Criteria:** Bead still shows old session assignment after close

---

### Test #3: AI-Powered Bead Creation
**Setup:**
1. Navigate to BeadsPanel
2. Locate the PRD/planning textbox above beads

**Test Steps:**
1. Paste the following text:
```
Build a REST API with:
- User authentication with JWT
- CRUD operations for products
- Input validation on all endpoints
- Unit tests with 80% coverage
```
2. Click "Generate Beads" button
3. Wait for AI processing

**Expected Results:**
- [ ] Textbox is visible above beads section
- [ ] Generate button triggers AI processing
- [ ] 4+ beads are created with meaningful titles
- [ ] Each bead has description and priority set
- [ ] Beads appear in the list

**Failure Criteria:** No textbox visible, or beads not generated

---

### Test #4: Context Monitor
**Setup:**
1. Spawn an agent
2. Assign a bead with a complex task

**Test Steps:**
1. Let agent process for 30+ seconds
2. Observe session card in AgentPanel

**Expected Results:**
- [ ] Context usage is displayed (e.g., "12k tokens" or "45k / 200k")
- [ ] Value increases as agent works
- [ ] Format is readable and meaningful

**Failure Criteria:** No context info shown, or shows 0/static value

---

### Test #5: Model Display
**Setup:**
1. Spawn an agent

**Test Steps:**
1. Look at session card in AgentPanel

**Expected Results:**
- [ ] Model name is visible (e.g., "claude-sonnet-4", "claude-opus-4")
- [ ] If unavailable, shows "Unknown"
- [ ] Each session shows its own model

**Failure Criteria:** No model info displayed anywhere

---

### Test #6 & #7: Tmux Session Name Visibility
**Setup:**
1. Spawn an agent (creates tmux session like `cao-abc123`)
2. Assign a bead to it

**Test Steps:**
1. Look at AgentPanel session card
2. Look at BeadsPanel assigned bead

**Expected Results:**
- [ ] Session card shows tmux session ID (e.g., `cao-abc123`)
- [ ] Assigned bead shows session ID in assignment display
- [ ] ID matches actual tmux session: `tmux list-sessions`

**Failure Criteria:** Session ID not visible in either location

---

### Test #8: Clock Emoji Removed from Flows
**Setup:**
1. Navigate to Flows tab
2. Have at least one scheduled flow

**Test Steps:**
1. Scan all flow items visually

**Expected Results:**
- [ ] No clock emoji (🕐 🕑 🕒 ⏰ etc.) visible
- [ ] Scheduled flows still identifiable by other means

**Failure Criteria:** Clock emoji still present

---

### Test #10: Agent Status Accuracy
**Setup:**
1. Spawn an agent
2. Assign a complex task that takes 30+ seconds

**Test Steps:**
1. Watch status indicator during processing
2. Watch status when agent finishes and waits
3. Disconnect network briefly to trigger error

**Expected Results:**
- [ ] YELLOW when agent is actively processing/generating
- [ ] GREEN when agent is idle or waiting for input
- [ ] RED when error occurs
- [ ] No flip-flopping between green/yellow during active work

**Failure Criteria:** Shows green while agent is visibly working in terminal

---

### Test #11: Session Close Modal
**Setup:**
1. Have an active agent session with assigned bead

**Test Steps:**
1. Click close/delete button on session
2. Observe modal

**Expected Results:**
- [ ] Modal appears immediately
- [ ] Shows step: "Killing tmux session cao-xxxxx..."
- [ ] Shows step: "Verifying session closed..."
- [ ] Shows step: "Unassigning beads..."
- [ ] Shows step: "Done" or "Complete"
- [ ] Modal blocks until all steps complete
- [ ] Cannot dismiss early

**Failure Criteria:** No modal, or instant close without feedback

---

### Test #12: Ralph Loop UI
**Setup:**
1. Navigate to Ralph tab
2. Ensure no active Ralph loop

**Test Steps:**
1. Locate PRD textarea
2. Paste a simple PRD:
```
Create a hello world function.
Output <promise>COMPLETE</promise> when done.
```
3. Set Max Iterations: 5
4. Set Min Iterations: 1
5. Set Completion Promise: COMPLETE
6. Click "Start Ralph Loop"

**Expected Results:**
- [ ] Textarea is visible and accepts input
- [ ] Configuration inputs are present and editable
- [ ] Start button is clickable
- [ ] After click, UI shows "Running" state
- [ ] Iteration counter updates
- [ ] Can verify with: `cat ~/.kiro/ralph-loop.local.json`

**Failure Criteria:** No textarea, or loop doesn't start

---

## Final Verification
Run all tests in sequence. All checkboxes must be checked for PRD to be considered complete.

**Summary Scorecard:**
| Test | Pass/Fail |
|------|-----------|
| #1 Auto-mode | |
| #2 Bead unassign | |
| #3 AI bead creation | |
| #4 Context monitor | |
| #5 Model display | |
| #6-7 Session name | |
| #8 Clock emoji | |
| #10 Status accuracy | |
| #11 Close modal | |
| #12 Ralph UI | |

**PRD Status:** ___/10 tests passing

Output `<promise>COMPLETE</promise>` only when ALL 10 tests pass.

---

## Completion
Output `<promise>COMPLETE</promise>` when all bugs are fixed and tested.
