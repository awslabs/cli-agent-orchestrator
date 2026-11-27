# AGENTS.md

CLI Agent Orchestrator (CAO) runs coding-agent CLIs in terminals and lets them hand work to each other.
It is a local server (`cao-server`) with a CLI (`cao`), an MCP server and a web UI.

## Feature map

[`docs/feature-map/`](docs/feature-map/README.md) has one short file per feature area. Each answers
four questions: what exists, how a user reaches it, how to drive it (commands and expected output),
and what usually lies (known traps). Open the file for the area you are changing, and run its
Drive step before you say the change works.

## Tests

```bash
uv sync
uv run pytest -v                  # default suite; e2e is deselected
uv run pytest -m "not e2e and not integration" -v   # fast inner loop
```

[DEVELOPMENT.md](DEVELOPMENT.md) has the details, including what CI runs and how the tests isolate
`HOME` and tmux. [CONTRIBUTING.md](CONTRIBUTING.md) covers the type-checking ratchet and PR rules.
[CODEBASE.md](CODEBASE.md) is the map of the source tree.

## Update rule

If a change alters how an area behaves, update that area's feature-map entry in the same PR.
A stale map is worse than none.
