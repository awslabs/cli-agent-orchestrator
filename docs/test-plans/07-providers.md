# Test Implementation: Providers

## Overview

| Module | Source | Test File | Status |
|--------|--------|-----------|--------|
| base | `src/cli_agent_orchestrator/providers/base.py` | `test/providers/test_base.py` | NEW |
| claude_code | `src/cli_agent_orchestrator/providers/claude_code.py` | `test/providers/test_claude_code.py` | NEW |
| codex | `src/cli_agent_orchestrator/providers/codex.py` | `test/providers/test_codex.py` | EXISTS |
| kiro_cli | `src/cli_agent_orchestrator/providers/kiro_cli.py` | `test/providers/test_kiro_cli.py` | EXISTS |
| manager | `src/cli_agent_orchestrator/providers/manager.py` | `test/providers/test_manager.py` | EXISTS |
| q_cli | `src/cli_agent_orchestrator/providers/q_cli.py` | `test/providers/test_q_cli.py` | EXISTS |

---

## test/providers/test_base.py

```python
# Test Cases
- test_base_provider_abstract
- test_base_provider_interface
```

---

## test/providers/test_claude_code.py

**Dependencies to mock:** `tmux_client`, subprocess

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

## Fixtures

```python
@pytest.fixture
def mock_tmux():
    with patch("cli_agent_orchestrator.providers.claude_code.tmux_client") as mock:
        yield mock
```
