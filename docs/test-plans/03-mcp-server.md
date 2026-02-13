# Test Implementation: MCP Server

## Overview

| Module | Source | Test File | Priority |
|--------|--------|-----------|----------|
| server | `src/cli_agent_orchestrator/mcp_server/server.py` | `test/mcp_server/test_server.py` | HIGH |
| models | `src/cli_agent_orchestrator/mcp_server/models.py` | `test/mcp_server/test_models.py` | LOW |
| utils | `src/cli_agent_orchestrator/mcp_server/utils.py` | `test/mcp_server/test_utils.py` | LOW |

---

## test/mcp_server/test_server.py

**Dependencies to mock:** `terminal_service`, `inbox_service`, `httpx`

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

## test/mcp_server/test_models.py

```python
# Test Cases
- test_handoff_request_validation
- test_assign_request_validation
- test_send_message_request_validation
```

---

## test/mcp_server/test_utils.py

```python
# Test Cases
- test_get_terminal_id_from_env
- test_get_terminal_id_missing
```

---

## Fixtures

```python
@pytest.fixture
def mock_terminal_service():
    with patch("cli_agent_orchestrator.mcp_server.server.terminal_service") as mock:
        yield mock

@pytest.fixture
def mock_httpx():
    with patch("httpx.AsyncClient") as mock:
        yield mock
```
