# TSK-071 — tmux-callsite audit result

## Summary
- Total findings: 18
- LEGIT: 1
- PROVIDER-EXPECTED: 2
- HIDDEN-LEAKAGE: 14
- UNIX-TOOLING: 1

No `TMUX` env-var reads were found under `src/cli_agent_orchestrator/`. No subprocess `which`/`grep` invocations were found either.

## HIDDEN-LEAKAGE (review required)
- `src/cli_agent_orchestrator/providers/claude_code.py:12,241-258`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
      ...
  tmux_client.send_keys(self.session_name, self.window_name, command)
  ```
  Why leakage: the provider is still wired to the concrete `clients.tmux` singleton for shell readiness, history reads, and launch. Plan §3/§5 only calls out the raw `send-keys -l` and direct libtmux trust-path bypasses, not the broader file-level dependency on the tmux-named singleton.

- `src/cli_agent_orchestrator/providers/codex.py:9,252-267`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
      ...
  tmux_client.send_keys(self.session_name, self.window_name, "echo ready")
  ```
  Why leakage: Codex still imports and drives the concrete tmux singleton for shell warm-up, launch, and status reads. Plan §5 only calls out the trust-prompt Enter bypass and WezTerm launch-spec work, not the rest of this file's tmux-bound surface.

- `src/cli_agent_orchestrator/providers/gemini_cli.py:39,432-465`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  tmux_client.send_keys(self.session_name, self.window_name, f"echo {warmup_marker}")
  output = tmux_client.get_history(self.session_name, self.window_name)
  ```
  Why leakage: Gemini still assumes the tmux singleton for pane CWD lookup, warm-up echo, launch, and history polling. Plan §5 explicitly defers Gemini WezTerm wiring, so this is known-but-unlisted tmux coupling outside the provider exception list.

- `src/cli_agent_orchestrator/providers/copilot_cli.py:17-19,139-143,192-195`
  ```python
  from libtmux.exc import LibTmuxException
  from cli_agent_orchestrator.clients.tmux import tmux_client
  pane_working_dir = tmux_client.get_pane_working_directory(...)
  tmux_client.send_special_key(self.session_name, self.window_name, "Enter")
  ```
  Why leakage: this is the only non-`clients/tmux.py` source file importing `libtmux`, and it also imports the tmux singleton directly. Plan §3/§5 does not call out Copilot at all, so this is a hidden provider-side dependency.

- `src/cli_agent_orchestrator/providers/q_cli.py:8,54-71`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  tmux_client.send_keys(self.session_name, self.window_name, command)
  output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
  ```
  Why leakage: Q CLI startup and status detection are still hard-bound to `clients.tmux.tmux_client`. Plan §5 does not mention this provider, so the dependency is outside the explicit provider exception list.

- `src/cli_agent_orchestrator/providers/opencode_cli.py:25,140-144,194`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
      ...
  tmux_client.send_keys(self.session_name, self.window_name, command)
  ```
  Why leakage: OpenCode still depends on the tmux singleton for shell readiness, message delivery, and history capture. Plan §5 does not list OpenCode, so this is hidden coupling outside the documented exceptions.

- `src/cli_agent_orchestrator/providers/kimi_cli.py:38,337-344,389`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
      ...
  tmux_client.send_keys(self.session_name, self.window_name, command)
  ```
  Why leakage: Kimi launch and status logic are still pinned to the tmux singleton. The provider is absent from the Phase 2 provider patch list, so this dependency is currently hidden from the planned scope.

- `src/cli_agent_orchestrator/providers/kiro_cli.py:25,160-184`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  tmux_client.send_keys(self.session_name, self.window_name, command)
  tmux_client.send_keys(self.session_name, self.window_name, "/exit")
  ```
  Why leakage: Kiro initialization, fallback recovery, and status reads all still assume `tmux_client`. Plan §5 does not include Kiro, so this provider-side dependency is not currently on the explicit Phase 2 task list.

- `src/cli_agent_orchestrator/services/session_service.py:29,77-90,112-125`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  tmux_sessions = tmux_client.list_sessions()
  if not tmux_client.session_exists(session_name):
      ...
  tmux_client.kill_session(session_name)
  ```
  Why leakage: session CRUD is still coupled to the tmux singleton import path instead of a backend-neutral multiplexer entrypoint. Plan §2 says services should avoid a full rewrite, but it does not explicitly list this file's import-path dependency.

- `src/cli_agent_orchestrator/services/terminal_service.py:32,129-140,188`
  ```python
  from cli_agent_orchestrator.clients.tmux import tmux_client
  if tmux_client.session_exists(session_name):
      ...
  tmux_client.create_session(session_name, window_name, terminal_id, working_directory)
  tmux_client.pipe_pane(session_name, window_name, str(log_path))
  ```
  Why leakage: the main orchestration service still imports `clients.tmux.tmux_client` directly for session/window lifecycle and log streaming. Phase 2 intends a compatibility shim, but this file remains an external tmux-named callsite outside the boundary.

