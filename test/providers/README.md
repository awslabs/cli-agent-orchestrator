# Q CLI Provider Tests

This directory contains comprehensive test suites for the Q CLI provider implementation.

## Test Structure

```
test/providers/
├── test_q_cli_unit.py          # Unit tests (fast, mocked)
├── test_q_cli_integration.py   # Integration tests (slow, real Q CLI)
├── fixtures/                    # Test fixture files
│   ├── q_cli_idle_output.txt
│   ├── q_cli_completed_output.txt
│   ├── q_cli_processing_output.txt
│   ├── q_cli_permission_output.txt
│   ├── q_cli_error_output.txt
│   ├── q_cli_complex_response.txt
│   └── generate_fixtures.py    # Script to regenerate fixtures
└── README.md
```

## Test Coverage

### Unit Tests (`test_q_cli_unit.py`)

**34 tests covering:**

1. **Initialization (4 tests)**
   - Successful initialization
   - Shell timeout handling
   - Q CLI timeout handling
   - Different agent profiles

2. **Status Detection (7 tests)**
   - IDLE status
   - COMPLETED status
   - PROCESSING status
   - WAITING_USER_ANSWER status
   - ERROR status
   - Empty output handling
   - tail_lines parameter

3. **Message Extraction (6 tests)**
   - Successful extraction
   - Complex messages with code blocks
   - Missing green arrow error
   - Missing final prompt error
   - Empty response error
   - Multiple responses (uses last)

4. **Regex Patterns (5 tests)**
   - Green arrow pattern
   - Idle prompt pattern
   - Prompt with percentage
   - Permission prompt pattern
   - ANSI code cleaning

5. **Prompt Patterns (3 tests)**
   - Basic prompt
   - Prompt with usage percentage
   - Prompt with special characters

6. **Edge Cases (9 tests)**
   - Exit command
   - Idle pattern for logs
   - Cleanup
   - Long profile names
   - Unicode characters
   - Control characters
   - Multiple error indicators
   - Terminal attributes
   - Whitespace variations

**Coverage:** 100% of q_cli.py

### Integration Tests (`test_q_cli_integration.py`)

**9 tests covering:**

1. **Real Q CLI Operations (5 tests)**
   - Initialization flow
   - Simple query execution
   - Status detection
   - Exit command
   - Different agent profiles

2. **Handoff Scenarios (2 tests)**
   - Status transitions during handoff
   - Message integrity verification

3. **Error Handling (2 tests)**
   - Invalid session handling
   - Non-existent session status

**Requirements:** 
- Q CLI must be installed (`q` command available)
- Q CLI must be authenticated (AWS credentials configured)
- tmux 3.2+ must be installed

**Agent Setup:**
The integration tests automatically create a test agent named `agent-q-cli-integration-test` if it doesn't exist. The agent is created at:
- `~/.aws/amazonq/cli-agents/agent-q-cli-integration-test.json`

If you want to create the agent manually before running tests:
```bash
mkdir -p ~/.aws/amazonq/cli-agents
cat > ~/.aws/amazonq/cli-agents/agent-q-cli-integration-test.json << 'EOF'
{
  "name": "agent-q-cli-integration-test",
  "description": "Test agent for integration tests",
  "instructions": "You are a helpful developer assistant for testing purposes.",
  "tools": []
}
EOF
```

For more information on custom agents, see: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-custom-agents.html

## Running Tests

### Run All Unit Tests (Recommended)
```bash
uv run pytest test/providers/test_q_cli_unit.py -v
```

### Run Unit Tests with Coverage
```bash
uv run pytest test/providers/test_q_cli_unit.py --cov=src/cli_agent_orchestrator/providers/q_cli.py --cov-report=term-missing -v
```

### Run Integration Tests (Requires Q CLI)
```bash
uv run pytest test/providers/test_q_cli_integration.py -v
```

### Run All Tests
```bash
uv run pytest test/providers/ -v
```

### Run Tests by Marker
```bash
# Run only integration tests
uv run pytest test/providers/ -m integration -v

# Skip integration tests (unit only)
uv run pytest test/providers/ -m "not integration" -v

# Run only slow tests
uv run pytest test/providers/ -m slow -v
```

### Run Specific Test Class
```bash
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderStatusDetection -v
```

### Run Specific Test
```bash
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderStatusDetection::test_get_status_idle -v
```

## Test Fixtures

Test fixtures contain realistic Q CLI terminal output with proper ANSI escape sequences. To regenerate fixtures:

```bash
uv run python test/providers/fixtures/generate_fixtures.py
```

### Fixture Contents

- **q_cli_idle_output.txt** - Agent prompt without response
- **q_cli_completed_output.txt** - Complete response with green arrow
- **q_cli_processing_output.txt** - Partial output during processing
- **q_cli_permission_output.txt** - Permission request prompt
- **q_cli_error_output.txt** - Error message output
- **q_cli_complex_response.txt** - Multi-line response with code blocks
- **q_cli_handoff_successful.txt** - Successful handoff between agents
- **q_cli_handoff_error.txt** - Failed handoff with error message
- **q_cli_handoff_with_permission.txt** - Handoff requiring user permission

