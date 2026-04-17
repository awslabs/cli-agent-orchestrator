# Memory System

CAO's memory system gives agents persistent, cross-session storage. Agents store facts, decisions, and preferences during a session; CAO injects relevant memories back as context when the agent starts its next session.

## How It Works

1. **Agent stores a memory** via `memory_store` MCP tool during a session
2. **CAO persists it** as a markdown wiki file under `~/.aws/cli-agent-orchestrator/memory/`
3. **On next session start**, CAO injects matching memories as a `<cao-memory>` context block before the agent's first message
4. **Agent recalls** with `memory_recall` when it needs to look something up explicitly

## Memory Scopes

Scope controls where a memory is stored and who can read it back.

| Scope | Storage location | Use when |
|---|---|---|
| `global` | `memory/global/wiki/global/` | Cross-project facts: user preferences, coding standards |
| `project` | `memory/{cwd_hash}/wiki/project/` | Project-specific: architecture decisions, conventions |
| `session` | `memory/global/wiki/session/` | Ephemeral: notes for current session only |
| `agent` | `memory/global/wiki/agent/` | Role-specific: patterns the agent role always applies |

`project` is the default scope. The project hash is `sha256(realpath(cwd))[:12]`.

> **Note:** `session` and `agent` scopes are stored under the global container, not in their own top-level directories. Only `project` scope gets a dedicated directory keyed by project hash.

## Memory Types

Type is a classification label — it does not affect storage location.

| Type | Use for |
|---|---|
| `project` | Architecture notes, project conventions (default) |
| `user` | User preferences, working style |
| `feedback` | Corrections, recurring mistakes to avoid |
| `reference` | Pointers to external resources, docs, links |

## MCP Tools

Agents use these tools via the `cao-mcp-server` MCP server.

### `memory_store`

Store or update a memory. If the key already exists, the new content is appended as a timestamped entry (upsert).

```
memory_store(
  content="Always use pytest for testing in this project",
  scope="project",          # optional, default: "project"
  memory_type="feedback",   # optional, default: "project"
  key="testing-framework",  # optional, auto-generated from content if omitted
  tags="testing,pytest"     # optional
)
```

### `memory_recall`

Search memories by keyword query and optional filters.

```
memory_recall(
  query="testing",     # optional, searches content
  scope="project",     # optional, filter by scope
  memory_type=None,    # optional, filter by type
  limit=10             # optional, default 10, max 100
)
```

Results are returned sorted by recency, with scope precedence: `session` > `project` > `global`.

### `memory_forget`

Remove a memory by key.

```
memory_forget(
  key="testing-framework",
  scope="project"
)
```

## CLI Commands

```bash
# List memories (shows global + current project by default)
cao memory list
cao memory list --all              # all projects
cao memory list --scope global
cao memory list --type feedback

# Show full content of a memory
cao memory show <key>
cao memory show <key> --scope global

# Delete a memory
cao memory delete <key>
cao memory delete <key> --scope project --yes

# Clear all memories for a scope
cao memory clear --scope session --yes
```

## Context Injection

When an agent receives its first message in a session, CAO prepends a `<cao-memory>` block containing relevant memories (up to 3000 characters). The block format:

```
<cao-memory>
## Context from CAO Memory
- [session] recent-decision: Use the existing auth middleware, do not rewrite
- [project] testing-framework: Always use pytest for testing in this project
- [global] user-prefers-concise: User prefers concise responses without trailing summaries
</cao-memory>

<original user message>
```

Memories are selected in scope precedence order: `session` > `project` > `global`.

**Kiro CLI:** Memory injection happens via the `agentSpawn` hook registered at `~/.kiro/agents/{profile}.json`, which fires before the first user message. CAO does not double-inject for Kiro.

## Auto-Save Hooks

CAO registers hooks to prompt agents to save memories automatically.

### Claude Code

Registered in `.claude/settings.local.json` (in the working directory) at terminal creation:

- **Stop hook**: fires every 15 human messages, prompts agent to save key findings
- **PreCompact hook**: fires before context compression, prompts emergency save

### Kiro CLI

Registered in `~/.kiro/agents/{profile}.json` at terminal creation:

- **`agentSpawn`**: injects memory context at agent startup (via CAO API)
- **`userPromptSubmit`**: fires a save reminder every 15 user prompts

Hook scripts are installed to `~/.aws/cli-agent-orchestrator/hooks/` on server startup.

## Storage Layout

```
~/.aws/cli-agent-orchestrator/memory/
├── global/
│   └── wiki/
│       ├── index.md              # index of all global/session/agent memories
│       ├── global/
│       │   └── {key}.md
│       ├── session/
│       │   └── {key}.md
│       └── agent/
│           └── {key}.md
└── {cwd_hash}/                   # e.g. 14ae6bda7bac
    └── wiki/
        ├── index.md              # index of this project's memories
        └── project/
            └── {key}.md
```

Each wiki file is a markdown document with YAML-like comment header and timestamped entries:

```markdown
# testing-framework
<!-- id: abc123 | scope: project | type: feedback | tags: testing,pytest -->

## 2026-04-16T10:30:00Z
Always use pytest for testing in this project. Do not use unittest.
```

## Retention

| Scope | Retention |
|---|---|
| `global` | Never expires |
| `project` | 90 days since last update |
| `session` | 14 days |
| `agent` | Never expires |

Cleanup runs automatically in the background when `cao-server` starts.

## Adding Memory Instructions to an Agent Profile

Add a `## Memory` section to the agent's system prompt:

```markdown
## Memory

When you discover something worth remembering — user preferences, project conventions,
important decisions, recurring corrections — store it immediately using the `memory_store`
CAO tool. Keep each memory to 1–2 sentences. Store decisions and conclusions, not conversation.
Use `memory_recall` to check if you already know something before asking the user.

Note: `memory_store` and `memory_recall` are CAO's cross-provider memory tools, distinct from
any provider-native memory system.
```
