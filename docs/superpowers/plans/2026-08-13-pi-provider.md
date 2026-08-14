# Pi Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add production-ready Pi coding-agent support with terminal UX, hard built-in tool restrictions, reliable lifecycle/output state, and live CAO MCP orchestration tools.

**Architecture:** A `PiProvider` launches Pi's regular TUI in the current backend. A bundled Pi extension writes authoritative lifecycle/output state and registers tools exposed by a bundled Python MCP proxy that uses CAO's existing official MCP SDK dependency.

**Tech Stack:** Python 3.10+, pytest, MCP Python SDK, Pi TypeScript extension API, tmux terminal backend, Markdown documentation.

**Spec:** `docs/superpowers/specs/2026-08-13-pi-provider-design.md`

## Global Constraints

- Resolve `pi` from `PATH`; do not hard-code the local Gohan installation path.
- Support the verified Pi 0.84.1 CLI and extension API.
- Do not install community Pi packages or modify `~/.pi`.
- Disable ambient Pi extensions and explicitly load only CAO's bundled extension.
- Use CAO-owned `0700` directories and `0600` generated files.
- Preserve repository context-file discovery while rejecting unapproved project-local Pi resources.
- Treat Pi as hard enforcement only for its seven built-in tools; do not claim sandboxing.
- Support stdio MCP servers in V1 and fail clearly for unsupported transports or duplicate names.
- Preserve the existing CAO input/backend contract; RPC transport is out of scope.
- Use test-driven development for every production behavior.

---

### Task 1: MCP JSONL proxy

**Files:**
- Create: `src/cli_agent_orchestrator/providers/pi_mcp_proxy.py`
- Create: `test/providers/test_pi_mcp_proxy.py`

**Interfaces:**
- Consumes: a JSON config path containing `{terminalId, servers}` and newline-delimited requests on stdin.
- Produces: `run_proxy(config_path: Path, reader: TextIO, writer: TextIO) -> None`, plus a module CLI accepting `--config`.
- Produces protocol operations `list_tools`, `call_tool`, and `shutdown` with ID-correlated JSON responses.

- [ ] **Step 1: Write failing config-validation and protocol tests**

```python
def test_load_config_rejects_http_server(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text('{"terminalId":"t1","servers":{"remote":{"url":"https://x"}}}')
    with pytest.raises(ProxyConfigError, match="stdio"):
        load_proxy_config(config)

def test_duplicate_tool_names_fail_closed():
    with pytest.raises(ProxyProtocolError, match="duplicate tool name"):
        flatten_tools({"one": [_tool("handoff")], "two": [_tool("handoff")]})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest test/providers/test_pi_mcp_proxy.py -q`

Expected: collection fails because `pi_mcp_proxy` does not exist.

- [ ] **Step 3: Implement validated config models and pure protocol helpers**

```python
@dataclass(frozen=True)
class ProxyServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    request_timeout_ms: int

def response_ok(request_id: str, result: Any) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}
```

Reject missing commands, invalid environment values, timeouts outside `1..1_200_000`, and
duplicate exposed tool names. Error responses include exception messages but never dump config
or inherited environment values.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest test/providers/test_pi_mcp_proxy.py -q`

Expected: pure helper and validation tests pass.

- [ ] **Step 5: Add a real SDK-backed fake-server integration test**

```python
def test_proxy_lists_and_calls_a_stdio_mcp_server(tmp_path):
    result = run_proxy_fixture(tmp_path, requests=[
        {"id": "1", "type": "list_tools"},
        {"id": "2", "type": "call_tool", "server": "fixture", "name": "echo", "arguments": {"text": "hi"}},
        {"id": "3", "type": "shutdown"},
    ])
    assert result[0]["result"]["tools"][0]["name"] == "echo"
    assert result[1]["result"]["content"] == [{"type": "text", "text": "hi"}]
