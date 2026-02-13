# Test Implementation: API

## Overview

| Module | Source | Test File | Priority |
|--------|--------|-----------|----------|
| main | `src/cli_agent_orchestrator/api/main.py` | `test/api/test_main.py` | HIGH |

---

## test/api/test_main.py

**Dependencies:** FastAPI TestClient, mock all services

### Session Endpoints
```python
- test_create_session_success
- test_create_session_invalid_provider
- test_list_sessions
- test_get_session_success
- test_get_session_not_found
- test_delete_session_success
```

### Terminal Endpoints
```python
- test_create_terminal_in_session
- test_list_terminals_in_session
- test_get_terminal_working_directory
- test_exit_terminal
- test_delete_terminal
```

### Health & Lifecycle
```python
- test_health_check
- test_lifespan_startup
- test_flow_daemon_execution
```

---

## Fixtures

```python
from fastapi.testclient import TestClient

@pytest.fixture
def mock_services():
    with patch("cli_agent_orchestrator.api.main.session_service") as sess, \
         patch("cli_agent_orchestrator.api.main.terminal_service") as term, \
         patch("cli_agent_orchestrator.api.main.flow_service") as flow:
        yield {"session": sess, "terminal": term, "flow": flow}

@pytest.fixture
def client(mock_services):
    from cli_agent_orchestrator.api.main import app
    with TestClient(app) as c:
        yield c
```
