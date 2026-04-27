# cao-memory-kiro-cli

CAO plugin that injects the persistent memory context into Kiro CLI
terminals on creation. Replaces the provider-side `agentSpawn` +
`userPromptSubmit` hooks.

Writes `<cwd>/.kiro/steering/cao-memory.md` with the full `<cao-memory>`
block on `post_create_terminal` when the provider is `kiro_cli`. Kiro
CLI loads every file in `.kiro/steering/` as persistent project-level
instructions, so the memory block becomes part of the agent's context
for the session.

This file is **separate from** `.kiro/steering/agent-identity.md`,
which is still written by CAO's terminal service (Phase 2 U7). Two
files, two owners, no shared state.

## Install

From a CAO checkout:

```
uv pip install -e examples/plugins/cao-memory-kiro-cli
```

Then restart `cao-server`.

## Scope

- Triggers only when `event.provider == "kiro_cli"`. Other providers
  are ignored silently.
- Write target is path-validated: resolved `.kiro/steering/cao-memory.md`
  must sit inside the resolved tmux pane working directory. Symlink
  escape is rejected.
- Does not read, modify, or delete `agent-identity.md` — that file is
  owned by `terminal_service._write_kiro_steering_file`.
- All failures (metadata missing, memory fetch error, write error) are
  logged at WARNING and do not crash `cao-server`.

## Limitations

- Memory context is written once, at terminal creation. Mid-session
  memory changes are not reflected until the next terminal spawn.
- No periodic-reminder replacement; that gap is expected to close when
  CAO core ships a `post_user_prompt` event.
