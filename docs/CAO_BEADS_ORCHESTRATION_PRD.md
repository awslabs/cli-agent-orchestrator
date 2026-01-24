# CAO + Beads UI Integration PRD

## Overview

This PRD defines how to represent CLI Agent Orchestrator (CAO) capabilities in the web UI, integrating with the Beads task tracking system. The goal is to make multi-agent orchestration intuitive while supporting both simple (1 bead → 1 agent) and complex (orchestrator → multiple workers) workflows.

---

## Problem Statement

Currently, the UI supports:
- Viewing beads (tasks)
- Assigning a bead to a single agent
- Viewing active sessions

**Missing capabilities:**
1. No way to use an orchestrator/supervisor agent that coordinates multiple workers
2. No visibility into parent-child agent relationships
3. No view of inter-agent communication (inbox/messages)
4. No representation of sub-beads created during orchestration
5. No workflow visualization showing how agents collaborate

---

## User Personas

### 1. Solo Developer
- Wants to assign individual tasks to specialized agents
- Needs simple 1:1 bead-to-agent mapping
- Doesn't need orchestration complexity

### 2. Power User
- Wants to leverage multi-agent orchestration
- Assigns complex beads to a supervisor that decomposes work
- Needs visibility into worker status and coordination

### 3. Observer/Manager
- Wants to monitor agent activity across the system
- Needs high-level view of what's happening
- Wants to intervene when things go wrong

---

## Core Concepts

### CAO Orchestration Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Handoff** | Sync: Supervisor → Worker → Wait → Result | Sequential dependent tasks |
| **Assign** | Async: Supervisor → Spawn Worker → Continue | Parallel independent tasks |
| **Send Message** | Direct agent-to-agent communication | Coordination, feedback |

### Beads Hierarchy

Beads supports hierarchical IDs:
- `bd-a3f8` - Epic/Parent bead
- `bd-a3f8.1` - Child task
- `bd-a3f8.1.1` - Sub-task

This maps naturally to orchestrator decomposition.

### Agent Relationships

```
Supervisor (code_supervisor)
├── Worker 1 (developer) - assigned via "assign"
├── Worker 2 (log-diver) - assigned via "assign"  
└── Worker 3 (reviewer) - waiting for handoff
```

---

## UI Design

### 1. Assignment Modal - Two Modes

When clicking "Assign" on a bead, present two clear options:

```
┌─────────────────────────────────────────────────────┐
│ Assign Bead                                    [X]  │
├─────────────────────────────────────────────────────┤
│                                                     │
│ 📋 Task                                             │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Fix SQS DeleteMessageBatch Throttling           │ │
│ │ P1 · Link: t.corp.amazon.com/...                │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ How should this be worked on?                       │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🤖 SINGLE AGENT                            [●]  │ │
│ │                                                 │ │
│ │ Assign to one agent who works on the entire    │ │
│ │ task independently.                            │ │
│ │                                                 │ │
│ │ Best for: Simple tasks, quick fixes,           │ │
│ │ well-defined scope                             │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🎯 ORCHESTRATOR                            [○]  │ │
│ │                                                 │ │
│ │ Assign to a supervisor who decomposes the      │ │
│ │ task and coordinates multiple worker agents.   │ │
│ │                                                 │ │
│ │ Best for: Complex tasks, multi-step work,      │ │
│ │ tasks needing different expertise              │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### Single Agent Mode (expanded)

```
┌─────────────────────────────────────────────────────┐
│ Select Agent                                        │
│                                                     │
│ EXISTING SESSIONS                                   │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🟢 generalist (cao-abc123)              [idle]  │ │
│ │ 🟡 developer (cao-def456)               [busy]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ SPAWN NEW AGENT                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🤖 generalist                          [+ new]  │ │
│ │ 🔧 developer                           [+ new]  │ │
│ │ 🔍 log-diver                           [+ new]  │ │
│ │ 🛡️ oncall-buddy                        [+ new]  │ │
│ │ ⚔️ ticket-ninja                        [+ new]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│                              [Cancel]  [Assign →]   │
└─────────────────────────────────────────────────────┘
```

#### Orchestrator Mode (expanded)

```
┌─────────────────────────────────────────────────────┐
│ Select Supervisor                                   │
│                                                     │
│ The supervisor will analyze the task and            │
│ automatically spawn worker agents as needed.        │
│                                                     │
│ AVAILABLE SUPERVISORS                               │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🎯 code_supervisor                              │ │
│ │    Coordinates development workflows            │ │
│ │    Workers: developer, reviewer, log-diver      │ │
│ │                                                 │ │
│ │ 🎯 ticket_supervisor                            │ │
│ │    Coordinates ticket investigation             │ │
│ │    Workers: ticket-ninja, log-diver, oncall     │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ⚙️ Options                                          │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Max workers: [3 ▼]                              │ │
│ │ ☑ Auto-create sub-beads for decomposed tasks   │ │
│ │ ☐ Require approval before spawning workers     │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│                           [Cancel]  [Start Work →]  │
└─────────────────────────────────────────────────────┘
```

---

### 2. Sessions Panel - Hierarchy View

Replace flat session list with hierarchical view showing orchestration relationships:

```
┌─────────────────────────────────────────────────────┐
│ Active Sessions                              [↻]    │
├─────────────────────────────────────────────────────┤
│                                                     │
│ 🎯 code_supervisor (cao-abc123)         [BUSY]     │
│ │  Orchestrating: bd-a3f8                          │
│ │  Mode: Assign (parallel)                         │
│ │                                                  │
│ ├─🔧 developer (cao-def456)             [WIP]      │
│ │    Task: bd-a3f8.1 "Analyze throttling"          │
│ │    Progress: Investigating logs...               │
│ │                                                  │
│ ├─🔍 log-diver (cao-ghi789)             [WIP]      │
│ │    Task: bd-a3f8.2 "Check CloudWatch"            │
│ │    Progress: Querying metrics...                 │
│ │                                                  │
│ └─📝 reviewer (cao-jkl012)              [WAITING]  │
│      Task: bd-a3f8.3 "Review fix"                  │
│      Blocked by: bd-a3f8.1, bd-a3f8.2              │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ 🤖 generalist (cao-mno345)              [IDLE]     │
│    No active task                                   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### Session Card (expanded)

