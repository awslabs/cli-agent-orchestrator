# Settings

This document has moved. See [configuration.md](configuration.md) for the unified `settings.json` schema and [Obsidian Vault](obsidian-vault.md) for vault configuration.

For the on-demand memory ranking diagnostic, see [Memory explain](memory.md#on-demand-relevance-explain). The `memory.explain_enabled` setting defaults to `true`, with `CAO_MEMORY_EXPLAIN_ENABLED` taking precedence.

`memory.lifecycle.enabled` defaults to `false`; see [Supersedes lifecycle](memory.md#supersedes-lifecycle-opt-in). `CAO_MEMORY_LIFECYCLE_ENABLED` overrides the nested file value.

`memory.injection.clear_on_empty` and `memory.injection.v1_fixes` default to `true`. Set either key to `false` with `cao config set` to disable that injection-hygiene behavior.

`memory.non_accessing_inspection` defaults to `false`; see [Operator inspection](memory.md#non-accessing-operator-inspection-opt-in). `CAO_MEMORY_NON_ACCESSING_INSPECTION` overrides the file value.
