# Changelog — `cao-contributor`

This file is **generated** by `scripts/build_agent_plugin.py`. Edit the package
configuration in that script, not this file; `make check-agent-plugin` fails on
any hand edit.

## 2.5.3

Targets the [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
specification.

This package is pinned to CAO v2.5.3; see
[its release notes](https://github.com/awslabs/cli-agent-orchestrator/releases/tag/v2.5.3).
Between releases the skills here are rebuilt from `main`, so they can be newer
than that release. The release workflow re-pins after publishing, which is why
the committed pin lags by one release by construction.

Skills:

- `cao-provider`
- `cao-plugin`
- `cao-contributing`

Excluded: every operator-facing skill (they belong in the `cao` package), and the adjacent-feature and vendored skills excluded there for the same reasons.
