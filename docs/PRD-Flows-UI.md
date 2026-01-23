# PRD: Flows UI for CLI Agent Orchestrator

## Overview

Add a Flows management panel to the CAO web dashboard, enabling users to create, manage, and monitor scheduled agent sessions directly from the UI without using CLI commands.

## Problem Statement

Currently, flows (scheduled agent sessions) can only be managed via CLI commands (`cao flow add/list/run/enable/disable/remove`). Users must:
- Write markdown files manually with correct YAML frontmatter
- Use terminal commands to manage flows
- Have no visibility into flow execution history or status from the dashboard

## Goals

1. **Visual flow management** - Create, edit, delete flows from the UI
2. **Real-time monitoring** - See flow status, next run time, execution history
3. **Easy scheduling** - Human-friendly cron builder
4. **Quick actions** - Enable/disable, manual trigger with one click

## Non-Goals

- Complex workflow orchestration (multi-step flows)
- Flow templates marketplace
- External integrations (webhooks, notifications)

---

## User Stories

| As a... | I want to... | So that... |
|---------|--------------|------------|
| User | See all my scheduled flows in one place | I can monitor what's running |
| User | Create a new flow without writing markdown | I can quickly set up automation |
| User | Manually trigger a flow | I can test it before scheduling |
| User | Enable/disable flows with one click | I can pause automation without deleting |
| User | See when a flow last ran and what happened | I can debug issues |
| User | Edit an existing flow's schedule or prompt | I can adjust without recreating |

---

## Proposed Solution

### UI Components

#### 1. Flows Panel (Main View)

```
┌──────────────────────────────────────────────────────────────────┐
│ ⏰ Scheduled Flows                              [+ Create Flow]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 🟢 daily-standup                              ● Enabled    │  │
│  │                                                            │  │
│  │ 📅 Schedule: 0 9 * * 1-5                                   │  │
│  │    "At 9:00 AM, Monday through Friday"                     │  │
│  │                                                            │  │
│  │ 🤖 Agent: developer                                        │  │
│  │ ⏭️  Next run: Mon Jan 27, 9:00 AM                          │  │
│  │ ✅ Last run: Fri Jan 24, 9:00 AM (success)                 │  │
│  │                                                            │  │
│  │                        [▶ Run Now]  [Edit]  [Disable]  [×] │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ ⚫ monitor-service                            ○ Disabled   │  │
│  │                                                            │  │
│  │ 📅 Schedule: */5 * * * *                                   │  │
│  │    "Every 5 minutes"                                       │  │
│  │                                                            │  │
│  │ 🤖 Agent: developer                                        │  │
│  │ 🔧 Script: ./health-check.sh (conditional)                 │  │
│  │ ⏭️  Next run: --                                           │  │
│  │ ❌ Last run: Thu Jan 23, 2:15 PM (skipped)                 │  │
│  │                                                            │  │
│  │                        [▶ Run Now]  [Edit]  [Enable]   [×] │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                     📭 No more flows                       │  │
│  │              Create one to automate agent tasks            │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

#### 2. Create/Edit Flow Modal

```
┌──────────────────────────────────────────────────────────────────┐
│ Create New Flow                                            [×]   │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Flow Name *                                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ daily-standup                                              │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Agent Profile *                                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 🤖 developer                                           ▼   │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Schedule *                                                      │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ ○ Simple    ● Cron Expression                            │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 0 9 * * 1-5                                                │  │
│  └────────────────────────────────────────────────────────────┘  │
│  💡 "At 9:00 AM, Monday through Friday"                          │
│                                                                  │
│  ── OR use simple scheduler ──                                   │
│                                                                  │
│  Every [ 1 ▼] [day ▼] at [09:00 ▼]                              │
│                                                                  │
│  Prompt *                                                        │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Review yesterday's commits in the current repository       │  │
│  │ and create a standup summary including:                    │  │
│  │ - What was completed                                       │  │
│  │ - Any blockers found                                       │  │
│  │ - Suggested next steps                                     │  │
│  │                                                            │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ▶ Advanced Options                                              │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Conditional Script (optional)                              │  │
│  │ ┌────────────────────────────────────────────────────────┐ │  │
│  │ │ ./health-check.sh                                      │ │  │
│  │ └────────────────────────────────────────────────────────┘ │  │
│  │ Script must return JSON: {"execute": true, "output": {}}   │  │
│  │                                                            │  │
│  │ Provider                                                   │  │
│  │ ┌────────────────────────────────────────────────────────┐ │  │
│  │ │ kiro_cli                                           ▼   │ │  │
│  │ └────────────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│                              [Cancel]  [Create Flow]             │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

