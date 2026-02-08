# Ralph Agent Configuration

## Build Instructions

```bash
# Install dependencies
uv sync
```

## Test Instructions

```bash
# Run all unit tests (excludes E2E and Q CLI integration)
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py

# Run <provider> unit tests with coverage
uv run pytest test/providers/test_<provider>_unit.py -v --cov=src/cli_agent_orchestrator/providers/<provider>.py --cov-report=term-missing

# Code quality
uv run black --check src/ test/
uv run isort --check-only src/ test/
uv run mypy src/cli_agent_orchestrator/providers/<provider>.py
```

## Run Instructions

```bash
# Start CAO server
uv run cao-server

# Launch with <Provider> provider
uv run cao launch --agents code_supervisor --provider <provider>
```

## Notes
- Use `uv run` prefix for all Python commands
- Full unit test suite should pass before any changes
- Target >90% coverage on provider modules
- Do NOT commit unless explicitly instructed by user
