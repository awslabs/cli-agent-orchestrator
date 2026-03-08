# Cross-Provider Workflow

Allow agent profiles to declare which provider they run on, enabling a supervisor on one provider (e.g., Kiro CLI) to delegate tasks to workers running on a different provider (e.g., Claude Code).

## Motivation

Today, every terminal spawned via `assign` inherits the provider of the calling terminal. A `cao launch --provider claude_code` session produces only Claude Code workers, regardless of what each worker's agent profile is designed for. This prevents mixed-provider workflows where different agents are best suited to different providers.

## Design

### Agent Profile `provider` Key

Agent profiles gain an optional `provider` key in their YAML frontmatter:

```markdown
---
name: developer
description: Developer Agent
provider: claude_code
---

# System prompt content...
```

Valid values are the `ProviderType` enum strings: `q_cli`, `kiro_cli`, `claude_code`, `codex`.

### Provider Resolution Logic

A new `resolve_provider()` utility determines the effective provider when creating a terminal. Resolution follows a two-tier priority:

| Scenario | Provider used |
|----------|--------------|
| Profile has valid `provider` key | Profile's provider |
| Profile has no `provider` key | Fallback (inherited from caller) |
| Profile has invalid `provider` value | Fallback (inherited from caller) + warning logged |
| Profile not found in agent store | Fallback (inherited from caller) |

### `cao launch` Override

The `cao launch --provider` flag is treated as an **explicit override** and always takes precedence over the profile's `provider` key. This ensures the CLI user retains full control when launching the initial session.

The MCP-initiated `assign` flow does **not** have this override behavior — it always respects the profile's `provider` key when present, falling back to the caller's provider otherwise.

### Flow Comparison

#### Current Flow (assign)

```
MCP: assign(agent_profile="developer")
  → _create_terminal()
    → GET /terminals/{id}  →  provider = supervisor's provider
    → POST .../terminals?provider=<supervisor's>&agent_profile=developer
      → terminal_service.create_terminal(provider=<supervisor's>, ...)
```

#### New Flow (assign)

```
MCP: assign(agent_profile="developer")
  → _create_terminal()
    → GET /terminals/{id}  →  fallback_provider = supervisor's provider
    → POST .../terminals?provider=<supervisor's>&agent_profile=developer
      → resolve_provider("developer", fallback=<supervisor's>)
        → load_agent_profile("developer")
        → profile.provider = "claude_code" (valid) → use it
      → terminal_service.create_terminal(provider="claude_code", ...)
```

#### `cao launch` (explicit override preserved)

```
cao launch --agents supervisor --provider kiro_cli
  → POST /sessions?provider=kiro_cli&agent_profile=supervisor
    → terminal_service.create_terminal(provider=kiro_cli, ...)
      # Profile's provider key is NOT consulted — CLI flag wins
```

## Implementation

### Touched Files

| File | Change |
|------|--------|
| `models/agent_profile.py` | Add `provider: Optional[str] = None` field |
| `utils/agent_profiles.py` | Add `resolve_provider()` function |
| `api/main.py` | Resolve provider in `create_terminal_in_session`; skip resolution in `create_session` |

The MCP server (`server.py`), terminal service, provider manager, and provider implementations require **no changes**. They continue to receive an already-resolved provider string.

### 1. `models/agent_profile.py`

Add an optional `provider` field to `AgentProfile`:

```python
class AgentProfile(BaseModel):
    """Agent profile configuration with Q CLI agent fields."""

    name: str
    description: str
    provider: Optional[str] = None       # ← new field
    system_prompt: Optional[str] = None
    # ... rest unchanged
```

### 2. `utils/agent_profiles.py`

Add `resolve_provider()` next to the existing `load_agent_profile()`:

```python
import logging
from cli_agent_orchestrator.constants import PROVIDERS

logger = logging.getLogger(__name__)


def resolve_provider(agent_profile_name: str, fallback_provider: str) -> str:
    """Resolve the provider for an agent profile.

    Loads the agent profile from the CAO agent store and checks for a
    `provider` key. If present and valid, returns the profile's provider.
    Otherwise returns the fallback provider.

    Args:
        agent_profile_name: Name of the agent profile to look up.
        fallback_provider: Provider to use if the profile doesn't specify
            one or specifies an invalid value.

    Returns:
        Resolved provider type string.
    """
    try:
        profile = load_agent_profile(agent_profile_name)
    except RuntimeError:
        # Profile not found — provider.initialize() will surface
        # a clear error later. Fall back for now.
        return fallback_provider

    if profile.provider:
        if profile.provider in PROVIDERS:
            return profile.provider
        else:
            logger.warning(
                f"Agent profile '{agent_profile_name}' has invalid provider "
                f"'{profile.provider}'. Valid providers: {PROVIDERS}. "
                f"Falling back to '{fallback_provider}'."
            )

    return fallback_provider
```

### 3. `api/main.py`

Apply resolution only in `create_terminal_in_session` (the path used by `assign`). Skip resolution in `create_session` (the path used by `cao launch`) so that the CLI flag remains an explicit override.

```python
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider


@app.post("/sessions/{session_name}/terminals", ...)
async def create_terminal_in_session(
    session_name: str,
    provider: str,
    agent_profile: str,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create additional terminal in existing session."""
    try:
        resolved_provider = resolve_provider(agent_profile, fallback_provider=provider)

        result = terminal_service.create_terminal(
            provider=resolved_provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=False,
            working_directory=working_directory,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create terminal: {str(e)}",
        )
```

`create_session` remains **unchanged** — it passes `provider` straight through.

### Tests

New test cases should cover:

1. **Profile with valid `provider`** — `resolve_provider("agent-with-claude", "kiro_cli")` returns `"claude_code"`
2. **Profile without `provider` key** — falls back to the supplied fallback
3. **Profile with invalid `provider` value** — falls back + warning logged
4. **Profile not found** — falls back without error
5. **`create_terminal_in_session` API** — verify resolved provider is used in the created terminal
6. **`create_session` API** — verify profile `provider` key is ignored and CLI flag provider is used

### Migration

No database schema changes. No breaking changes to existing agent profiles — the `provider` key is optional and existing profiles without it behave identically to today.

## Example: Mixed-Provider Workflow

```markdown
# supervisor.md (runs on Kiro CLI)
---
name: supervisor
description: Code Supervisor
provider: kiro_cli
---
You orchestrate tasks across developer and reviewer agents.
```

```markdown
# developer.md (runs on Claude Code)
---
name: developer
description: Developer Agent
provider: claude_code
---
You write code based on specifications.
```

```markdown
# reviewer.md (runs on Kiro CLI, inherits from supervisor)
---
name: reviewer
description: Code Reviewer
---
You review code for quality and correctness.
```

```bash
cao launch --agents supervisor --provider kiro_cli
```

- Supervisor starts on Kiro CLI (explicit `--provider` flag)
- `assign(agent_profile="developer")` → resolves to `claude_code` (from profile)
- `assign(agent_profile="reviewer")` → falls back to `kiro_cli` (no profile key, inherits from supervisor)