Clicking a session expands to show:

```
┌─────────────────────────────────────────────────────┐
│ 🎯 code_supervisor (cao-abc123)                     │
├─────────────────────────────────────────────────────┤
│ Status: BUSY (orchestrating)                        │
│ Bead: bd-a3f8 "Fix SQS throttling"                 │
│ Started: 10 min ago                                 │
│                                                     │
│ Workers (3)                                         │
│ ┌─────────────────────────────────────────────────┐ │
│ │ developer    │ WIP     │ bd-a3f8.1 │ [View]    │ │
│ │ log-diver    │ WIP     │ bd-a3f8.2 │ [View]    │ │
│ │ reviewer     │ WAITING │ bd-a3f8.3 │ [View]    │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ Inbox (2 pending)                                   │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 📨 From developer: "Analysis complete"    2m    │ │
│ │ 📨 From log-diver: "Found anomaly"        1m    │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ [View Terminal]  [Send Message]  [Stop]             │
└─────────────────────────────────────────────────────┘
```

---

### 3. Beads Panel - Show Decomposition

When a bead is decomposed by an orchestrator, show the hierarchy:

```
┌─────────────────────────────────────────────────────┐
│ Bead Queue                    [Open ▼]  [+ New]     │
├─────────────────────────────────────────────────────┤
│                                                     │
│ ▼ bd-a3f8: Fix SQS throttling              P1 WIP  │
│ │  Assigned to: code_supervisor                    │
│ │  Workers: 3 active                               │
│ │                                                  │
│ ├─ bd-a3f8.1: Analyze throttling logs      P1 WIP  │
│ │  └─ Assigned to: developer                       │
│ │                                                  │
│ ├─ bd-a3f8.2: Check CloudWatch metrics     P2 WIP  │
│ │  └─ Assigned to: log-diver                       │
│ │                                                  │
│ └─ bd-a3f8.3: Review and apply fix         P1 WAIT │
│    └─ Assigned to: reviewer                        │
│    └─ Blocked by: bd-a3f8.1, bd-a3f8.2             │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ bd-xyz: Another task                       P2 OPEN  │
│   Unassigned                                        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

### 4. Orchestration Flow View (New Panel)

A visual representation of active orchestrations:

```
┌─────────────────────────────────────────────────────┐
│ Orchestration Flow                           [↻]    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  bd-a3f8: Fix SQS throttling                        │
│                                                     │
│              ┌──────────────┐                       │
│              │  Supervisor  │                       │
│              │ code_super.. │                       │
│              └──────┬───────┘                       │
│                     │                               │
│         ┌──────────┼──────────┐                     │
│         │ assign   │ assign   │ handoff             │
│         ▼          ▼          ▼                     │
│    ┌─────────┐ ┌─────────┐ ┌─────────┐             │
│    │developer│ │log-diver│ │reviewer │             │
│    │  [WIP]  │ │  [WIP]  │ │ [WAIT]  │             │
│    └────┬────┘ └────┬────┘ └─────────┘             │
│         │          │            ▲                   │
│         └──────────┴────────────┘                   │
│              send_message                           │
│                                                     │
│  Legend: ─── sync  ╌╌╌ async  ▶ message             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