- `src/cli_agent_orchestrator/services/terminal_service.py:278-312,364,395-447,453`
  ```python
  working_dir = tmux_client.get_pane_working_directory(...)
  tmux_client.send_keys(metadata["tmux_session"], metadata["tmux_window"], message, enter_count=enter_count)
  return tmux_client.get_history(metadata["tmux_session"], metadata["tmux_window"])
  ```
  Why leakage: input delivery, history capture, special keys, pane CWD lookup, and delete-path cleanup all still reach directly into the tmux singleton. These are outside the two provider exceptions and should be tracked as residual external coupling even if Phase 2 keeps them working through a shim.

- `src/cli_agent_orchestrator/utils/terminal.py:15,37-50`
  ```python
  from cli_agent_orchestrator.clients.tmux import TmuxClient
  def wait_for_shell(tmux_client: "TmuxClient", session_name: str, window_name: str, ...):
      output = tmux_client.get_history(session_name, window_name)
  ```
  Why leakage: the shared helper's type import and parameter name encode the concrete `TmuxClient` type into otherwise generic logic. Plan §2 references this helper but does not call out the concrete tmux type annotation as cleanup work.

- `src/cli_agent_orchestrator/cli/commands/info.py:23-32`
  ```python
  # Try to get current session name from tmux
  result = subprocess.run(
      ["tmux", "display-message", "-p", "#S"],
  ```
  Why leakage: direct tmux subprocess invocation in CLI UX code, outside the wrapper boundary. Plan §9 explicitly says UX-only attach/display commands are out of scope, so this remains a hidden non-MVP leak unless tracked separately.

- `src/cli_agent_orchestrator/cli/commands/launch.py:185-187`
  ```python
  # Attach to tmux session unless headless
  if not headless:
      subprocess.run(["tmux", "attach-session", "-t", terminal["session_name"]])
  ```
  Why leakage: direct tmux attach from the CLI command, outside `clients/tmux.py`. The Phase 2 plan explicitly excludes this UX path from MVP scope, so it is not covered by the current task list.

- `src/cli_agent_orchestrator/api/main.py:672-680`
  ```python
  # Start tmux attach inside the PTY
  proc = subprocess.Popen(
      ["tmux", "-u", "attach-session", "-t", f"{session_name}:{window_name}"],
  ```
  Why leakage: the API websocket terminal viewer still shells out to tmux directly. Plan §9 excludes `attach-session` UX work from scope, so this coupling is real but currently outside the explicit Phase 2 implementation set.

## PROVIDER-EXPECTED (confirmed against plan)
- `src/cli_agent_orchestrator/providers/claude_code.py:204-224` — confirmed: the plan's Claude hotspot is real and limited to the startup handler's raw `tmux send-keys -l "\x1b[B"` path plus the direct libtmux `pane.send_keys("", enter=True)` trust confirmation.
- `src/cli_agent_orchestrator/providers/codex.py:233-240` — confirmed: the plan's Codex hotspot is real and limited to the direct libtmux `pane.send_keys("", enter=True)` trust confirmation path.

## UNIX-TOOLING (Windows risk)
- `src/cli_agent_orchestrator/services/inbox_service.py:51-52` — uses `tail -n` via `subprocess.run`; replace with a pure-Python tail helper that seeks backward from the log file end and returns the last `N` lines without shelling out.

## LEGIT (count only)
Count: 1 finding in `src/cli_agent_orchestrator/clients/`.

Covered directory:
- `src/cli_agent_orchestrator/clients/tmux.py` — the libtmux wrapper itself, including its internal `tmux` subprocess calls, `capture-pane`/`paste-buffer`/`pipe-pane` primitives, and the tmux-scoped `cat >>` pipe target.

No additional tmux-dependent implementation callsites were found under `src/cli_agent_orchestrator/multiplexers/` in this pass.

## Verdict for Phase 2 scope
The plan correctly identifies the two provider bypass hotspots and the `inbox_service` `tail -n` problem, but it does not fully capture how much source outside the boundary still imports or types against `cli_agent_orchestrator.clients.tmux.tmux_client`. If Phase 2's goal is strict boundary cleanup, the task list should explicitly track the service/helper/provider files above as residual tmux-named coupling, even if they remain temporarily functional via the compatibility shim; if the goal is only Claude/Codex MVP on Windows, the current plan is sufficient for runtime-critical paths, but the CLI/API attach flows, Copilot `libtmux` import, and generic helper/service imports should be recorded as deferred follow-up leaks rather than left implicit.