```

The fixture server uses `FastMCP`, proving initialization, `tools/list`, and `tools/call`
through the official SDK instead of mocking the SDK internals.

- [ ] **Step 6: Implement async MCP sessions and the stdin/stdout loop**

Use `AsyncExitStack`, `stdio_client(StdioServerParameters(...))`, and `ClientSession`. Merge
`os.environ`, declared server environment, and `CAO_TERMINAL_ID`; apply per-server
`read_timeout_seconds`; serialize Pydantic results using `model_dump(mode="json")`.

- [ ] **Step 7: Verify proxy tests**

Run: `uv run pytest test/providers/test_pi_mcp_proxy.py -q`

Expected: all proxy unit and integration tests pass with no stdout diagnostics.

- [ ] **Step 8: Commit the proxy slice**

```bash
git add src/cli_agent_orchestrator/providers/pi_mcp_proxy.py test/providers/test_pi_mcp_proxy.py
git commit -m "feat(pi): add MCP bridge proxy"
```

### Task 2: Bundled Pi extension and package contract

**Files:**
- Create: `src/cli_agent_orchestrator/providers/pi_extension.ts`
- Create: `test/providers/test_pi_extension.py`

**Interfaces:**
- Consumes environment variables `CAO_PI_STATE_FILE`, `CAO_PI_MCP_CONFIG`, and `CAO_PI_BRIDGE_PYTHON`.
- Consumes/produces the Task 1 JSONL proxy protocol.
- Produces atomic state JSON with `status`, `lastAssistantText`, `error`, and `updatedAt`.
- Registers each unique MCP tool with its original name and JSON Schema parameters.

- [ ] **Step 1: Write failing package and source-contract tests**

```python
def test_bundled_extension_exists_and_has_required_events():
    source = pi_extension_path().read_text()
    assert 'pi.on("agent_start"' in source
    assert 'pi.on("message_end"' in source
    assert 'pi.on("agent_settled"' in source
    assert "pi.registerTool" in source

def test_wheel_contains_pi_extension(tmp_path):
    wheel = build_wheel(tmp_path)
    assert "cli_agent_orchestrator/providers/pi_extension.ts" in wheel.namelist()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest test/providers/test_pi_extension.py -q`

Expected: failure because the extension asset is absent.

- [ ] **Step 3: Implement proxy client, dynamic tools, and atomic state writes**

```typescript
export default function caoPiExtension(pi: any) {
  pi.on("session_start", async (_event: any, ctx: any) => {
    await bridge.start();
    for (const tool of await bridge.listTools()) pi.registerTool(toPiTool(tool));
    await writeState({ status: "idle", lastAssistantText: "", error: "" });
  });
  pi.on("agent_start", async () => writeState({ status: "processing" }));
  pi.on("message_end", async (event: any) => cacheAssistantText(event.message));
  pi.on("agent_settled", async () => writeState({ status: "completed" }));
  pi.on("session_shutdown", async () => bridge.shutdown());
}
```

Use Node built-ins only. Parse LF-delimited JSON manually, correlate requests, propagate abort
signals, normalize MCP content to Pi text/image content, and surface bridge failures through
both the state file and `ctx.ui.setStatus`.

- [ ] **Step 4: Verify source/package tests and a live installed-Pi load probe**

Run: `uv run pytest test/providers/test_pi_extension.py -q`

Run an RPC-mode no-model probe with the installed Pi, explicit extension, fake state/config,
and no ambient resources. Expected: Pi initializes the extension, writes `idle`, and exits
without changing `~/.pi` configuration.

- [ ] **Step 5: Commit the extension slice**

```bash
git add src/cli_agent_orchestrator/providers/pi_extension.ts test/providers/test_pi_extension.py
git commit -m "feat(pi): bundle lifecycle and MCP extension"
```

### Task 3: Pi provider adapter

**Files:**
- Create: `src/cli_agent_orchestrator/providers/pi.py`
- Create: `test/providers/test_pi_unit.py`
- Create: `test/providers/fixtures/pi_idle.txt`
- Create: `test/providers/fixtures/pi_processing.txt`
- Create: `test/providers/fixtures/pi_completed.txt`

**Interfaces:**
- Consumes: `BaseProvider`, `load_agent_profile`, `resolve_mcp_server_config`, Task 2 extension asset, `allowed_tools`, `skill_prompt`, and optional `model`.
- Produces: `PiProvider`, `_build_pi_command() -> str`, state-backed `get_status()`, exact response extraction, `exit_cli() == "C-d"`, and cleanup.

- [ ] **Step 1: Write failing command and file-safety tests**

```python
def test_build_command_is_private_explicit_and_shell_safe(tmp_path):
    provider = make_provider(model="openai/gpt-5", home=tmp_path)
    command = provider._build_pi_command()
    assert "pi --tui-mode regular --no-approve --no-extensions" in command
    assert "--extension" in command
    assert "--session-id term-1" in command
    assert "--model openai/gpt-5" in command
    assert stat.S_IMODE(provider.state_path.stat().st_mode) == 0o600

