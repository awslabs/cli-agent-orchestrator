# Unit Test Coverage Improvement Plan

## CLI Agent Orchestrator

*Last Updated: February 2025*

---

## Table of Contents

1. [Test File Mapping](#test-file-mapping)
2. [Implementation Plan](#implementation-plan)
3. [Test Guidelines](#test-guidelines)
4. [CI/CD Configuration](#cicd-configuration)

---

## Test File Mapping

### Complete Source-to-Test Mapping

Each source file maps to exactly one test file:

```
src/cli_agent_orchestrator/          →  test/
├── api/
│   └── main.py                      →  api/test_main.py
├── clients/
│   ├── database.py                  →  clients/test_database.py
│   └── tmux.py                      →  clients/test_tmux.py
├── cli/
│   ├── main.py                      →  cli/test_main.py
│   └── commands/
│       ├── flow.py                  →  cli/commands/test_flow.py
│       ├── init.py                  →  cli/commands/test_init.py
│       ├── install.py               →  cli/commands/test_install.py
│       ├── launch.py                →  cli/commands/test_launch.py (exists)
│       └── shutdown.py              →  cli/commands/test_shutdown.py
├── mcp_server/
│   ├── models.py                    →  mcp_server/test_models.py
│   ├── server.py                    →  mcp_server/test_server.py
│   └── utils.py                     →  mcp_server/test_utils.py
├── models/
│   ├── agent_profile.py             →  models/test_agent_profile.py
│   ├── flow.py                      →  models/test_flow.py
│   ├── inbox.py                     →  models/test_inbox.py
│   ├── kiro_agent.py                →  models/test_kiro_agent.py
│   ├── provider.py                  →  models/test_provider.py
│   ├── q_agent.py                   →  models/test_q_agent.py
│   ├── session.py                   →  models/test_session.py
│   └── terminal.py                  →  models/test_terminal.py
├── providers/
│   ├── base.py                      →  providers/test_base.py
│   ├── claude_code.py               →  providers/test_claude_code.py
│   ├── codex.py                     →  providers/test_codex.py (exists)
│   ├── kiro_cli.py                  →  providers/test_kiro_cli.py (exists)
│   ├── manager.py                   →  providers/test_manager.py (exists)
│   └── q_cli.py                     →  providers/test_q_cli.py (exists)
├── services/
│   ├── cleanup_service.py           →  services/test_cleanup_service.py
│   ├── flow_service.py              →  services/test_flow_service.py
│   ├── inbox_service.py             →  services/test_inbox_service.py
│   ├── session_service.py           →  services/test_session_service.py
│   └── terminal_service.py          →  services/test_terminal_service.py (exists)
├── utils/
│   ├── agent_profiles.py            →  utils/test_agent_profiles.py
│   ├── logging.py                   →  utils/test_logging.py
│   ├── template.py                  →  utils/test_template.py
│   └── terminal.py                  →  utils/test_terminal.py
└── constants.py                     →  test_constants.py
```

### Target Test Directory Structure

```
test/
├── __init__.py
├── conftest.py                      # Shared fixtures
├── test_constants.py                # NEW
├── api/
│   ├── __init__.py
│   ├── test_inbox_messages.py       # EXISTS
│   ├── test_main.py                 # NEW
│   └── test_terminals.py            # EXISTS
├── cli/
│   ├── __init__.py
│   ├── test_main.py                 # NEW
│   └── commands/
│       ├── __init__.py
│       ├── test_flow.py             # NEW
│       ├── test_init.py             # NEW
│       ├── test_install.py          # NEW
│       ├── test_launch.py           # EXISTS
│       └── test_shutdown.py         # NEW
├── clients/
│   ├── __init__.py
│   ├── test_database.py             # NEW
│   └── test_tmux.py                 # NEW
├── mcp_server/
│   ├── __init__.py
│   ├── test_models.py               # NEW
│   ├── test_server.py               # NEW
│   └── test_utils.py                # NEW
├── models/
│   ├── __init__.py
│   ├── test_agent_profile.py        # NEW
│   ├── test_flow.py                 # NEW
│   ├── test_inbox.py                # NEW
│   ├── test_kiro_agent.py           # NEW
│   ├── test_provider.py             # NEW
│   ├── test_q_agent.py              # NEW
│   ├── test_session.py              # NEW
│   └── test_terminal.py             # NEW
├── providers/
│   ├── __init__.py
│   ├── test_base.py                 # NEW
│   ├── test_claude_code.py          # NEW
│   ├── test_codex.py                # EXISTS
│   ├── test_kiro_cli.py             # EXISTS
│   ├── test_manager.py              # EXISTS
│   └── test_q_cli.py                # EXISTS
├── services/
│   ├── __init__.py
│   ├── test_cleanup_service.py      # NEW
│   ├── test_flow_service.py         # NEW
│   ├── test_inbox_service.py        # NEW
│   ├── test_session_service.py      # NEW
│   └── test_terminal_service.py     # EXISTS
└── utils/
    ├── __init__.py
    ├── test_agent_profiles.py       # NEW
    ├── test_logging.py              # NEW
    ├── test_template.py             # NEW
    └── test_terminal.py             # NEW
```

---

## Implementation Plan

### Phase 1: Services (Week 1-2) - HIGH PRIORITY

#### test/services/test_flow_service.py

| Source | `src/cli_agent_orchestrator/services/flow_service.py` (7561 LOC) |
|--------|------------------------------------------------------------------|
| Dependencies | `database.py`, `tmux.py`, `template.py` |

```python
# Test Cases
- test_add_flow_success
- test_add_flow_missing_name
- test_add_flow_missing_agent_profile
- test_add_flow_invalid_cron
- test_list_flows_empty
- test_list_flows_multiple
- test_get_flow_success
- test_get_flow_not_found
- test_remove_flow_success
- test_remove_flow_not_found
- test_enable_flow
- test_disable_flow
- test_execute_flow_no_script
- test_execute_flow_with_script_success
- test_execute_flow_script_returns_false
- test_execute_flow_script_fails
- test_get_flows_to_run
```

**Mocking Strategy:**
```python
@pytest.fixture
def mock_db():
    with patch("cli_agent_orchestrator.services.flow_service.db_client") as mock:
        yield mock

@pytest.fixture
def mock_tmux():
    with patch("cli_agent_orchestrator.services.flow_service.tmux_client") as mock:
        yield mock
```

---

#### test/services/test_terminal_service.py (EXPAND)

| Source | `src/cli_agent_orchestrator/services/terminal_service.py` (7758 LOC) |
|--------|----------------------------------------------------------------------|
| Dependencies | `database.py`, `tmux.py`, `providers/manager.py` |

```python
# Additional Test Cases
- test_create_terminal_new_session
- test_create_terminal_existing_session
- test_create_terminal_with_working_directory
- test_create_terminal_invalid_provider
- test_get_terminal_success
- test_get_terminal_not_found
- test_get_working_directory
- test_send_input_success
- test_send_input_terminal_not_found
- test_get_output_full_mode
- test_get_output_last_mode
- test_delete_terminal_success
- test_delete_terminal_stops_pipe_pane
- test_exit_terminal
```

---

#### test/services/test_session_service.py

| Source | `src/cli_agent_orchestrator/services/session_service.py` (2215 LOC) |
|--------|---------------------------------------------------------------------|
| Dependencies | `database.py`, `tmux.py` |

```python
# Test Cases
- test_list_sessions_empty
- test_list_sessions_filters_cao_prefix
- test_get_session_success
- test_get_session_not_found
- test_delete_session_success
- test_delete_session_with_terminals
- test_delete_session_not_found
```

---

#### test/services/test_inbox_service.py

| Source | `src/cli_agent_orchestrator/services/inbox_service.py` (4266 LOC) |
|--------|-------------------------------------------------------------------|
| Dependencies | `database.py`, `tmux.py` |

```python
# Test Cases
- test_get_log_tail_success
- test_get_log_tail_file_not_found
- test_has_idle_pattern_true
- test_has_idle_pattern_false_processing
- test_check_and_send_pending_no_messages
- test_check_and_send_pending_terminal_busy
- test_check_and_send_pending_success
- test_log_file_handler_on_modified
- test_log_file_handler_ignores_non_log
```

---

#### test/services/test_cleanup_service.py

| Source | `src/cli_agent_orchestrator/services/cleanup_service.py` (2251 LOC) |
|--------|---------------------------------------------------------------------|
| Dependencies | `database.py` |

```python
# Test Cases
- test_cleanup_old_terminals
- test_cleanup_old_inbox_messages
- test_cleanup_old_terminal_logs
- test_cleanup_old_server_logs
- test_cleanup_no_old_data
```

---

### Phase 2: Clients (Week 3-4) - HIGH PRIORITY

#### test/clients/test_database.py

| Source | `src/cli_agent_orchestrator/clients/database.py` (11917 LOC) |
|--------|--------------------------------------------------------------|
| Dependencies | SQLite (use in-memory for tests) |

```python
# Test Cases - Terminal Operations
- test_create_terminal
- test_get_terminal_metadata_success
- test_get_terminal_metadata_not_found
- test_list_terminals_by_session
- test_update_last_active
- test_delete_terminal
- test_delete_terminals_by_session

# Test Cases - Inbox Operations
- test_create_inbox_message
- test_get_pending_messages
- test_get_inbox_messages_with_status
- test_get_inbox_messages_with_limit
- test_update_message_status

# Test Cases - Flow Operations
- test_create_flow
- test_get_flow_success
- test_get_flow_not_found
- test_list_flows
- test_update_flow_run_times
- test_update_flow_enabled
- test_delete_flow
- test_get_flows_to_run
```

**Test Isolation Strategy:**
```python
@pytest.fixture
def test_db(tmp_path):
    """Create isolated test database."""
    db_path = tmp_path / "test.db"
    client = DatabaseClient(str(db_path))
    client.initialize()
    yield client
    client.close()
```

---

#### test/clients/test_tmux.py

| Source | `src/cli_agent_orchestrator/clients/tmux.py` (12168 LOC) |
|--------|----------------------------------------------------------|
| Dependencies | subprocess (mock all tmux calls) |

```python
# Test Cases
- test_resolve_working_directory_default
- test_resolve_working_directory_custom
- test_resolve_working_directory_invalid
- test_create_session_success
- test_create_session_with_working_directory
- test_create_window_success
- test_create_window_session_not_found
- test_send_keys_success
- test_send_keys_chunking_large_input
- test_get_history_success
- test_get_history_with_tail
- test_list_sessions
- test_get_session_windows
- test_kill_session
- test_session_exists_true
- test_session_exists_false
- test_get_pane_working_directory
- test_pipe_pane_success
- test_stop_pipe_pane
```

**Mocking Strategy:**
```python
@pytest.fixture
def mock_subprocess():
    with patch("subprocess.run") as mock:
        yield mock
```

---

### Phase 3: MCP Server (Week 5) - HIGH PRIORITY

#### test/mcp_server/test_server.py

| Source | `src/cli_agent_orchestrator/mcp_server/server.py` (16031 LOC) |
|--------|---------------------------------------------------------------|
| Dependencies | `terminal_service.py`, `inbox_service.py`, httpx |

```python
# Test Cases
- test_handoff_success
- test_handoff_timeout
- test_handoff_with_working_directory
- test_assign_success
- test_assign_with_working_directory
- test_assign_failure
- test_send_message_success
- test_send_message_no_terminal_id
- test_create_terminal_new_session
- test_create_terminal_existing_session
- test_send_direct_input
- test_send_to_inbox
```

---

#### test/mcp_server/test_models.py

| Source | `src/cli_agent_orchestrator/mcp_server/models.py` (517 LOC) |
|--------|-------------------------------------------------------------|

```python
# Test Cases
- test_handoff_request_validation
- test_assign_request_validation
- test_send_message_request_validation
```

---

#### test/mcp_server/test_utils.py

| Source | `src/cli_agent_orchestrator/mcp_server/utils.py` (479 LOC) |
|--------|-------------------------------------------------------------|

```python
# Test Cases
- test_get_terminal_id_from_env
- test_get_terminal_id_missing
```

---

### Phase 4: API (Week 6) - HIGH PRIORITY

#### test/api/test_main.py

| Source | `src/cli_agent_orchestrator/api/main.py` (14365 LOC) |
|--------|------------------------------------------------------|
| Dependencies | All services, FastAPI TestClient |

```python
# Test Cases - Session Endpoints
- test_create_session_success
- test_create_session_invalid_provider
- test_list_sessions
- test_get_session_success
- test_get_session_not_found
- test_delete_session_success

# Test Cases - Terminal Endpoints
- test_create_terminal_in_session
- test_list_terminals_in_session
- test_get_terminal_working_directory
- test_exit_terminal
- test_delete_terminal

# Test Cases - Health & Lifecycle
- test_health_check
- test_lifespan_startup
- test_flow_daemon_execution
```

**Test Setup:**
```python
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from cli_agent_orchestrator.api.main import app
    with TestClient(app) as c:
        yield c
```

---

### Phase 5: CLI Commands (Week 7) - MEDIUM PRIORITY

#### test/cli/commands/test_flow.py

| Source | `src/cli_agent_orchestrator/cli/commands/flow.py` (2776 LOC) |
|--------|--------------------------------------------------------------|

```python
# Test Cases
- test_flow_add_success
- test_flow_add_invalid_file
- test_flow_list_empty
- test_flow_list_with_flows
- test_flow_enable
- test_flow_disable
- test_flow_run
- test_flow_remove
```

---

#### test/cli/commands/test_install.py

| Source | `src/cli_agent_orchestrator/cli/commands/install.py` (6590 LOC) |
|--------|------------------------------------------------------------------|

```python
# Test Cases
- test_install_builtin_agent
- test_install_from_local_file
- test_install_from_url
- test_install_with_provider
- test_install_agent_not_found
```

---

#### test/cli/commands/test_shutdown.py

| Source | `src/cli_agent_orchestrator/cli/commands/shutdown.py` (1306 LOC) |
|--------|-------------------------------------------------------------------|

```python
# Test Cases
- test_shutdown_all
- test_shutdown_specific_session
- test_shutdown_session_not_found
```

---

#### test/cli/commands/test_init.py

| Source | `src/cli_agent_orchestrator/cli/commands/init.py` (378 LOC) |
|--------|-------------------------------------------------------------|

```python
# Test Cases
- test_init_creates_config
- test_init_existing_config
```

---

#### test/cli/test_main.py

| Source | `src/cli_agent_orchestrator/cli/main.py` (620 LOC) |
|--------|-----------------------------------------------------|

```python
# Test Cases
- test_cli_help
- test_cli_version
```

---

### Phase 6: Models (Week 7) - LOW PRIORITY

#### test/models/test_terminal.py

| Source | `src/cli_agent_orchestrator/models/terminal.py` (1277 LOC) |
|--------|-------------------------------------------------------------|

```python
# Test Cases
- test_terminal_model_creation
- test_terminal_status_enum_values
- test_terminal_model_validation
```

---

#### test/models/test_provider.py

| Source | `src/cli_agent_orchestrator/models/provider.py` (191 LOC) |
|--------|-----------------------------------------------------------|

```python
# Test Cases
- test_provider_type_enum_values
- test_provider_type_q_cli
- test_provider_type_kiro_cli
- test_provider_type_claude_code
- test_provider_type_codex
```

---

#### test/models/test_flow.py

| Source | `src/cli_agent_orchestrator/models/flow.py` (943 LOC) |
|--------|-------------------------------------------------------|

```python
# Test Cases
- test_flow_model_creation
- test_flow_model_validation
- test_flow_optional_fields
```

---

#### test/models/test_inbox.py

| Source | `src/cli_agent_orchestrator/models/inbox.py` (721 LOC) |
|--------|--------------------------------------------------------|

```python
# Test Cases
- test_inbox_message_creation
- test_inbox_message_status_enum
```

---

#### test/models/test_session.py

| Source | `src/cli_agent_orchestrator/models/session.py` (551 LOC) |
|--------|----------------------------------------------------------|

```python
# Test Cases
- test_session_model_creation
- test_session_model_validation
```

---

#### test/models/test_agent_profile.py

| Source | `src/cli_agent_orchestrator/models/agent_profile.py` (1049 LOC) |
|--------|------------------------------------------------------------------|

```python
# Test Cases
- test_agent_profile_creation
- test_agent_profile_validation
```

---

#### test/models/test_q_agent.py

| Source | `src/cli_agent_orchestrator/models/q_agent.py` (832 LOC) |
|--------|----------------------------------------------------------|

```python
# Test Cases
- test_q_agent_model_creation
```

---

#### test/models/test_kiro_agent.py

| Source | `src/cli_agent_orchestrator/models/kiro_agent.py` (841 LOC) |
|--------|-------------------------------------------------------------|

```python
# Test Cases
- test_kiro_agent_model_creation
```

---

### Phase 7: Providers & Utils (Week 8) - LOW PRIORITY

#### test/providers/test_base.py

| Source | `src/cli_agent_orchestrator/providers/base.py` (2503 LOC) |
|--------|-----------------------------------------------------------|

```python
# Test Cases
- test_base_provider_abstract
- test_base_provider_interface
```

---

#### test/providers/test_claude_code.py

| Source | `src/cli_agent_orchestrator/providers/claude_code.py` (6684 LOC) |
|--------|-------------------------------------------------------------------|

```python
# Test Cases
- test_initialization
- test_get_status_idle
- test_get_status_processing
- test_get_status_completed
- test_get_status_error
- test_extract_last_message
- test_exit_cli
- test_cleanup
```

---

#### test/utils/test_terminal.py

| Source | `src/cli_agent_orchestrator/utils/terminal.py` (3096 LOC) |
|--------|-----------------------------------------------------------|

```python
# Test Cases
- test_generate_session_name_format
- test_generate_session_name_uniqueness
- test_generate_terminal_id_format
- test_generate_terminal_id_length
- test_generate_terminal_id_hex_chars
```

---

#### test/utils/test_agent_profiles.py

| Source | `src/cli_agent_orchestrator/utils/agent_profiles.py` (1479 LOC) |
|--------|------------------------------------------------------------------|

```python
# Test Cases
- test_get_agent_profile_path
- test_get_builtin_profiles
- test_load_agent_profile
```

---

#### test/utils/test_template.py

| Source | `src/cli_agent_orchestrator/utils/template.py` (869 LOC) |
|--------|----------------------------------------------------------|

```python
# Test Cases
- test_render_template_simple
- test_render_template_with_variables
- test_render_template_missing_variable
```

---

#### test/utils/test_logging.py

| Source | `src/cli_agent_orchestrator/utils/logging.py` (760 LOC) |
|--------|----------------------------------------------------------|

```python
# Test Cases
- test_setup_logging
- test_get_logger
```

---

#### test/test_constants.py

| Source | `src/cli_agent_orchestrator/constants.py` (1530 LOC) |
|--------|------------------------------------------------------|

```python
# Test Cases
- test_constants_defined
- test_default_values
```

---

## Test Guidelines

### Naming Convention

```python
def test_<function_name>_<scenario>():
    """Test <function> when <condition>."""
```

### Test Structure (AAA Pattern)

```python
def test_example():
    # Arrange
    mock_dep.return_value = expected

    # Act
    result = function_under_test(param)

    # Assert
    assert result == expected
```

### Shared Fixtures (test/conftest.py)

```python
import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_db_client():
    with patch("cli_agent_orchestrator.clients.database.db_client") as mock:
        yield mock

@pytest.fixture
def mock_tmux_client():
    with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock:
        yield mock

@pytest.fixture
def sample_terminal():
    from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
    from cli_agent_orchestrator.models.provider import ProviderType
    from datetime import datetime
    return Terminal(
        id="test1234",
        name="test-window",
        provider=ProviderType.Q_CLI,
        session_name="cao-test",
        agent_profile="developer",
        status=TerminalStatus.IDLE,
        last_active=datetime.now(),
    )

@pytest.fixture
def sample_flow():
    from cli_agent_orchestrator.models.flow import Flow
    return Flow(
        name="test-flow",
        schedule="0 9 * * *",
        agent_profile="developer",
        prompt="Test prompt",
    )
```

---

## CI/CD Configuration

### pyproject.toml

```toml
[tool.pytest.ini_options]
testpaths = ["test"]
python_files = "test_*.py"
python_functions = "test_*"
asyncio_mode = "auto"
addopts = "--cov=src --cov-report=term-missing --cov-fail-under=80"
markers = [
    "unit: unit tests",
    "integration: integration tests",
    "slow: slow tests",
]
```

### GitHub Actions (.github/workflows/test.yml)

```yaml
- name: Run Tests
  run: |
    uv run pytest test/ -m "not integration" \
      --cov=src \
      --cov-report=xml \
      --cov-fail-under=80

- name: Upload Coverage
  uses: codecov/codecov-action@v3
```

---

## Summary

| Phase | Files | Tests | Hours |
|-------|-------|-------|-------|
| 1. Services | 5 | 55 | 20 |
| 2. Clients | 2 | 38 | 15 |
| 3. MCP Server | 3 | 15 | 10 |
| 4. API | 1 | 15 | 8 |
| 5. CLI | 5 | 20 | 10 |
| 6. Models | 8 | 15 | 5 |
| 7. Providers/Utils | 7 | 20 | 8 |
| **Total** | **31** | **178** | **76** |

### Priority Order

1. **Week 1-2**: Services (flow, terminal, session, inbox, cleanup)
2. **Week 3-4**: Clients (database, tmux)
3. **Week 5**: MCP Server (server, models, utils)
4. **Week 6**: API (main)
5. **Week 7**: CLI Commands + Models
6. **Week 8**: Providers + Utils
