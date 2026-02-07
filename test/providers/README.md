# Provider Tests

This directory contains comprehensive test suites for provider implementations.

## Providers

### Kiro CLI Provider (Default)
Tests for Kiro CLI integration (`kiro_cli`) - the default provider.

### Q CLI Provider
Tests for Amazon Q CLI integration (`q_cli`)

Since Kiro CLI has identical output format to Q CLI, the test fixtures are reused with renamed files.

## Test Structure

```
test/providers/
├── test_kiro_cli_unit.py       # Kiro CLI unit tests (fast, mocked) - default provider
├── test_q_cli_unit.py          # Q CLI unit tests (fast, mocked)
├── test_claude_code_unit.py    # Claude Code unit tests (fast, mocked)
├── test_codex_provider_unit.py # Codex CLI unit tests (fast, mocked)
├── test_base_provider.py       # Base provider abstract interface tests
├── test_tmux_working_directory.py # TmuxClient working directory tests
├── test_q_cli_integration.py   # Q CLI integration tests (slow, real Q CLI)
├── fixtures/                    # Test fixture files
│   ├── kiro_cli_*.txt          # Kiro CLI fixtures (default provider)
│   ├── q_cli_*.txt             # Q CLI fixtures
│   ├── codex_*.txt             # Codex CLI fixtures
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

The project includes multiple GitHub Actions workflows that run on pull requests and pushes:

### Comprehensive Workflow (`ci.yml`)
Runs **all tests** in `test/` (excluding Q CLI integration), plus security scanning:
- **Unit tests**: Python 3.10, 3.11, 3.12 matrix with coverage
- **Code quality**: black, isort, mypy
- **Security scan**: Trivy vulnerability scanner (CRITICAL/HIGH)
- **Dependency review**: License and vulnerability checks on PRs

### Provider-Specific Workflows (path-triggered)
Each provider has a dedicated workflow that runs only when its files change:

| Workflow | Tests | Trigger Paths |
|---|---|---|
| `test-codex-provider.yml` | `test_codex_provider_unit.py` | `providers/codex.py`, `test/providers/**` |
| `test-claude-code-provider.yml` | `test_claude_code_unit.py` | `providers/claude_code.py`, `test/providers/**` |
| `test-kiro-cli-provider.yml` | `test_kiro_cli_unit.py` | `providers/kiro_cli.py`, `test/providers/**` |
| `test-q-cli-provider.yml` | `test_q_cli_unit.py` | `providers/q_cli.py`, `test/providers/**` |

Each includes unit tests (Python 3.10/3.11/3.12) and code quality checks (black, isort, mypy).

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

## Claude Code Provider Tests

### Test Coverage (`test_claude_code_unit.py`)

**33 tests covering:**

1. **Initialization (7 tests)**
   - Successful initialization (with `wait_for_shell` assertion)
   - Shell timeout handling
   - Claude Code timeout handling
   - Initialization with agent profile
   - Invalid agent profile error handling
   - MCP server configuration
   - Command verification (`claude` sent to tmux)

2. **Status Detection (10 tests)**
   - IDLE status with old `>` prompt
   - IDLE status with new `❯` prompt
   - IDLE status with ANSI-coded terminal output
   - COMPLETED status (both prompt styles)
   - PROCESSING status
   - WAITING_USER_ANSWER status
   - ERROR status (empty output, unrecognized output)
   - Status detection with `tail_lines` parameter

3. **Message Extraction (5 tests)**
   - Successful extraction
   - No response pattern error
   - Empty response error
   - Multiple responses (uses last)
   - Separator handling

4. **Miscellaneous (5 tests)**
   - Exit command, idle pattern for log, cleanup
   - Building claude command (no profile, with system prompt)

5. **Trust Prompt Handling (6 tests)**
   - Trust prompt detected and auto-accepted via Enter key
   - Early return when Claude starts without trust prompt (Welcome banner)
   - Timeout handling when neither prompt nor banner appears
   - Empty output followed by trust prompt detection
   - Trust prompt NOT misdetected as `WAITING_USER_ANSWER` in `get_status()`
   - `initialize()` integration with trust prompt acceptance flow

**Coverage:** 100% of claude_code.py

### Running Claude Code Tests

```bash
# Run all Claude Code unit tests
uv run pytest test/providers/test_claude_code_unit.py -v

# Run with coverage
uv run pytest test/providers/test_claude_code_unit.py --cov=src/cli_agent_orchestrator/providers/claude_code.py --cov-report=term-missing -v

# Run specific test class
uv run pytest test/providers/test_claude_code_unit.py::TestClaudeCodeProviderInitialization -v
```

## Codex CLI Provider Tests

### Test Coverage (`test_codex_provider_unit.py`)

**56 tests covering:**

1. **Initialization (3 tests)**
   - Successful initialization (warm-up `echo ready` + codex with `--no-alt-screen --disable shell_snapshot`)
   - Shell timeout handling
   - Codex timeout handling

2. **Command Building (10 tests)**
   - Base command without agent profile
   - Command with agent profile (developer_instructions injection)
   - Double quote escaping in system prompts
   - Newline escaping for TOML/tmux compatibility
   - MCP server config injection via `-c mcp_servers.<name>.<field>`
   - MCP server with environment variables
   - Empty system prompt handling
   - None system prompt handling
   - Agent profile load failure (ProviderError)
   - Initialize with agent profile end-to-end

3. **Status Detection — Label Format (14 tests)**
   - IDLE, COMPLETED, PROCESSING, WAITING_USER_ANSWER, ERROR states
   - Empty output handling
   - tail_lines parameter
   - Old prompt in scrollback (bottom-N-lines approach)
   - Assistant mentioning error/approval text (not false positives)
   - TUI output with status bar (idle + completed)
   - Trust prompt detection

4. **Status Detection — Bullet Format (7 tests)**
   - COMPLETED with `•` response after `›` user input
   - PROCESSING with partial `•` output (no idle prompt)
   - IDLE when no `•` response after user message
   - Code blocks within `•` response
   - Error detection not masked by bullet pattern
   - Multi-turn `•` conversations
   - TUI status bar with `•` bullet format

5. **Message Extraction — Label Format (4 tests)**
   - Successful extraction, complex messages, missing marker, empty response

6. **Message Extraction — Bullet Format (5 tests)**
   - Single-line `•` response
   - Multi-line `•` response (all bullets preserved)
   - Code blocks within `•` response
   - Multi-turn extraction (only last response)
   - Extraction without trailing idle prompt

7. **Miscellaneous (5 tests)**
   - Exit command, idle pattern for log, cleanup, extraction without trailing prompt

8. **Trust Prompt Handling (4 tests)**
   - Trust prompt detected and auto-accepted
   - Trust prompt not needed (welcome banner)
   - Trust prompt as WAITING_USER_ANSWER status
   - Initialize with trust prompt flow

**Coverage:** 96% of codex.py

### Running Codex Tests

```bash
# Run all Codex CLI unit tests
uv run pytest test/providers/test_codex_provider_unit.py -v

# Run with coverage
uv run pytest test/providers/test_codex_provider_unit.py --cov=src/cli_agent_orchestrator/providers/codex.py --cov-report=term-missing -v

# Run specific test class
uv run pytest test/providers/test_codex_provider_unit.py::TestCodexBuildCommand -v
```

## Kiro CLI Provider Tests

### Running Kiro CLI Tests

```bash
# Run all Kiro CLI unit tests
uv run pytest test/providers/test_kiro_cli_unit.py -v

# Run with coverage
uv run pytest test/providers/test_kiro_cli_unit.py --cov=src/cli_agent_orchestrator/providers/kiro_cli.py --cov-report=term-missing -v

# Run specific test class
uv run pytest test/providers/test_kiro_cli_unit.py::TestKiroCliProviderStatusDetection -v
```

Note: Kiro CLI has identical output format to Q CLI, so the test structure and fixtures mirror the Q CLI tests.

### Key Test Validations

1. **Index Integrity**: Verifies ANSI codes don't corrupt position-based extraction
2. **Message Completeness**: Ensures multi-line handoff messages are fully captured
3. **Status Transitions**: Monitors state changes during handoff (IDLE → PROCESSING → COMPLETED)
4. **Error Handling**: Tests failed handoff scenarios
5. **Permission Prompts**: Tests handoffs requiring user approval

## TmuxClient send_keys Tests

Unit tests for the `TmuxClient.send_keys` method are in `test/clients/test_tmux_send_keys.py`.

**8 tests covering:**

1. **Literal mode (3 tests)**
   - Text chunks use `literal=True` (prevents tmux key interpretation)
   - Final `C-m` (Enter) is NOT sent as literal
   - Commands with single quotes use literal mode (the original bug)

2. **Chunking (2 tests)**
   - Long commands are split into multiple chunks
   - Short commands remain as a single chunk

3. **Correctness (1 test)**
   - All chunks reconstruct the original command

4. **Error handling (2 tests)**
   - Session not found
   - Window not found

### Running TmuxClient Tests

```bash
uv run pytest test/clients/test_tmux_send_keys.py -v
```

## Launch Command Tests

Unit tests for the `launch` CLI command are in `test/cli/commands/test_launch.py`.

**10 tests covering:**

1. **Core functionality (4 tests)**
   - Working directory included in API params
   - Custom session name
   - Headless mode (no tmux attach)
   - Invalid provider error

2. **Error handling (2 tests)**
   - RequestException (server unreachable)
   - Generic exception

3. **Workspace access confirmation (4 tests)**
   - Confirmation shown and accepted for `claude_code` provider
   - Confirmation declined cancels launch
   - `--yes` flag skips confirmation
   - Default provider (`kiro_cli`) also shows confirmation

**Coverage:** 100% of launch.py

### Running Launch Tests

```bash
uv run pytest test/cli/commands/test_launch.py -v
```

## Test Quality Metrics

- **Provider Unit Test Count:** ~152 (across all providers)
- **CLI Command Test Count:** ~10
- **Client Unit Test Count:** ~20
- **Integration Test Count:** 9
- **Total Test Count:** 467
- **Coverage:** 83% overall; 96-100% of all provider modules and launch.py
- **Execution Time:** <5s (unit), <90s (integration)
- **Test Categories:** 12 (initialization, status label-format, status bullet-format, extraction label-format, extraction bullet-format, command building, patterns, prompts, handoff, edge cases, tmux send_keys, workspace confirmation)