### 5. Inbox/Messages Panel (New)

Show inter-agent communication:

```
┌─────────────────────────────────────────────────────┐
│ Agent Messages                               [↻]    │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Filter: [All ▼]  [Pending only ☐]                   │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 📨 developer → code_supervisor          2m ago  │ │
│ │ "Analysis complete. Found rate limiting at      │ │
│ │ DeleteMessageBatch. Recommend batch size..."    │ │
│ │                                    [Delivered]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 📨 log-diver → code_supervisor          1m ago  │ │
│ │ "CloudWatch shows throttling spikes at 14:00    │ │
│ │ correlating with batch job. See metrics..."     │ │
│ │                                      [Pending]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 📨 code_supervisor → reviewer           5m ago  │ │
│ │ "Please review the proposed fix once dev        │ │
│ │ and log-diver complete their analysis."         │ │
│ │                                    [Delivered]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

### 6. Activity Feed Enhancement

Show orchestration events in the activity feed:

```
┌─────────────────────────────────────────────────────┐
│ Activity                                            │
├─────────────────────────────────────────────────────┤
│ 10:15 🎯 Orchestration started: bd-a3f8             │
│       └─ Supervisor: code_supervisor                │
│                                                     │
│ 10:15 🤖 Worker spawned: developer                  │
│       └─ Task: bd-a3f8.1 (via assign)               │
│                                                     │
│ 10:15 🤖 Worker spawned: log-diver                  │
│       └─ Task: bd-a3f8.2 (via assign)               │
│                                                     │
│ 10:16 📨 Message: developer → supervisor            │
│       └─ "Analysis complete"                        │
│                                                     │
│ 10:17 🔄 Handoff initiated: supervisor → reviewer   │
│       └─ Task: bd-a3f8.3                            │
│                                                     │
│ 10:20 ✅ Task completed: bd-a3f8.1                  │
│       └─ Agent: developer                           │
└─────────────────────────────────────────────────────┘
```

---

## Navigation & Layout

### Main Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ CAO Dashboard                                    [Settings] [?] │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [Beads]  [Sessions]  [Orchestrations]  [Messages]  [Activity]  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────┐  ┌───────────────────────────────┐ │
│  │                         │  │                               │ │
│  │    Main Content Area    │  │    Terminal / Detail View     │ │
│  │    (selected tab)       │  │                               │ │
│  │                         │  │                               │ │
│  │                         │  │                               │ │
│  │                         │  │                               │ │
│  └─────────────────────────┘  └───────────────────────────────┘ │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  Activity Feed (collapsible)                                    │
└─────────────────────────────────────────────────────────────────┘
```

### Tab Descriptions

| Tab | Purpose |
|-----|---------|
| **Beads** | Task queue with hierarchy, assignment, status |
| **Sessions** | Active agents with parent-child relationships |
| **Orchestrations** | Visual flow diagrams of active orchestrations |
| **Messages** | Inter-agent inbox/communication log |
| **Activity** | Real-time event feed |

---

## Data Model

### Session (enhanced)

```typescript
interface Session {
  id: string                    // cao-abc123
  agent_name: string            // code_supervisor
  status: 'IDLE' | 'BUSY' | 'WAITING' | 'ERROR'
  
  // Orchestration
  parent_session?: string       // Parent supervisor session
  spawned_sessions: string[]    // Workers this session created
  orchestration_mode?: 'handoff' | 'assign' | 'send_message'
  
  // Task
  assigned_bead?: string        // bd-a3f8
  
  // Inbox
  pending_messages: number
}
```

### Bead (enhanced)

