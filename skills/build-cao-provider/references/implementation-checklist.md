# Implementation Checklist

Step-by-step file creation guide for a new CAO provider. Replace `<provider>` with the actual name (e.g., `kimi_cli`).

## Table of Contents

1. [Provider Enum](#step-1-provider-enum)
2. [Provider Implementation](#step-2-provider-implementation)
3. [Registration](#step-3-registration)
4. [Test Fixtures](#step-4-test-fixtures)
5. [Unit Tests](#step-5-unit-tests)
6. [E2E Tests](#step-6-e2e-tests)
7. [CI Workflow](#step-7-ci-workflow)
8. [Provider Documentation](#step-8-provider-documentation)

---

## Step 1: Provider Enum

**File:** `src/cli_agent_orchestrator/models/provider.py`

Add to `ProviderType` enum:
```python
NEW_PROVIDER = "<provider>"
```

---

## Step 2: Provider Implementation

**File:** `src/cli_agent_orchestrator/providers/<provider>.py` (NEW, ~250-400 lines)

Use `kimi_cli.py` as the primary reference template.

### Module-Level Pattern Constants

Each pattern needs a detailed comment explaining what it matches and why:

```python
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"           # Strip ANSI escape codes
IDLE_PROMPT_PATTERN = r"..."                       # CLI's idle prompt
IDLE_PROMPT_TAIL_LINES = 50                        # Must cover tall terminals (see lessons #2)
IDLE_PROMPT_PATTERN_LOG = r"..."                   # Simplified for log file monitoring
# Response/assistant markers, user input markers, error patterns, TUI chrome patterns
```

### Required Methods

**`_build_command() -> str`**
- Use `shlex.join()` for safe shell escaping
- Handle optional agent profiles (temp YAML/config files if needed)
- Handle MCP server config injection
- **CRITICAL:** Forward `CAO_TERMINAL_ID` to MCP server env (see lessons #1)
- Track temp files for cleanup

**`initialize() -> bool`**
```python
def initialize(self) -> bool:
    if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
        raise TimeoutError("Shell initialization timed out")
    command = self._build_command()
    tmux_client.send_keys(self.session_name, self.window_name, command)
    if not wait_until_status(self, TerminalStatus.IDLE, timeout=120.0, polling_interval=1.0):
        raise TimeoutError("CLI initialization timed out")
    self._initialized = True
    return True
```

**`get_status(tail_lines) -> TerminalStatus`**
- Strip ANSI codes first
- Check bottom `IDLE_PROMPT_TAIL_LINES` lines for idle prompt (end-of-line anchored)
- If prompt found: IDLE (no user input) or COMPLETED (has user input + response)
- If no prompt: check for ERROR patterns, otherwise PROCESSING
- If the CLI has trust/permission prompts: check before idle

**`extract_last_message_from_script(script_output) -> str`**
- Find the last user input marker
- Collect content between user input and next idle prompt
- Filter out thinking lines using raw ANSI codes (see lessons #3)
- Filter out TUI chrome (status bars, spinners)
- Raise `ValueError` with descriptive message on failure
- Include fallback: if all lines filtered, return all content

**`exit_cli() -> str`** — Return the CLI's exit command (e.g., `/exit`)

**`get_idle_pattern_for_log() -> str`** — Return simplified pattern for log monitoring

**`cleanup() -> None`** — Remove temp files, reset `_initialized`

---

## Step 3: Registration

**File:** `src/cli_agent_orchestrator/providers/manager.py`
- Import the new provider class
- Add `elif` in `create_provider()` for the new `ProviderType` value

**File:** `src/cli_agent_orchestrator/cli/commands/launch.py`
- Add `"<provider>"` to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS`

---

## Step 4: Test Fixtures

**Directory:** `test/providers/fixtures/`

Create 5 fixture files from REAL captured terminal output (from Phase 1):
- `<provider>_idle_output.txt` — Idle prompt visible, no user input
- `<provider>_completed_output.txt` — User input + complete response + idle prompt
- `<provider>_processing_output.txt` — Mid-stream, no idle prompt
- `<provider>_error_output.txt` — Error message visible
- `<provider>_complex_response.txt` — Multi-line response with code blocks, thinking

---

## Step 5: Unit Tests

**File:** `test/providers/test_<provider>_unit.py` (NEW, target >90% coverage)

Organize into test classes:

| Class | Tests | Coverage Focus |
|-------|-------|----------------|
| TestInitialization | 4-5 | success, shell timeout, CLI timeout, agent profile, state tracking |
| TestStatusDetection | 8-12 | every TerminalStatus, ANSI, tail_lines, **tall terminal** |
| TestMessageExtraction | 8-10 | success, thinking filter, errors, fixtures, fallback |
| TestBuildCommand | 8-13 | base, agent profile, MCP, CAO_TERMINAL_ID, escaping, errors |
| TestPatterns | 8-10 | every regex constant matches/rejects correctly |
| TestLifecycle | 6-8 | cleanup, exit, idle pattern, attributes, double cleanup |

**Include from day one:** `test_get_status_idle_tall_terminal` (see lessons #2)

---

## Step 6: E2E Tests

**File:** `test/e2e/conftest.py`
- Add `require_<provider>` fixture (skip if CLI not installed)

**Files:** `test/e2e/test_handoff.py`, `test_assign.py`, `test_send_message.py`, `test_supervisor_orchestration.py`
- Add test classes: `Test<Provider>Handoff` (2 tests), `Test<Provider>Assign` (3 tests), `Test<Provider>SendMessage` (1 test), `Test<Provider>SupervisorOrchestration` (2 tests: handoff delegation + assign+handoff delegation)
- Supervisor orchestration tests verify the full flow: supervisor calls MCP tools → workers spawn → results flow back (see lessons #11, #12)
- Verify supervisor does not busy-wait after assign — if the model runs shell commands (sleep/echo) to "wait" for results, update the supervisor agent profile with message delivery guidance (see lessons #17)

---

## Step 7: CI Workflow

**File:** `.github/workflows/test-<provider>.yml` (NEW)

Copy from an existing workflow (e.g., `test-kimi-cli-provider.yml`), update:
- Trigger paths: `providers/<provider>.py`, `test/providers/**`
- Test target: `test/providers/test_<provider>_unit.py`
- Python matrix: 3.10, 3.11, 3.12

---

## Step 8: Provider Documentation

**File:** `docs/<provider>.md` (NEW)

Follow `docs/kimi-cli.md` template. Sections:
- Overview, Prerequisites, Quick Start
- Status Detection (table of patterns)
- Message Extraction
- Agent Profiles
- MCP Server Configuration (including CAO_TERMINAL_ID forwarding)
- Command Flags
- Implementation Notes (lifecycle, IDLE_PROMPT_TAIL_LINES value)
- E2E Testing commands
- Troubleshooting

**Verify:** All constants (like `IDLE_PROMPT_TAIL_LINES`) match the actual code values. No stale references.
