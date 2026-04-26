# cao-memory-claude-code

CAO plugin that injects the persistent memory context into Claude Code
terminals on creation. Replaces the provider-side `Stop` / `PreCompact`
hooks.

Writes `<cwd>/.claude/CLAUDE.md` with a delimited `<cao-memory>` section
on `post_create_terminal` when the provider is `claude_code`. Existing
CLAUDE.md content is preserved; prior plugin blocks are replaced.

## Install

From a CAO checkout:

```
uv pip install -e examples/plugins/cao-memory-claude-code
```

Then restart `cao-server` — plugins are discovered at startup via the
`cao.plugins` entry point group.

## Scope

- Triggers only when `event.provider == "claude_code"`. Other providers
  are ignored silently.
- Write target is path-validated: resolved `.claude/CLAUDE.md` must sit
  inside the resolved tmux pane working directory. Symlink-escape is
  rejected.
- All failures (metadata missing, memory fetch error, write error) are
  logged at WARNING and do not crash `cao-server`.

## Limitations

- Memory context is written once, at terminal creation. Mid-session
  memory changes are not reflected until the next terminal spawn.
- No periodic-reminder replacement; that gap is expected to close when
  CAO core ships a `post_user_prompt` or `post_agent_idle` event.
