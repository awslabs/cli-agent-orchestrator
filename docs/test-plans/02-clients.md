# Test Implementation: Clients

## Overview

| Module | Source | Test File | Priority |
|--------|--------|-----------|----------|
| database | `src/cli_agent_orchestrator/clients/database.py` | `test/clients/test_database.py` | HIGH |
| tmux | `src/cli_agent_orchestrator/clients/tmux.py` | `test/clients/test_tmux.py` | HIGH |

---

## test/clients/test_database.py

**Dependencies:** SQLite (use in-memory `":memory:"` for tests)

### Terminal Operations
```python
- test_create_terminal
- test_get_terminal_metadata_success
- test_get_terminal_metadata_not_found
- test_list_terminals_by_session
- test_update_last_active
- test_delete_terminal
- test_delete_terminals_by_session
```

### Inbox Operations
```python
- test_create_inbox_message
- test_get_pending_messages
- test_get_inbox_messages_with_status
- test_get_inbox_messages_with_limit
- test_update_message_status
```

### Flow Operations
```python
- test_create_flow
- test_get_flow_success
- test_get_flow_not_found
- test_list_flows
- test_update_flow_run_times
- test_update_flow_enabled
- test_delete_flow
- test_get_flows_to_run
```

---

## test/clients/test_tmux.py

**Dependencies to mock:** `subprocess.run`

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

---

## Fixtures

```python
@pytest.fixture
def test_db(tmp_path):
    """Create isolated test database."""
    from cli_agent_orchestrator.clients.database import DatabaseClient
    db_path = tmp_path / "test.db"
    client = DatabaseClient(str(db_path))
    client.initialize()
    yield client
    client.close()

@pytest.fixture
def mock_subprocess():
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout="")
        yield mock
```
