# Installation Per Agent

Expanded instructions for installing the MCP Apps builder skills (`create-mcp-app`, `add-app-to-server`, `migrate-oai-app`, `convert-web-app`) on each supported agent.

## Claude Code (Plugin Marketplace)

**Skills directory:** `~/.claude/skills/`

```bash
# Via the plugin marketplace (recommended)
/plugin marketplace add modelcontextprotocol/ext-apps
/plugin install mcp-apps@modelcontextprotocol-ext-apps

# Verify
/plugin list   # should show mcp-apps@modelcontextprotocol-ext-apps
```

**Manual alternative:**
```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.claude/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.claude/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.claude/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.claude/skills/convert-web-app
```

**Common issues:**
- Skills not found after install → restart the Claude Code session
- Plugin marketplace unavailable → use the manual copy method above

## Kiro CLI

**Skills directory:** `~/.kiro/skills/`

```bash
# Via Vercel Skills CLI
npx skills add modelcontextprotocol/ext-apps

# Manual
git clone https://github.com/modelcontextprotocol/ext-apps.git
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.kiro/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.kiro/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.kiro/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.kiro/skills/convert-web-app
```

**Verify:** Ask Kiro "what skills do you have?" — it should list the four MCP Apps skills.

## VS Code / GitHub Copilot

**Skills directory:** `~/.copilot/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.copilot/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.copilot/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.copilot/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.copilot/skills/convert-web-app
```

**Common issues:**
- Skills not loaded → ensure the skills directory is at `~/.copilot/skills/`, not a project-local path
- Copilot ignoring skill → try referencing it explicitly: "use the create-mcp-app skill"

## Gemini CLI

**Skills directory:** `~/.gemini/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.gemini/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.gemini/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.gemini/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.gemini/skills/convert-web-app
```

**Verify:** `ls ~/.gemini/skills/` should show all four skill directories.

## Codex CLI

**Skills directory:** `~/.codex/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.codex/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.codex/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.codex/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.codex/skills/convert-web-app
```

## Cursor

**Skills directory:** `~/.cursor/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.cursor/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.cursor/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.cursor/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.cursor/skills/convert-web-app
```

## Goose

**Skills directory:** `~/.config/goose/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.config/goose/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.config/goose/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.config/goose/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.config/goose/skills/convert-web-app
```

## OpenCode

**Skills directory:** `~/.opencode/skills/`

```bash
cp -r ext-apps/plugins/mcp-apps/skills/create-mcp-app ~/.opencode/skills/create-mcp-app
cp -r ext-apps/plugins/mcp-apps/skills/add-app-to-server ~/.opencode/skills/add-app-to-server
cp -r ext-apps/plugins/mcp-apps/skills/migrate-oai-app ~/.opencode/skills/migrate-oai-app
cp -r ext-apps/plugins/mcp-apps/skills/convert-web-app ~/.opencode/skills/convert-web-app
```

## Universal: Vercel Skills CLI

Works with any agent that supports the skills directory convention:

```bash
npx skills add modelcontextprotocol/ext-apps
```

This clones the ext-apps repo and copies the skills into the detected agent's skills directory.

## Verification (all agents)

After installation, ask the agent any of these:
- "What skills do you have?"
- "Build an MCP App" (should trigger `create-mcp-app`)
- "Add a UI to my MCP server" (should trigger `add-app-to-server`)

If the agent doesn't find the skill, check:
1. The skill directory path matches the agent's expected location
2. Each skill directory contains a valid `SKILL.md` file
3. The agent session was restarted after installation
