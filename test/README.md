# Test Suite for CLI Agent Orchestrator

This directory contains the unit and integration tests for the CLI Agent Orchestrator.

## Prerequisites

### Required Packages

Install the required packages using pip:

```bash
pip install pytest pytest-cov pytest-asyncio
```

Or install all development dependencies:

```bash
pip install -e ".[dev]"
```

### Package Installation

Before running tests, install the package in editable mode:

```bash
cd cli-agent-orchestrator
pip install -e .
```

## Running Tests

### Run All Tests

```bash
pytest test/
```

### Run Tests with Verbose Output

```bash
pytest test/ -v
```

### Run Tests with Coverage Report

```bash
pytest test/ --cov=src --cov-report=term-missing
```

### Run Specific Test File

```bash
pytest test/providers/test_kiro_cli_unit.py -v
```

### Run Specific Test Class

```bash
pytest test/providers/test_kiro_cli_unit.py::TestKiroCliProvider -v
```

### Run Specific Test Method

```bash
pytest test/providers/test_kiro_cli_unit.py::TestKiroCliProvider::test_init -v
```

### Skip Integration Tests

Integration tests require actual CLI tools to be installed. To skip them:

```bash
pytest test/ -v --ignore=test/providers/test_q_cli_integration.py
```

## Test Organization

```
test/
├── README.md              # This file
├── api/                   # API endpoint tests
│   ├── test_inbox_messages.py
│   └── test_terminals.py
├── cli/                   # CLI command tests
│   ├── test_main.py
│   └── commands/
│       ├── test_flow.py
│       ├── test_init.py
│       ├── test_install.py
│       ├── test_launch.py
│       └── test_shutdown.py
├── clients/               # Client tests
│   ├── test_database.py
│   └── test_tmux_send_keys.py
├── mcp_server/            # MCP server tests
│   └── test_models.py
├── models/                # Model tests
│   └── test_session.py
├── providers/             # Provider tests
│   ├── test_base_provider.py
│   ├── test_claude_code_unit.py
│   ├── test_codex_provider_unit.py
│   ├── test_kiro_cli_unit.py
│   ├── test_provider_manager_unit.py
│   └── test_q_cli_unit.py
├── services/              # Service tests
│   ├── test_inbox_service.py
│   ├── test_session_service.py
│   └── test_terminal_service.py
└── utils/                 # Utility tests
    ├── test_logging.py
    ├── test_template.py
    └── test_terminal.py
```

## Coverage Goals

The project aims for >90% test coverage for core modules.

### Current Coverage Status (467 tests passing)

**Modules at 100% Coverage:**
- `cli/commands/` - All CLI commands (flow, init, install, launch, shutdown)
- `constants.py` - Configuration constants
- `mcp_server/models.py`, `mcp_server/utils.py` - MCP models and utilities
- `models/` - All Pydantic models
- `providers/` - All provider implementations (claude_code, codex, kiro_cli, q_cli)
- `services/inbox_service.py`, `services/session_service.py` - Core services
- `utils/` - All utility modules (agent_profiles, logging, template, terminal)

**Modules at 90%+ Coverage:**
- `cli/main.py` (93%) - Main CLI entry point
- `providers/manager.py` (96%) - Provider manager
- `services/terminal_service.py` (95%) - Terminal service

### Files with Limited Test Coverage (Justified)

Some files have limited test coverage due to their nature:

| Module | Coverage | Justification |
|--------|----------|---------------|
| **mcp_server/server.py** | 0% | Requires MCP protocol runtime environment. The MCP server runs as a separate process and communicates via the MCP protocol. Testing requires mocking the entire MCP communication layer, which is better handled by integration tests with actual MCP clients. |
| **clients/tmux.py** | ~30% | Requires real tmux sessions for full coverage. Core `send_keys` behavior (literal mode, chunking) is unit-tested via `test_tmux_send_keys.py`. Operations like session creation and history capture are better covered by integration tests. |
| **api/main.py** | 44% | FastAPI endpoints require async testing setup with TestClient and running event loops. Endpoints interact with the database, tmux sessions, and providers simultaneously. Better tested via end-to-end integration tests. |
| **services/cleanup_service.py** | 20% | Background cleanup service that runs in a separate thread, monitoring and cleaning up stale sessions. Requires running processes and real session state to test cleanup logic. |
| **services/flow_service.py** | 25% | Flow orchestration service that manages complex multi-step agent interactions. Requires complex runtime state including active sessions, message queues, and provider instances. |
| **clients/database.py** | 80% | Database operations with some edge cases (transaction rollbacks, concurrent access) difficult to test without full database integration. Core CRUD operations are tested. |
| **providers/base.py** | 81% | Abstract base class with abstract methods that must be implemented by subclasses. The abstract methods themselves cannot be tested directly. All concrete implementations are at 100%. |

## Writing New Tests

### Test File Naming

- Unit tests: `test_<module_name>.py` or `test_<module_name>_unit.py`
- Integration tests: `test_<module_name>_integration.py`

### Test Class Naming

```python
class TestClassName:
    """Tests for ClassName."""

    def test_method_name(self):
        """Test specific method or behavior."""
        pass
```

### Using Mocks

Most unit tests use mocks to isolate the code under test:

```python
from unittest.mock import MagicMock, patch

@patch("cli_agent_orchestrator.providers.kiro_cli.TmuxClient")
def test_with_mock(self, mock_tmux):
    mock_tmux_instance = MagicMock()
    mock_tmux.return_value = mock_tmux_instance
    # ... test code
```

## Troubleshooting

### ModuleNotFoundError

If you see `ModuleNotFoundError: No module named 'cli_agent_orchestrator'`:

```bash
pip install -e .
```

### pytest-cov not found

```bash
pip install pytest-cov
```

### Q CLI Integration Tests Failing

Q CLI integration tests require the Q CLI tool to be installed and authenticated. These tests are expected to fail if Q CLI is not available. Skip them with:

```bash
pytest test/ --ignore=test/providers/test_q_cli_integration.py
```