```typescript
interface Bead {
  id: string                    // bd-a3f8 or bd-a3f8.1
  title: string
  description?: string
  priority: 1 | 2 | 3
  status: 'open' | 'wip' | 'closed'
  
  // Assignment
  assignee?: string             // Session ID
  assignee_type?: 'single' | 'orchestrator'
  
  // Hierarchy
  parent_bead?: string          // bd-a3f8 for bd-a3f8.1
  child_beads: string[]         // Sub-beads created by orchestrator
  
  // Dependencies
  blocked_by: string[]          // Other bead IDs
  blocks: string[]
}
```

### InboxMessage

```typescript
interface InboxMessage {
  id: string
  from_session: string
  to_session: string
  content: string
  status: 'pending' | 'delivered' | 'read'
  timestamp: string
  related_bead?: string
}
```

### Orchestration

```typescript
interface Orchestration {
  id: string
  supervisor_session: string
  root_bead: string
  workers: {
    session_id: string
    bead_id: string
    mode: 'handoff' | 'assign'
    status: 'active' | 'completed' | 'failed'
  }[]
  started_at: string
  status: 'active' | 'completed' | 'failed'
}
```

---

## API Endpoints (New/Enhanced)

### Orchestration

```
POST /api/v2/orchestrations
  - Start orchestration with supervisor
  - Body: { bead_id, supervisor_agent, options }

GET /api/v2/orchestrations
  - List active orchestrations

GET /api/v2/orchestrations/{id}
  - Get orchestration details with worker tree

DELETE /api/v2/orchestrations/{id}
  - Stop orchestration and all workers
```

### Sessions (enhanced)

```
GET /api/v2/sessions/{id}/children
  - Get worker sessions spawned by this session

GET /api/v2/sessions/{id}/inbox
  - Get pending messages for session
```

### Beads (enhanced)

```
GET /api/v2/beads/{id}/children
  - Get sub-beads

POST /api/v2/beads/{id}/decompose
  - Create sub-beads from parent
  - Body: { sub_beads: [{title, description, priority}] }
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1)
- [ ] Add orchestrator mode toggle to assignment modal
- [ ] Track parent_session in session model
- [ ] Show basic hierarchy in sessions panel
- [ ] Update activity feed with orchestration events

### Phase 2: Hierarchy (Week 2)
- [ ] Implement sub-bead creation (bd-xxx.1)
- [ ] Show bead hierarchy in BeadsPanel
- [ ] Link beads to sessions bidirectionally
- [ ] Add blocked_by/blocks to beads

### Phase 3: Communication (Week 3)
- [ ] Create Messages panel
- [ ] Show inbox count on sessions
- [ ] Real-time message updates via WebSocket
- [ ] Message filtering and search

### Phase 4: Visualization (Week 4)
- [ ] Create Orchestrations panel
- [ ] Implement flow diagram component
- [ ] Add orchestration status tracking
- [ ] Bulk operations (stop all workers, etc.)

---

## Success Metrics

1. **Usability**: Users can start an orchestration in < 3 clicks
2. **Visibility**: Users can see all worker status at a glance
3. **Control**: Users can intervene (stop, redirect) any agent
4. **Understanding**: New users understand orchestration modes within 5 minutes

---

## Open Questions

1. Should sub-beads be auto-created by the UI or only by the supervisor agent?
2. How to handle failed workers - auto-retry or manual intervention?
3. Should there be a "max depth" for orchestration hierarchies?
4. How to visualize very large orchestrations (10+ workers)?

---

## Appendix: CAO Orchestration Modes Reference

### Handoff (Synchronous)
```
Supervisor                    Worker
    │                           │
    │──── handoff(task) ───────▶│
    │         (wait)            │
    │                           │ (working...)
    │                           │
    │◀──── result ─────────────│
    │                           │ (exits)
    ▼
```

### Assign (Asynchronous)
```
Supervisor                    Worker
    │                           │
    │──── assign(task) ────────▶│
    │                           │
    │ (continues working)       │ (working...)
    │                           │
    │◀── send_message(done) ───│
    │                           │
    ▼                           ▼
```

### Send Message (Direct)
```
Agent A                      Agent B
    │                           │
    │── send_message(msg) ─────▶│ (queued if busy)
    │                           │
    │ (continues)               │ (receives when idle)
    │                           │
    ▼                           ▼
```
