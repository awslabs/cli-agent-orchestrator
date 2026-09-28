# External Tool Integration

CAO skills follow the universal [SKILL.md](https://github.com/anthropics/skills) format. Any LLM harness that reads this format can consume CAO skills directly — no conversion needed.

Any tool that loads SKILL.md files can consume these skills. [OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/NousResearch/hermes-agent) are both used as worked examples below; the same approach works for any compatible harness.

## What This Enables

By adding the `cao-session-management` skill to an external tool, the agent in that tool can orchestrate CAO sessions via shell commands — launching supervisor agents, sending tasks, and collecting results without leaving its own chat loop. The companion `cao-session-liveness` skill teaches the same agent how to confirm a session is genuinely alive before it reports progress.

The agent can:

- **Launch a CAO session** with a specific agent profile and initial task
- **Send work synchronously** — block until the CAO agent completes and return results inline
- **Send work asynchronously** — fire a task and continue, check back later for status
- **Monitor sessions** — list active sessions, check worker status, retrieve output
- **Verify liveness** — corroborate a reported status against terminal output, and recognize a session whose provider has already exited
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
TARGET_SKILLS=~/.openclaw/workspace/skills          # OpenClaw example
# TARGET_SKILLS=~/.hermes/skills/cli-agent-orchestrator   # Hermes Agent example
mkdir -p "$TARGET_SKILLS"

# Symlink the session management skill and its liveness companion
for skill in cao-session-management cao-session-liveness; do
  ln -sf ~/.aws/cli-agent-orchestrator/skills/"$skill" "$TARGET_SKILLS/$skill"
done
```

Each skill is a self-contained `<skill-name>/SKILL.md` folder, which is the layout
both hosts expect: OpenClaw loads them flat under its skill directory, and Hermes
Agent nests the same folders under a category directory.

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

For Hermes Agent specifically, the agent can register each skill under a category
directory, which produces `~/.hermes/skills/cli-agent-orchestrator/<skill-name>/`:

```python
from pathlib import Path

for skill in ("cao-session-management", "cao-session-liveness"):
    source = Path(f"~/.aws/cli-agent-orchestrator/skills/{skill}/SKILL.md").expanduser()
    skill_manage(
        action="create",
        name=skill,
        category="cli-agent-orchestrator",
        content=source.read_text(),
    )
```

Note that Option C creates a copy that will go stale on CAO upgrades — prefer Option A (symlink) when a shared filesystem is available.

## Scope

This gives the external agent **knowledge** of how to drive CAO via `cao session` shell commands. It does not add CAO as a live MCP server — the agent invokes shell commands, not MCP tools. If direct MCP access is needed, add `cao-ops-mcp-server` to the target tool's MCP configuration instead.

## Related

- [Skills reference](skills.md) — authoring, CLI commands, provider delivery
- [Control Planes](control-planes.md) — choosing between CLI, MCP, and Web UI surfaces
