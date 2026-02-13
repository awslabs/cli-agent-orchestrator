# Test Implementation Plans

Each file contains a self-contained test implementation plan that can be assigned to an agent.

## Files

| File | Module | Priority | Est. Tests |
|------|--------|----------|------------|
| [01-services.md](01-services.md) | Services layer | HIGH | 55 |
| [02-clients.md](02-clients.md) | Database & Tmux clients | HIGH | 38 |
| [03-mcp-server.md](03-mcp-server.md) | MCP Server | HIGH | 15 |
| [04-api.md](04-api.md) | FastAPI endpoints | HIGH | 15 |
| [05-cli.md](05-cli.md) | CLI commands | MEDIUM | 20 |
| [06-models.md](06-models.md) | Pydantic models | LOW | 15 |
| [07-providers.md](07-providers.md) | Provider implementations | LOW | 10 |
| [08-utils.md](08-utils.md) | Utility functions | LOW | 10 |

## Execution Order

1. `01-services.md` - Core business logic
2. `02-clients.md` - Infrastructure layer
3. `03-mcp-server.md` - MCP tools
4. `04-api.md` - REST API
5. `05-cli.md` - CLI commands
6. `06-models.md`, `07-providers.md`, `08-utils.md` - Can run in parallel

## Usage

Assign each file to an agent:

```
Implement the tests described in docs/test-plans/01-services.md
```

## Test Guidelines

- Use AAA pattern (Arrange, Act, Assert)
- Mock external dependencies
- Use `pytest.fixture` for shared setup
- Name tests: `test_<function>_<scenario>`
