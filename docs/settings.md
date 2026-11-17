# Settings

This document has moved. See [configuration.md](configuration.md) for the unified `settings.json` schema and [Obsidian Vault](obsidian-vault.md) for vault configuration.

For the on-demand memory ranking diagnostic, see [Memory explain](memory.md#on-demand-relevance-explain). The `memory.explain_enabled` setting defaults to `true`, with `CAO_MEMORY_EXPLAIN_ENABLED` taking precedence.

`memory.parser.lenient` defaults to `true`; see [Native topic visibility](memory.md#native-topic-visibility). Set it to `false` to restore legacy parsing that skips topics without canonical entry headings.

`memory.lifecycle.enabled` defaults to `false`; see [Supersedes lifecycle](memory.md#supersedes-lifecycle-opt-in). `CAO_MEMORY_LIFECYCLE_ENABLED` overrides the nested file value.

`memory.injection.clear_on_empty` and `memory.injection.v1_fixes` default to `true`. Set either key to `false` with `cao config set` to disable that injection-hygiene behavior. `memory.injection.write_project_files` defaults to `false`; set it to `true` to opt into legacy provider memory writes under the terminal working directory.

`memory.ranking.default_sort` defaults to `recency`; set it to `bm25` to opt query recall into BM25-first results. No-query listings remain recency ordered.

`memory.relationships.content_links` defaults to `false`; set it to `true` to
opt into future store and repair operations producing relationships from
authored memory links. `CAO_MEMORY_RELATIONSHIPS_CONTENT_LINKS` overrides the
file value. Disabling does not purge existing edges, and `memory_related`
continues to show any existing human, compiler, or content-link edges; see
[Typed relationships](memory.md#typed-relationships).

`memory.supersede.auto` defaults to `true`; set it to `false` to stop memory
stores from superseding the notes they replace. See
[Automatic supersede and undo](memory.md#automatic-supersede-and-undo).

`memory.non_accessing_inspection` defaults to `true`; see [Operator inspection](memory.md#non-accessing-operator-inspection). `CAO_MEMORY_NON_ACCESSING_INSPECTION` overrides the file value.