def test_build_command_resolves_and_injects_mcp_terminal_id(tmp_path):
    config = json.loads(make_provider(home=tmp_path)._write_mcp_config().read_text())
    assert config["terminalId"] == "term-1"
    assert Path(config["servers"]["cao-mcp-server"]["command"]).is_absolute()
```

- [ ] **Step 2: Run command tests and verify RED**

Run: `uv run pytest test/providers/test_pi_unit.py -q`

Expected: collection fails because `PiProvider` does not exist.

- [ ] **Step 3: Implement paths, private file writes, prompt/config composition, and command building**

Resolve `pi` once with `shutil.which("pi")` and launch that exact absolute executable so the
server and tmux cannot select different binaries from different `PATH` values. Append
`profile.system_prompt or profile.prompt`, runtime `skill_prompt`, model, resolved MCP config,
native denylist, and the explicit extension. Use `shlex.quote` for every dynamic shell token.

- [ ] **Step 4: Add failing lifecycle, extraction, and cleanup tests**

```python
def test_status_prefers_sidecar_over_stale_tui(tmp_path):
    provider = make_provider(home=tmp_path)
    write_state(provider, status="completed", lastAssistantText="exact answer")
    assert provider.get_status("Working... stale frame") is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script("terminal chrome") == "exact answer"

def test_mark_input_closes_idle_race(tmp_path):
    provider = make_provider(home=tmp_path)
    write_state(provider, status="idle")
    provider.mark_input_received()
    assert provider.get_status("old idle screen") is TerminalStatus.PROCESSING
```

- [ ] **Step 5: Implement initialization, state parsing, TUI fallback, extraction, and cleanup**

Initialization waits for the shell, launches Pi, and accepts only `IDLE`; bridge errors and
missing Pi fail clearly. State reads validate ownership, JSON shape, and status values. TUI
fallback recognizes Pi's `Working...` frame and regular editor/footer. Cleanup unlinks private
temporary files idempotently; `exit_cli()` returns `C-d`; `paste_enter_count` returns `1`.

- [ ] **Step 6: Verify provider unit tests**

Run: `uv run pytest test/providers/test_pi_unit.py -q`

Expected: all command, status, extraction, exit, and cleanup tests pass.

- [ ] **Step 7: Commit the provider slice**

```bash
git add src/cli_agent_orchestrator/providers/pi.py test/providers/test_pi_unit.py test/providers/fixtures/pi_*.txt
git commit -m "feat(pi): add terminal provider adapter"
```

### Task 4: Public registration and tool policy

**Files:**
- Modify: `src/cli_agent_orchestrator/models/provider.py`
- Modify: `src/cli_agent_orchestrator/providers/manager.py`
- Modify: `src/cli_agent_orchestrator/cli/commands/launch.py`
- Modify: `src/cli_agent_orchestrator/services/terminal_service.py`
- Modify: `src/cli_agent_orchestrator/utils/tool_mapping.py`
- Modify: `src/cli_agent_orchestrator/schemas/agent_profile.schema.json`
- Modify: provider/manager/tool mapping tests

**Interfaces:**
- Produces public provider ID `pi` throughout enum-derived CLI/API validation.
- Produces hard native built-in denylist mapping and runtime skill-prompt delivery.

- [ ] **Step 1: Write failing registration and mapping tests**

```python
def test_pi_provider_is_public_and_requires_workspace_access():
    assert ProviderType.PI.value == "pi"
    assert "pi" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS
    assert "pi" in RUNTIME_SKILL_PROMPT_PROVIDERS
    assert "pi" not in SOFT_ENFORCEMENT_PROVIDERS

def test_pi_reviewer_native_denylist():
    assert get_disallowed_tools("pi", ["fs_read", "fs_list"]) == ["bash", "edit", "write"]
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest test/providers/test_pi_unit.py test/utils/test_tool_mapping.py -q`

- [ ] **Step 3: Register Pi and add the exact tool mapping**

Add `ProviderType.PI`, a `ProviderManager` branch passing profile/tools/skill/model, workspace
access, runtime skill prompt support, and the mapping defined in the design spec. Do not add Pi
to `SOFT_ENFORCEMENT_PROVIDERS`.

- [ ] **Step 4: Verify registration, install, schema, and terminal-service tests**

Run: `uv run pytest test/providers/test_pi_unit.py test/providers/test_provider_manager.py test/services/test_install_service.py test/services/test_terminal_service.py test/utils/test_tool_mapping.py -q`

- [ ] **Step 5: Commit the public integration slice**

```bash
git add src/cli_agent_orchestrator test/providers test/services test/utils
git commit -m "feat(pi): register provider and enforce tool policy"
```

### Task 5: Real orchestration and E2E coverage

**Files:**
- Modify: `test/e2e/conftest.py`
- Modify: `test/e2e/test_allowed_tools.py`
- Modify: `test/e2e/test_assign.py`
- Modify: `test/e2e/test_handoff.py`
- Modify: `test/e2e/test_send_message.py`
- Modify: `test/e2e/test_supervisor_orchestration.py`

**Interfaces:**
- Consumes public provider ID `pi` and installed/authenticated Pi.
- Proves launch, exact output, hard tool restriction, and CAO orchestration behavior.

- [ ] **Step 1: Add `require_pi` and provider matrix cases**

```python
@pytest.fixture()
def require_pi():
    if shutil.which("pi") is None:
        pytest.skip("pi CLI not installed")
