# Settings

CAO stores user configuration in `~/.aws/cli-agent-orchestrator/settings.json`. This file is managed by the settings service and can be edited via the Web UI Settings page or the REST API.

## Agent Profile Directories

CAO discovers agent profiles by scanning multiple directories. When loading or listing profiles, directories are scanned in this order (first match wins):

1. **Local store** — `~/.aws/cli-agent-orchestrator/agent-store/`
2. **Provider-specific directories** — Configured per provider (see defaults below)
3. **Extra custom directories** — User-added paths
4. **Built-in store** — Bundled with the CAO package

### Default Directories

| Key | Provider | Default Path |
|-----|----------|-------------|
| `kiro_cli` | Kiro CLI | `~/.kiro/agents` |
| `q_cli` | Q CLI | `~/.aws/amazonq/cli-agents` |
| `claude_code` | Claude Code | `~/.aws/cli-agent-orchestrator/agent-store` |
| `codex` | Codex | `~/.aws/cli-agent-orchestrator/agent-store` |
| `cao_installed` | CAO Installed | `~/.aws/cli-agent-orchestrator/agent-context` |

The `cao_installed` directory is where `cao install` places agent profiles. This keeps installed profiles separate from hand-authored ones in `agent-store`.

### Overriding Directories

Override any provider directory via the REST API or Web UI Settings page:

```bash
# Via REST API
curl -X POST http://localhost:9889/settings/agent-dirs \
  -H "Content-Type: application/json" \
  -d '{"kiro_cli": "/custom/path/to/agents"}'
```

Or edit `settings.json` directly:

```json
{
  "agent_dirs": {
    "kiro_cli": "/custom/path/to/agents"
  }
}
```

Only specified providers are updated; others retain their defaults.

### Extra Directories

Add additional directories that are scanned for agent profiles across all providers:

```json
{
  "extra_agent_dirs": [
    "/path/to/team-shared-agents",
    "/path/to/project-specific-agents"
  ]
}
```

## settings.json Format

```json
{
  "agent_dirs": {
    "kiro_cli": "~/.kiro/agents",
    "q_cli": "~/.aws/amazonq/cli-agents",
    "claude_code": "~/.aws/cli-agent-orchestrator/agent-store",
    "codex": "~/.aws/cli-agent-orchestrator/agent-store",
    "cao_installed": "~/.aws/cli-agent-orchestrator/agent-context"
  },
  "extra_agent_dirs": []
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/settings/agent-dirs` | Get current agent directories (merged with defaults) |
| `POST` | `/settings/agent-dirs` | Update agent directories |
| `GET` | `/settings/extra-agent-dirs` | Get extra custom directories |
| `POST` | `/settings/extra-agent-dirs` | Set extra custom directories |

See [api.md](api.md) for the full API reference.
