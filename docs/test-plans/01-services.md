# Test Implementation: Services

## Overview

| Module | Source | Test File | Priority |
|--------|--------|-----------|----------|
| flow_service | `src/cli_agent_orchestrator/services/flow_service.py` | `test/services/test_flow_service.py` | HIGH |
| terminal_service | `src/cli_agent_orchestrator/services/terminal_service.py` | `test/services/test_terminal_service.py` | HIGH |
| session_service | `src/cli_agent_orchestrator/services/session_service.py` | `test/services/test_session_service.py` | HIGH |
| inbox_service | `src/cli_agent_orchestrator/services/inbox_service.py` | `test/services/test_inbox_service.py` | HIGH |
| cleanup_service | `src/cli_agent_orchestrator/services/cleanup_service.py` | `test/services/test_cleanup_service.py` | MEDIUM |

---

## test/services/test_flow_service.py

**Dependencies to mock:** `database.py`, `tmux.py`, `template.py`

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

---

## test/services/test_terminal_service.py (EXPAND EXISTING)

**Dependencies to mock:** `database.py`, `tmux.py`, `providers/manager.py`

```python
# Test Cases
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

## test/services/test_session_service.py

**Dependencies to mock:** `database.py`, `tmux.py`

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

## test/services/test_inbox_service.py

**Dependencies to mock:** `database.py`, `tmux.py`

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

## test/services/test_cleanup_service.py

**Dependencies to mock:** `database.py`

```python
# Test Cases
- test_cleanup_old_terminals
- test_cleanup_old_inbox_messages
- test_cleanup_old_terminal_logs
- test_cleanup_old_server_logs
- test_cleanup_no_old_data
```

---

## Fixtures

```python
@pytest.fixture
def mock_db():
    with patch("cli_agent_orchestrator.services.flow_service.db_client") as mock:
        yield mock

@pytest.fixture
def mock_tmux():
    with patch("cli_agent_orchestrator.services.flow_service.tmux_client") as mock:
        yield mock

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
