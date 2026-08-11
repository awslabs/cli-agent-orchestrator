# Changelog — `cao-contributor`

This file is **generated** by `scripts/build_agent_plugin.py`. Edit the package
configuration in that script, not this file; `make check-agent-plugin` fails on
any hand edit.

## 2.4.1

Targets the [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
specification. Version is synced from CAO's own package metadata, so the plugin
and the CAO release it corresponds to cannot diverge.

Skills:

- `cao-provider`
- `cao-plugin`

Excluded: every operator-facing skill (they belong in the `cao` package), and the adjacent-feature and vendored skills excluded there for the same reasons. `cao-contributing` is conditional on PR #448 (open, draft) and is not present; adding it is a one-line allowlist edit in scripts/build_agent_plugin.py.