#### 3. Flow Execution History (Expandable)

```
┌────────────────────────────────────────────────────────────────┐
│ 🟢 daily-standup                                   [Collapse]  │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Recent Executions                                             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ✅ Fri Jan 24, 9:00 AM    Duration: 2m 34s    [View Log] │  │
│  │ ✅ Thu Jan 23, 9:00 AM    Duration: 1m 58s    [View Log] │  │
│  │ ❌ Wed Jan 22, 9:00 AM    Error: timeout      [View Log] │  │
│  │ ✅ Tue Jan 21, 9:00 AM    Duration: 2m 12s    [View Log] │  │
│  │ ⏭️  Mon Jan 20, 9:00 AM    Skipped (disabled)            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  [Show More]                                                   │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## API Specification

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v2/flows` | List all flows |
| `POST` | `/api/v2/flows` | Create new flow |
| `GET` | `/api/v2/flows/{name}` | Get flow details |
| `PUT` | `/api/v2/flows/{name}` | Update flow |
| `DELETE` | `/api/v2/flows/{name}` | Delete flow |
| `POST` | `/api/v2/flows/{name}/run` | Manual trigger |
| `POST` | `/api/v2/flows/{name}/enable` | Enable flow |
| `POST` | `/api/v2/flows/{name}/disable` | Disable flow |
| `GET` | `/api/v2/flows/{name}/history` | Get execution history |

### Data Models

```typescript
interface Flow {
  name: string
  schedule: string              // Cron expression
  agent_profile: string
  provider: string              // Default: "kiro_cli"
  prompt: string
  script?: string               // Optional conditional script
  enabled: boolean
  created_at: string
  updated_at: string
  next_run?: string             // ISO timestamp
  last_run?: FlowExecution
}

interface FlowExecution {
  id: string
  flow_name: string
  started_at: string
  completed_at?: string
  status: "running" | "success" | "failed" | "skipped"
  duration_ms?: number
  error?: string
  session_id?: string           // Link to agent session
}

interface CreateFlowRequest {
  name: string
  schedule: string
  agent_profile: string
  prompt: string
  provider?: string
  script?: string
}
```

### Example Responses

**GET /api/v2/flows**
```json
[
  {
    "name": "daily-standup",
    "schedule": "0 9 * * 1-5",
    "schedule_human": "At 9:00 AM, Monday through Friday",
    "agent_profile": "developer",
    "provider": "kiro_cli",
    "prompt": "Review yesterday's commits...",
    "enabled": true,
    "next_run": "2026-01-27T09:00:00Z",
    "last_run": {
      "status": "success",
      "started_at": "2026-01-24T09:00:00Z",
      "duration_ms": 154000
    }
  }
]
```

---

## Technical Implementation

### Phase 1: API Layer (Backend)
1. Review existing `flow_service.py` implementation
2. Add REST endpoints to `v2.py`
3. Add execution history tracking
4. Add cron-to-human-readable conversion

### Phase 2: UI Components (Frontend)
1. Create `FlowsPanel.tsx` component
2. Add to main App layout
3. Implement flow list view
4. Implement create/edit modal

### Phase 3: Polish
1. Add cron builder helper
2. Add execution history view
3. Real-time status updates via WebSocket
4. Error handling and validation

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Users can create flow from UI | 100% feature parity with CLI |
| Time to create a flow | < 60 seconds |
| Flow status visibility | Real-time updates |

---

## Open Questions

1. **Script editing** - Should we allow editing scripts in the UI or just reference paths?
2. **Flow templates** - Should we provide pre-built flow templates?
3. **Notifications** - Should flows send notifications on failure?
4. **Permissions** - Any access control needed for flows?

---

## Timeline

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| Phase 1 | 1 day | API endpoints |
| Phase 2 | 1 day | Basic UI |
| Phase 3 | 1 day | Polish & history |

**Total: 3 days**

---

## Appendix: Cron Expression Reference

| Expression | Description |
|------------|-------------|
| `* * * * *` | Every minute |
| `*/5 * * * *` | Every 5 minutes |
| `0 * * * *` | Every hour |
| `0 9 * * *` | Daily at 9 AM |
| `0 9 * * 1-5` | Weekdays at 9 AM |
| `0 0 * * 0` | Weekly on Sunday |
| `0 0 1 * *` | Monthly on the 1st |
