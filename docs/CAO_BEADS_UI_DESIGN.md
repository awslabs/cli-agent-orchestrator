# CAO + Beads UI Design

## Current State
- BeadsPanel: List of tasks (beads)
- AgentPanel: List of sessions
- Assign: Bead → Single Agent

## Proposed Enhancement: Orchestration View

### 1. Assignment Modal - Add "Orchestrator Mode"

When assigning a bead, show two modes:

```
┌─────────────────────────────────────────┐
│ Assign Bead                             │
├─────────────────────────────────────────┤
│ Task: Fix SQS throttling issue          │
│                                         │
│ ○ Single Agent                          │
│   Assign to one agent to work on task   │
│                                         │
│ ○ Orchestrator                          │
│   Supervisor decomposes & coordinates   │
│   multiple workers                      │
│                                         │
├─────────────────────────────────────────┤
│ [Select Agent/Supervisor...]            │
└─────────────────────────────────────────┘
```

### 2. Orchestration Tree View

When a supervisor spawns workers, show hierarchy:

```
┌─────────────────────────────────────────┐
│ 🎯 bd-a3f8: Fix SQS throttling          │
│ └─ 🤖 code_supervisor (cao-abc123)      │
│    ├─ 🔧 developer (cao-def456) [WIP]   │
│    │   └─ bd-a3f8.1: Analyze logs       │
│    ├─ 🔍 log-diver (cao-ghi789) [WIP]   │
│    │   └─ bd-a3f8.2: Check metrics      │
│    └─ 📝 reviewer (cao-jkl012) [IDLE]   │
│        └─ bd-a3f8.3: Review fix         │
└─────────────────────────────────────────┘
```

### 3. Session Panel - Show Parent/Child Relationships

```
Sessions
├─ code_supervisor (cao-abc123) [BUSY]
│  ├─ Orchestrating: bd-a3f8
│  ├─ Workers: 3 spawned
│  └─ Mode: Assign (async)
│
├─ developer (cao-def456) [WIP]
│  ├─ Parent: cao-abc123
│  ├─ Task: bd-a3f8.1
│  └─ Mode: Handoff (sync)
```

### 4. Inbox/Message Queue View

Show inter-agent communication:

```
┌─────────────────────────────────────────┐
│ 📬 Agent Inbox                          │
├─────────────────────────────────────────┤
│ cao-abc123 (supervisor)                 │
│ └─ 2 pending messages                   │
│    ├─ From: cao-def456 "Analysis done"  │
│    └─ From: cao-ghi789 "Metrics ready"  │
└─────────────────────────────────────────┘
```

### 5. Workflow Visualization

For complex orchestrations, show flow:

```
        ┌──────────┐
        │Supervisor│
        └────┬─────┘
             │ assign
    ┌────────┼────────┐
    ▼        ▼        ▼
┌──────┐ ┌──────┐ ┌──────┐
│Dev 1 │ │Dev 2 │ │Review│
└──┬───┘ └──┬───┘ └──────┘
   │        │         ▲
   └────────┴─────────┘
         send_message
```

## Implementation Priority

### Phase 1: Basic Orchestrator Support
1. Add "Orchestrator Mode" toggle in assign modal
2. Show parent-child session relationships
3. Track which bead spawned which sessions

### Phase 2: Hierarchy Visualization  
1. Tree view of supervisor → workers
2. Sub-bead creation (bd-xxx.1, bd-xxx.2)
3. Inbox message queue display

### Phase 3: Advanced Orchestration
1. Workflow diagram visualization
2. Real-time status updates via WebSocket
3. Manual intervention controls (pause, redirect, etc.)

## Data Model Changes

### Session
```typescript
interface Session {
  id: string
  agent_name: string
  parent_session?: string  // For workers spawned by supervisor
  orchestration_mode?: 'handoff' | 'assign' | 'direct'
  spawned_sessions?: string[]  // Workers this session created
}
```

### Bead
```typescript
interface Bead {
  id: string  // bd-xxx or bd-xxx.1 for sub-beads
  parent_bead?: string
  assignee?: string
  child_beads?: string[]  // Sub-tasks created by orchestrator
}
```

### Inbox Message
```typescript
interface InboxMessage {
  id: string
  from_session: string
  to_session: string
  content: string
  status: 'pending' | 'delivered' | 'read'
  timestamp: string
}
```
