# Qwen Code (`qwen_cli`) Provider — Design

Issue: [#376](https://github.com/awslabs/cli-agent-orchestrator/issues/376) — *[Feat] Qwen Code (qwen) provider adapter*
Date: 2026-07-09
Status: Approved (design), implementation in progress

## 1. Summary

Add [Qwen Code](https://github.com/QwenLM/qwen-code) (`qwen`) as a first-class CAO
provider. `qwen` is an interactive, full-screen Ink TUI coding agent — a fork of
Google's `gemini-cli`. The adapter launches `qwen` inside a tmux window, detects
its status from terminal output, extracts the last response, and cleans up. It
integrates with CAO's assign/handoff/send_message orchestration exactly like the
existing providers.

Authentication is **user-managed**: `qwen` reads OpenAI-compatible credentials
from the ambient environment (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`,
DashScope-compatible endpoints) or `qwen-oauth`. The provider never handles
credentials.

## 2. Why `antigravity_cli` is the template

`qwen` and Google's `agy` (Antigravity CLI, `antigravity_cli.py`) are **both
gemini-cli-derived Ink TUIs**. They share the same status-surface shape and the
same class of bug, so the sibling provider is a far closer template than the
Kimi/Cursor providers named in the issue:

- **Footer-anchored status.** Idle footer shows `? for shortcuts`; the busy
  spinner line contains `esc to cancel`. Identical to `agy`.
- **In-place footer redraw → stale-buffer latch.** The raw pipe-pane byte stream
  keeps a stale `esc to cancel` forever after a turn ends. `antigravity_cli`
  solves this with `supports_screen_detection = True` + a pyte-rendered
  `get_status_from_screen()`. `qwen` needs the same fix.
- **`mcpServers`-shaped MCP config** and a `--model` flag map directly.

Startup-dialog handling borrows the polling-loop shape from `kimi_cli`
(`_handle_startup_dialog`).

> Note: the `skills/cao-provider/references/provider-template.md` template
> reflects an **older** provider contract (`get_status(tail_lines)` +
> `tmux_client.get_history`). The current `base.py` contract is
> `async initialize()` + `get_status(self, buffer)` (raw pipe-pane stream, with
> `_resolve_native_status()` called first). This design follows the real,
> current providers, not the stale template.

## 3. Relevant `qwen` v0.19.5 facts (verified against the installed binary)

| Concern | Fact |
|---|---|
| Auto-approve | `--approval-mode yolo` (footer: `YOLO mode (shift + tab to cycle)`). `-y/--yolo` equivalent. |
| Model | `-m, --model <name>`. |
| System prompt | `--append-system-prompt "<text>"` appends to qwen's built-in agent prompt (works in interactive mode). `--system-prompt` fully overrides. |
| MCP | `--mcp-config <path-or-inline-json>` accepts a **file path** to `{"mcpServers": {...}}`. Per-server `env` is honored. Also reads `~/.qwen/settings.json` / `.qwen/settings.json`. |
| Context file | `QWEN.md` (the `GEMINI.md` equivalent). |
| Auth (OpenAI-compat) | env `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` (alias `QWEN_MODEL`); all three present ⇒ auto-selects `openai` auth, skips the auth dialog. `qwen-oauth` is browser-only (not headless). |
| Idle signal | footer `? for shortcuts`; input placeholder `Type your message or @path/to/file`; no `esc to cancel`. |
| Busy signal | spinner line containing `esc to cancel` (with elapsed-seconds timer). |
| Response delimiting | user line prefixed `>`; assistant/tool lines prefixed `●`; errors prefixed `✕ [API Error: …]`. |
| Exit | `/quit`. |

## 4. Design decisions (aligned with the maintainer)

1. **PR scope: full package** — provider + all wiring + unit tests & fixtures +
   e2e test classes + docs, matching issue #376's acceptance criteria.
2. **MCP via per-instance `--mcp-config` temp file** — each terminal writes its
   own `{"mcpServers": {...}}` JSON (with `CAO_TERMINAL_ID` injected into each
   server's `env`) and points `--mcp-config` at it. No shared-file mutation, so
   no concurrency/cleanup hazard (avoids lesson #19). The temp file is tracked in
   `_tmp_paths` and deleted in `cleanup()`.
3. **System prompt via `--append-system-prompt`** — compose
   `profile.system_prompt` → `_apply_skill_prompt()` → (when restricted)
   `SECURITY_PROMPT` + allowed-tool list, and append it on top of qwen's built-in
   agent prompt. Preserves qwen's native coding/tool scaffolding.
4. **Soft tool enforcement** — like the sibling gemini-fork provider (and
   Kimi/Codex), restrictions are advisory text in the system prompt. `qwen_cli`
   joins `SOFT_ENFORCEMENT_PROVIDERS`. Documented as a Known Limitation. A
   `tool_mapping.py` entry (gemini-style native names) is added only for the
   launch-confirmation summary.

## 5. Provider implementation (`src/cli_agent_orchestrator/providers/qwen_cli.py`)

### Module-level patterns (calibrated to qwen's footer TUI)

- `ANSI` stripping — via `strip_terminal_escapes` from `utils/text.py`.
- `PROCESSING_FOOTER_PATTERN = r"esc to cancel"` — busy spinner line.
- `IDLE_FOOTER_PATTERN = r"\?\s*for shortcuts"` (+ `_LOG` variant for the file
  watcher).
- `QUERY_PROMPT_PATTERN = r"^\s*>\s+\S"` — user turn line (extraction boundary).
- `RESPONSE_BULLET_PATTERN = r"^\s*●\s"` — assistant/tool line.
- `SEPARATOR_PATTERN` — the full-width `─` rules bounding the input box.
- `WAITING_USER_ANSWER_PATTERN` — trust/confirmation dialogs (defensive; yolo
  suppresses most).
- `ERROR_PATTERN = r"✕\s*\[API Error"` — surfaced turn error.
- `FOOTER_TAIL_WINDOW = 2048`.

### Class `QwenCliProvider(BaseProvider)`

- `__init__(terminal_id, session_name, window_name, agent_profile=None,
  allowed_tools=None, model=None, skill_prompt=None)`; calls
  `super().__init__(terminal_id, session_name, window_name, allowed_tools,
  skill_prompt)`. Fields: `_agent_profile`, `_model`, `_initialized=False`,
  `_turns=0`, `_tmp_paths: list[Path]`.
- `paste_enter_count = 1` (Ink single-Enter submit; validate live).
- `_build_qwen_command()`:
  - `shutil.which("qwen")` guard → `ProviderError` if missing.
  - `qwen --approval-mode yolo`.
  - `--model` from `profile.model` (preferred) or `self._model`.
  - `--append-system-prompt "<composed>"` (only when composed text is non-empty).
  - `--mcp-config <temp file>` when `profile.mcpServers` is present; inject
    `CAO_TERMINAL_ID` into each server `env`; record path in `_tmp_paths`.
- `initialize()` (async): `wait_for_shell` → `status_monitor.notify_input_sent`
  → `send_keys(command)` → `_handle_startup_dialog()` (poll-dismiss first-run
  theme/trust splash; early-return once footer is ready) →
  `wait_until_status({IDLE, COMPLETED}, timeout≈180s)`.
- `get_status(buffer)`: `_resolve_native_status()` first; empty → `UNKNOWN`;
  `strip_terminal_escapes`; `tail = clean[-FOOTER_TAIL_WINDOW:]`. Priority:
  WAITING_USER_ANSWER → PROCESSING (`esc to cancel` in tail) → COMPLETED/IDLE
  (`? for shortcuts` and no `esc to cancel`; `_turns` splits COMPLETED vs IDLE) →
  ERROR (`✕ [API Error`) → UNKNOWN.
- `supports_screen_detection = True` + `get_status_from_screen(screen_lines)`
  mirroring the same precedence on the pyte-rendered frame (the stale-footer fix).
- `extract_last_message_from_script(script_output)`: text between the last
  `> <query>` line and the next separator/footer, chrome-filtered, escapes
  stripped.
- `exit_cli() -> "/quit"`.
- `cleanup()`: `_initialized=False`; delete each `_tmp_paths` entry (swallow
  errors).
- `mark_input_received()`: `super().mark_input_received()` then `_turns += 1`.

## 6. Wiring (6 files)

| File | Change |
|---|---|
| `models/provider.py` | `QWEN_CLI = "qwen_cli"` |
| `providers/manager.py` | `elif provider_type == ProviderType.QWEN_CLI.value:` → `QwenCliProvider(..., model=model, skill_prompt=skill_prompt)` |
| `providers/__init__` / import in manager | import `QwenCliProvider` |
| `cli/commands/launch.py` | add `"qwen_cli"` to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` |
| `services/terminal_service.py` | add `ProviderType.QWEN_CLI.value` to `RUNTIME_SKILL_PROMPT_PROVIDERS` and `SOFT_ENFORCEMENT_PROVIDERS` |
| `utils/tool_mapping.py` | add `"qwen_cli"` block (gemini-style native tool names, display-only) |

`model` already flows automatically: `terminal_service.py` passes
`model=profile.model` into `create_provider`.

## 7. Testing

### Unit — `test/providers/test_qwen_cli_unit.py` (no network required)
- Initialization: success, shell timeout, CLI timeout, startup-dialog dismissal.
- Status: IDLE, PROCESSING, COMPLETED, WAITING_USER_ANSWER, ERROR, empty;
  **COMPLETED-over-PROCESSING** stale-buffer case; `get_status_from_screen` path.
- Extraction: success, no marker, empty, multiple turns (last wins), ANSI strip.
- Regex patterns matched against fixture text.
- Command building: `--approval-mode yolo`, `--model`, `--append-system-prompt`
  composition, `--mcp-config` file written with `CAO_TERMINAL_ID` injected,
  soft `SECURITY_PROMPT` appended when restricted, missing-binary → `ProviderError`.
- Edge: `exit_cli`, `cleanup` deletes `_tmp_paths`, `paste_enter_count`.

### Fixtures — `test/providers/fixtures/qwen_cli_*.txt`
`idle`, `processing`, `completed`, `response`, `waiting`. Captured from the real
`qwen` TUI in tmux (`tmux capture-pane -e -p`).

### E2E — add `TestQwenCli*` classes to the 5 e2e files + `require_qwen_cli`
fixture in `test/e2e/conftest.py` (mirroring the Kimi/Cursor classes): handoff
(2), assign (3), send_message (1), allowed_tools (3, restricted-cannot-bash
marked `xfail` for soft enforcement), supervisor_orchestration (2).

### Validation caveat (network)
The provided test endpoint is an Alibaba Cloud MaaS region
(`cn-beijing.maas.aliyuncs.com`) that is **unreachable from the CI/build
environment** (connection times out). Therefore:
- Unit tests and **idle / startup / processing / error-turn** fixtures are
  captured locally (qwen renders TUI chrome even when the API call times out).
- A **successful `●` response** fixture and the full live e2e suite require a
  reachable OpenAI-compatible endpoint. Per the issue, the author will calibrate
  status detection against real `qwen` TUI output on a reachable endpoint. The
  provider's status/extraction logic keys on TUI **chrome**, not response
  content, so the local fixtures exercise every `get_status` branch.

## 8. Documentation
- `docs/qwen-cli.md` (mirror `docs/antigravity-cli.md`): prerequisites, auth,
  launch examples, agent-profile format, MCP config, Known Limitations (soft
  tool enforcement; headless auth constraints), troubleshooting.
- `README.md`: provider table row + the four inline provider enumerations
  (intro line 7, frontmatter comment ~166, cross-provider list ~276).
- `CHANGELOG.md`: new-provider entry.

## 9. File checklist
- [ ] `src/cli_agent_orchestrator/models/provider.py`
- [ ] `src/cli_agent_orchestrator/providers/qwen_cli.py`
- [ ] `src/cli_agent_orchestrator/providers/manager.py`
- [ ] `src/cli_agent_orchestrator/cli/commands/launch.py`
- [ ] `src/cli_agent_orchestrator/services/terminal_service.py`
- [ ] `src/cli_agent_orchestrator/utils/tool_mapping.py`
- [ ] `test/providers/test_qwen_cli_unit.py`
- [ ] `test/providers/fixtures/qwen_cli_*.txt`
- [ ] `test/e2e/conftest.py` (+ `TestQwenCli*` in 5 e2e files)
- [ ] `docs/qwen-cli.md`
- [ ] `README.md`, `CHANGELOG.md`
