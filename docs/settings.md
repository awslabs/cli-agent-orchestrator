# Settings

This document has moved. See [configuration.md](configuration.md) for the unified `settings.json` schema and [Obsidian Vault](obsidian-vault.md) for vault configuration.

## Ephemeral agents

The operator-only `ephemeral` block configures `enabled` (off by default),
`allowed_providers`, `max_brief_bytes`, `pending_ttl_seconds`,
`claim_lease_seconds`, and `max_depth`. `child_may_delegate` controls the existing
ephemeral delegation and workflow gates; it never permits ephemeral creation
by an ephemeral caller. See [Ephemeral agents](ephemeral-agents.md) for the
defaults, creation limits, and persistent-file warning.