## CI/CD Integration

The project includes a GitHub Actions workflow (`.github/workflows/test-q-cli-provider.yml`) that automatically runs tests on pull requests and pushes to main/develop branches.

The workflow includes:
- **Unit tests**: Run on Python 3.10, 3.11, and 3.12 with coverage reporting
- **Integration tests**: Run only on main branch (requires Q CLI setup)
- **Code quality**: Checks formatting (black), import sorting (isort), and type checking (mypy)

For more details, see the [workflow file](../../.github/workflows/test-q-cli-provider.yml).

## Writing New Tests

### Unit Test Template

```python
@patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
def test_new_feature(self, mock_tmux):
    """Test description."""
    # Setup mock
    mock_tmux.get_history.return_value = "test output"
    
    # Create provider
    provider = QCliProvider("test1234", "test-session", "window-0", "developer")
    
    # Execute test
    result = provider.some_method()
    
    # Assert expectations
    assert result == expected_value
```

### Integration Test Template

```python
def test_new_integration(self, q_cli_available, test_session_name, cleanup_session):
    """Test description."""
    # Create session
    tmux_client.create_session(test_session_name, detached=True)
    window_name = "window-0"
    
    try:
        # Test logic
        provider = QCliProvider("test1234", test_session_name, window_name, "developer")
        # ... perform test operations
        
        assert result == expected
    finally:
        # Cleanup
        tmux_client.kill_session(test_session_name)
```

## Troubleshooting

### Unit Tests Fail with Import Error
```bash
# Sync dependencies (installs all required packages including dev dependencies)
uv sync
```

### Fixture Files Have Wrong Encoding
```bash
# Regenerate fixtures
uv run python test/providers/fixtures/generate_fixtures.py
```

### Integration Tests Skip
- Ensure Q CLI is installed: `which q`
- Ensure Q CLI is authenticated: `q status`
- Check that tmux is installed: `which tmux`

### Coverage Not 100%
Run with missing lines report:
```bash
uv run pytest test/providers/test_q_cli_unit.py --cov=src/cli_agent_orchestrator/providers/q_cli.py --cov-report=term-missing
```

## Maintenance

### When Q CLI Output Format Changes

1. Update fixture files in `fixtures/generate_fixtures.py`
2. Regenerate: `uv run python test/providers/fixtures/generate_fixtures.py`
3. Run tests to verify: `uv run pytest test/providers/test_q_cli_unit.py -v`
4. Update integration tests if behavior changes

### Adding New Q CLI Features

1. Add unit tests first (TDD approach)
2. Implement feature in q_cli.py
3. Add integration test for end-to-end validation
4. Update this README with new test info

## Handoff Testing

### Understanding the Index Problem

The Q CLI provider uses index-based extraction for parsing terminal output. This is critical to understand when testing handoff scenarios:

**How it works:**
1. Regex finds match positions (indices) in the ORIGINAL string WITH ANSI codes
2. Indices are used to extract substring: `script_output[start_pos:end_pos]`
3. ANSI codes are cleaned from the EXTRACTED text

**Why this matters:**
- Stripping ANSI codes BEFORE finding indices would corrupt the positions
- The current implementation correctly finds indices first, then cleans
- Tests verify this behavior remains correct during handoff scenarios

### Handoff Test Coverage

**Unit Tests (8 tests):**
- Successful handoff status detection
- Successful handoff message extraction
- Failed handoff error detection
- Failed handoff message extraction
- Handoff with permission prompts
- Multi-line handoff message preservation
- Index integrity verification
- ANSI code cleaning validation

**Integration Tests (2 tests):**
- Real handoff status transitions monitoring
- Message integrity during actual handoff execution

### Running Handoff Tests

```bash
# Run all handoff unit tests
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderHandoffScenarios -v

# Run handoff integration tests
uv run pytest test/providers/test_q_cli_integration.py::TestQCliProviderHandoffIntegration -v

# Run specific handoff test
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderHandoffScenarios::test_handoff_indices_not_corrupted -v
```

### Key Test Validations

1. **Index Integrity**: Verifies ANSI codes don't corrupt position-based extraction
2. **Message Completeness**: Ensures multi-line handoff messages are fully captured
3. **Status Transitions**: Monitors state changes during handoff (IDLE → PROCESSING → COMPLETED)
4. **Error Handling**: Tests failed handoff scenarios
5. **Permission Prompts**: Tests handoffs requiring user approval

## Test Quality Metrics

- **Unit Test Count:** 42
- **Integration Test Count:** 9
- **Coverage:** 100% of q_cli.py
- **Execution Time:** <1s (unit), <90s (integration)
- **Test Categories:** 7 (initialization, status, extraction, patterns, prompts, handoff, edge cases)