```

Add `TestPi*` classes that call the existing shared helpers with `provider="pi"`.

- [ ] **Step 2: Run Pi launch and simple response smoke tests**

Run the installed Pi through CAO in an isolated temporary CAO home and synthetic workspace.
Expected: launch reaches `IDLE`, the exact prompt response reaches `COMPLETED`, and LAST output
contains only assistant text.

- [ ] **Step 3: Run allowed-tools tests**

Run: `uv run pytest -m e2e test/e2e/test_allowed_tools.py -k Pi -v -o addopts=`

Expected: supervisor cannot bash/write; developer retains allowed Pi built-ins.

- [ ] **Step 4: Run orchestration tests in increasing scope**

Run `send_message`, then `handoff`, then `assign`, then supervisor orchestration Pi classes.
Inspect failures at the earliest semantic layer before proceeding.

- [ ] **Step 5: Commit E2E coverage**

```bash
git add test/e2e
git commit -m "test(pi): cover orchestration workflows"
```

### Task 6: Canonical documentation, packaging, and full verification

**Files:**
- Create: `docs/pi.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/agent-profile.md`
- Modify: `docs/configuration.md`
- Modify: `docs/skills.md`
- Modify: `docs/tool-restrictions.md`
- Modify: `src/cli_agent_orchestrator/skills/cao-session-management/SKILL.md`
- Modify: package/link validation tests as needed

**Interfaces:**
- Documents installation, profile use, trust/security boundary, MCP bridge, supported tools,
  session ownership, limitations, and troubleshooting.

- [ ] **Step 1: Write provider documentation and update canonical provider lists**

Document `cao launch --agents developer --provider pi`, Pi prerequisites, model overrides,
hard built-in tool enforcement, missing core `web_fetch`, explicit extension isolation,
CAO-owned session storage, stdio-only MCP V1, and that Pi has no sandbox.

- [ ] **Step 2: Verify all local documentation links**

Run: `uv run python scripts/validate_markdown_links.py`

- [ ] **Step 3: Build a wheel and verify the extension asset is present**

Run: `uv build --wheel`

Inspect the wheel archive for
`cli_agent_orchestrator/providers/pi_extension.ts`; install it into a temporary environment
and run `cao --help` plus a Pi extension load probe.

- [ ] **Step 4: Run focused quality gates**

```bash
uv run black --check src/cli_agent_orchestrator/providers/pi.py src/cli_agent_orchestrator/providers/pi_mcp_proxy.py test/providers/test_pi*.py
uv run isort --check-only src/cli_agent_orchestrator/providers/pi.py src/cli_agent_orchestrator/providers/pi_mcp_proxy.py test/providers/test_pi*.py
uv run mypy src/cli_agent_orchestrator/providers/pi.py src/cli_agent_orchestrator/providers/pi_mcp_proxy.py
uv run pytest test/providers/test_pi_unit.py test/providers/test_pi_mcp_proxy.py test/providers/test_pi_extension.py -q
```

- [ ] **Step 5: Run the broader regression suite**

Run: `uv run pytest test/ --ignore=test/e2e --ignore=test/fixtures/test_cao_server.py -m "not integration" -q`

The ignored fixture is a verified clean-baseline host dependency that currently errors when
Kiro is absent; it is unrelated to Pi. Re-run the Pi E2E subset separately.

- [ ] **Step 6: Review diff and commit documentation/final polish**

```bash
git diff --check
git status --short
git add README.md CHANGELOG.md docs src/cli_agent_orchestrator/skills
git commit -m "docs(pi): document provider support"
```

- [ ] **Step 7: Request code review before integration**

Use the repository review workflow on the complete branch. Address verified findings with a
new failing test before changing behavior, then repeat focused and broader verification.
