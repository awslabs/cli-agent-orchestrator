# TSK-073 — Phase 2 Task 3: route Claude/Codex startup handlers through send_special_key

You are executing Phase 2 Task 3 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1–2 + audit (TSK-071) committed. `BaseMultiplexer` and `TmuxMultiplexer` exist; `clients/tmux.py` is a shim re-exporting `tmux_client`.
- Plan binding spec: `docs/PLAN-phase2.md` §3 (last paragraph) and §5 (`claude_code.py` "Patch judgment", `codex.py` "Patch judgment").
- Audit findings: `spikes/TSK-071-result.md` PROVIDER-EXPECTED section confirms exact line ranges.

## Goal

Replace the two tmux-bypass patterns in providers with `tmux_client.send_special_key(...)` calls. Logic, regexes, and state machines stay byte-identical — only the route changes. Do NOT migrate `tmux_client` to a multiplexer accessor (that's Task 4/9). Do NOT touch any other tmux-bound logic in those files.

## Bypass patterns to remove

### `src/cli_agent_orchestrator/providers/claude_code.py`
1. **Line ~204-212**: raw `tmux send-keys -l "\x1b[B"` (down arrow). Currently goes through `subprocess.run(["tmux", ...])`. Replace with:
   ```python
   tmux_client.send_special_key(self.session_name, self.window_name, "\x1b[B", literal=True)
   ```
2. **Line ~218-224**: libtmux trust-confirmation Enter via `tmux_client.server.sessions...pane.send_keys("", enter=True)`. Replace with:
   ```python
   tmux_client.send_special_key(self.session_name, self.window_name, "Enter")
   ```

### `src/cli_agent_orchestrator/providers/codex.py`
3. **Line ~233-240**: libtmux trust-confirmation Enter — same pattern as Claude #2. Replace with:
   ```python
   tmux_client.send_special_key(self.session_name, self.window_name, "Enter")
   ```

## Verify

`TmuxMultiplexer.send_special_key` already supports `literal: bool = False` (added in Task 2). Confirm by reading `src/cli_agent_orchestrator/multiplexers/tmux.py` around the `send_special_key` method.

## Tests

Update `test/providers/test_claude_code_unit.py` and `test/providers/test_codex_provider_unit.py`:
- Replace any test that mocks `subprocess.run` for the down-arrow / trust-enter paths with a mock of `tmux_client.send_special_key` and assert the new call signatures.
- All existing assertions about idle/trust-prompt detection regex paths must continue to pass — those are not touched.

## Constraints (HARD)

- DO NOT change any regex pattern.
- DO NOT change `_handle_startup_prompts` control flow, timeouts, or polling cadences.
- DO NOT migrate the broader `from cli_agent_orchestrator.clients.tmux import tmux_client` lines to a multiplexer accessor — Task 4/9 does that.
- DO NOT modify `gemini_cli.py`, `copilot_cli.py`, `q_cli.py`, `kimi_cli.py`, `kiro_cli.py`, `opencode_cli.py` — those are Task 14 follow-up.
- DO NOT install or upgrade dependencies. Use `.venv\Scripts\python.exe -m pytest` for verification (the project's `rtk pytest` shim collects nothing here — Task 2 confirmed this).
- DO NOT commit or push. Produce a clean working-tree change for the supervising Opus to commit.

## Verification command sequence

```
.venv/Scripts/python.exe -m pytest test/providers/test_claude_code_unit.py test/providers/test_codex_provider_unit.py -x --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

The second run's failure count must not exceed the **43-failure baseline** from Task 2.

## Reporting

Write `spikes/TSK-073-result.md`:

```markdown
# TSK-073 — Task 3 result

## Files touched
<list>

## Bypass replacements
- claude_code.py down-arrow: <before line range> → <after line range, signature shown>
- claude_code.py trust-enter: <before> → <after>
- codex.py trust-enter: <before> → <after>

## Tests
- claude_code + codex unit suites: <N pass / M fail>
- full (excl. e2e): <N pass / M fail> — must be ≤43 fail

## Deviations
<any>

## Follow-ups
<any>
```

Echo: `TSK-073: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 3.

Begin.
