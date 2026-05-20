# External Tool Integration

CAO skills follow the universal [SKILL.md](https://github.com/anthropics/skills) format. Any LLM harness that reads this format can consume CAO skills directly — no conversion needed.

Any tool that loads SKILL.md files can consume these skills — [OpenClaw](https://github.com/openclaw/openclaw) is used as the example below, but the same approach works for any compatible harness.

## What This Enables

By adding the `cao-session-management` skill to an external tool, the agent in that tool can orchestrate CAO sessions via shell commands — launching supervisor agents, sending tasks, and collecting results without leaving its own chat loop.

The agent can:

- **Launch a CAO session** with a specific agent profile and initial task
- **Send work synchronously** — block until the CAO agent completes and return results inline
- **Send work asynchronously** — fire a task and continue, check back later for status
- **Monitor sessions** — list active sessions, check worker status, retrieve output
- **Shut down sessions** — clean up when done

This turns any SKILL.md-compatible tool into an orchestration client for CAO's multi-agent system.

## Prerequisites

- CAO installed and initialized (`cao init`)
- `cao-server` running (`cao-server`)
- Target tool installed with a writable skill directory
- Shared filesystem (symlinks require both on the same machine)

## Setup

### Option A: Symlink (recommended)

```bash
# Replace TARGET_SKILLS with your tool's skill directory
TARGET_SKILLS=~/.openclaw/workspace/skills   # OpenClaw example
mkdir -p "$TARGET_SKILLS"

# Symlink the session management skill
ln -sf ~/.aws/cli-agent-orchestrator/skills/cao-session-management \
       "$TARGET_SKILLS/cao-session-management"
```

Symlinks stay in sync with CAO upgrades automatically.

### Option B: Point your tool at CAO's skill directory

Some tools can load skills from additional directories without copying or symlinking. For OpenClaw, add CAO's skill store as an extra skill root in `~/.openclaw/openclaw.json`:

```json5
{
  skills: {
    load: {
      extraDirs: ["~/.aws/cli-agent-orchestrator/skills"]
    }
  }
}
```

This makes all CAO skills visible to OpenClaw agents with zero file operations.

### Option C: Ask the agent to install it

If the external tool's agent has filesystem access, tell it to install the skill directly:

> Install the skill from ~/.aws/cli-agent-orchestrator/skills/cao-session-management into your skills directory

The agent will read the SKILL.md, copy the folder into its own workspace, and make it available for future sessions.

## Scope

This gives the external agent **knowledge** of how to drive CAO via `cao session` shell commands. It does not add CAO as a live MCP server — the agent invokes shell commands, not MCP tools. If direct MCP access is needed, add `cao-ops-mcp-server` to the target tool's MCP configuration instead.

## Related

- [Skills reference](skills.md) — authoring, CLI commands, provider delivery
- [Control Planes](control-planes.md) — choosing between CLI, MCP, and Web UI surfaces
