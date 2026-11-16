# Changelog — `cao-ag-ui-builder`

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

- `agui-author`

Excluded: every operator skill (in the `cao` package), every contributor skill (in `cao-contributor`) and the Agent Plugin lifecycle skills (in `cao-agent-plugin-lifecycle`); `cao-mcp-apps` and `mcp-apps-builder` (MCP Apps is a different UI surface with its own consumers).
