# PRD-001: Auto-Mode Immediate Bead Assignment

## Overview
When a user clicks "Auto" on an agent session that is in WAITING/IDLE state with no bead assigned, the system should immediately find the highest priority unassigned bead and assign it to that session, then send the task prompt to the agent.

## Current Behavior
- Auto-mode is toggled on but does not immediately assign a bead
- The useEffect only triggers on dependency changes, not on initial Auto click
- User must wait for next render cycle or state change for auto-mode to pick up a bead

## Expected Behavior
1. User clicks "Auto" button on a session that is WAITING_INPUT or IDLE
2. System immediately checks for highest priority unassigned bead (status: OPEN or IN_PROGRESS)
3. If bead found:
   - Assign bead to session via API
   - Send bead's task prompt to agent via `api.sessions.input()`
   - Update UI to reflect assignment
4. If no bead available:
   - Auto-mode stays enabled
   - Will pick up next available bead when one becomes available

## Technical Requirements

### File: `web/src/components/AgentPanel.tsx`

#### 1. Modify Auto button click handler
```typescript
const handleAutoToggle = async (sessionId: string) => {
  const newAutoMode = !autoModeSessions.has(sessionId)
  
  if (newAutoMode) {
    autoModeSessions.add(sessionId)
    setAutoModeSessions(new Set(autoModeSessions))
    
    // Immediately try to assign a bead
    await tryAssignBead(sessionId)
  } else {
    autoModeSessions.delete(sessionId)
    setAutoModeSessions(new Set(autoModeSessions))
  }
}
```

#### 2. Extract bead assignment logic into reusable function
```typescript
const tryAssignBead = async (sessionId: string) => {
  const session = sessions.find(s => s.id === sessionId)
  if (!session) return
  
  const status = sessionStatuses[sessionId] || session.status
  if (status === 'PROCESSING') return // Skip if busy
  
  // Check if already has assigned bead
  const assignedBead = tasks.find(t => t.assigned_session === sessionId)
  if (assignedBead) return
  
  // Find highest priority unassigned bead (OPEN or IN_PROGRESS)
  const availableBeads = tasks
    .filter(t => !t.assigned_session && (t.status === 'OPEN' || t.status === 'IN_PROGRESS'))
    .sort((a, b) => {
      const priorityOrder = { high: 0, medium: 1, low: 2 }
      return (priorityOrder[a.priority] || 2) - (priorityOrder[b.priority] || 2)
    })
  
  const beadToAssign = availableBeads[0]
  if (!beadToAssign) return
  
  // Assign bead to session
  await api.tasks.update(beadToAssign.id, { assigned_session: sessionId })
  
  // Send task prompt to agent
  const prompt = beadToAssign.description || beadToAssign.title
  await api.sessions.input(sessionId, prompt)
  
  // Refresh tasks
  fetchTasks()
}
```

#### 3. Update useEffect to use the same function
```typescript
useEffect(() => {
  autoModeArray.forEach(sessionId => tryAssignBead(sessionId))
}, [autoModeArray.join(','), tasks, sessions, sessionStatuses])
```

## Acceptance Criteria
- [ ] Clicking "Auto" on WAITING/IDLE session with no bead immediately assigns highest priority bead
- [ ] Bead assignment considers both OPEN and IN_PROGRESS status beads
- [ ] Task prompt is sent to agent immediately after assignment
- [ ] If no beads available, auto-mode stays enabled for future beads
- [ ] Existing useEffect auto-mode logic continues to work for ongoing monitoring

## Testing
1. Create 3 beads with different priorities (high, medium, low)
2. Spawn an agent, wait for WAITING_INPUT status
3. Click "Auto" button
4. Verify: highest priority bead is immediately assigned and task sent
5. Verify: agent starts processing the task

## Files to Modify
- `web/src/components/AgentPanel.tsx`

## Dependencies
- None

## Risks
- Race condition if multiple auto-mode sessions try to grab same bead (mitigated by immediate API call)
