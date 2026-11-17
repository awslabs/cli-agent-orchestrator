# Settings

This document has moved. See [configuration.md](configuration.md) for the unified `settings.json` schema and [Obsidian Vault](obsidian-vault.md) for vault configuration.

For the on-demand memory ranking diagnostic, see [Memory explain](memory.md#on-demand-relevance-explain). The `memory.explain_enabled` setting defaults to `true`, with `CAO_MEMORY_EXPLAIN_ENABLED` taking precedence.

`memory.parser.lenient` defaults to `true`; see [Native topic visibility](memory.md#native-topic-visibility). Set it to `false` to restore legacy parsing that skips topics without canonical entry headings.

`memory.lifecycle.enabled` defaults to `false`; see [Supersedes lifecycle](memory.md#supersedes-lifecycle-opt-in). `CAO_MEMORY_LIFECYCLE_ENABLED` overrides the nested file value.

`memory.ranking.default_sort` defaults to `recency`; set it to `bm25` to opt query recall into BM25-first results. No-query listings remain recency ordered.

`memory.non_accessing_inspection` defaults to `true`; see [Operator inspection](memory.md#non-accessing-operator-inspection). `CAO_MEMORY_NON_ACCESSING_INSPECTION` overrides the file value.
