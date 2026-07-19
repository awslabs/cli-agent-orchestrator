# mock_cli provider — credential-free orchestration testing

## Why this exists

The other CAO providers (`claude_code`, `kiro_cli`, `codex`, `kimi_cli`, `copilot_cli`, `opencode_cli`) all wrap real coding-CLI binaries that need real authentication — Anthropic API keys, Google OAuth, AWS SSO, etc. That auth model is right for production but blocks two classes of work in CI:

1. **Fork CI cannot access secrets.** GitHub Actions running in a fork can't read `secrets.ANTHROPIC_API_KEY` or equivalent. Any test that hits a real CLI needs credentials plus tmux, so it's marked `integration`/`e2e` and excluded from the default CI run (`pyproject.toml`'s `addopts = -m 'not e2e'`; the per-provider workflows such as `.github/workflows/test-claude-code-provider.yml` run only unit tests). Contributors opening a PR from a fork get no end-to-end signal on their orchestration-layer changes.
2. **Real CLIs are slow, non-deterministic, and expensive.** Even with credentials, running a real model in CI burns real dollars, varies between runs, and adds 10–60s per terminal lifecycle. Orchestration logic — handoffs, the inbox watchdog, multi-provider sessions — doesn't need a real model; it just needs *something* that behaves like a CLI agent on the terminal-state contract.

`mock_cli` fills that gap. It's a tiny bash binary plus a thin provider that together let CAO drive a deterministic "agent" through the full lifecycle (initialize → IDLE → receive input → PROCESSING → COMPLETED → respond, plus ERROR injection). No auth, no network, no flakes, no cost.

## Design

Two components:

**1. `test/providers/fixtures/bin/mock_cli`** — a ~60-line bash REPL.

- Prints a banner (`MockCli ready.`), prints the prompt char `❯ `, reads stdin.
- On each input line: sleeps `--delay-ms` (default 50ms), echoes `> MOCK: <input>`, reprints the prompt.
- Magic strings for failure-mode injection:
  - `/exit` or `/quit` → clean exit with `goodbye`
  - `__mock_error__` → emit `ERROR: mock failure injected` (drives state to ERROR)
  - `__mock_sleep_<N>` → sleep N seconds (lets tests exercise long-PROCESSING paths)
- Not on PATH outside pytest. `test/conftest.py` prepends the fixture bin dir to `PATH` at module-load time so `shlex.join(["mock_cli", ...])` resolves.

**2. `src/cli_agent_orchestrator/providers/mock_cli.py`** — a ~95-line provider.

- Subclasses `BaseProvider`.
- `initialize()` waits for shell → spawns `mock_cli --delay-ms N` via `tmux_client.send_keys` → waits for IDLE/COMPLETED.
- `get_status()` strips ANSI then pattern-matches:
  - `ERROR: mock failure injected` present → ERROR
  - no `❯ ` visible → PROCESSING
  - `> MOCK:` + `❯ ` present → COMPLETED
  - just `❯ ` → IDLE
- `extract_last_message_from_script()` returns the payload of the last `> MOCK: <text>` line.
- Registered in `ProviderType.MOCK_CLI = "mock_cli"` and `ProviderManager.create_provider()` like any other provider.

That's it. No model API, no settings.json mangling, no auth flow, no PATH lookup in production.

## What this unlocks

Orchestration-layer tests that previously needed a real provider can now run in fork CI without secrets:

- **Handoff lifecycle** — spawn → send → wait for COMPLETED → extract → exit, all the way through `terminal_service`.
- **Assign + callback** — spawn → send with callback instructions → return immediately → exercise `send_message` flush via inbox.
- **Inbox watchdog** — send to busy receiver → assert PENDING → mock IDLE transition → assert flush.
- **Multi-provider sessions** — spawn two `mock_cli` workers + one `mock_cli` supervisor → assert message routing across the session.
- **Flow scheduling** — APScheduler-driven cron flows can hit mock terminals without burning real model calls.

## Boundaries

- **Production code never sees `mock_cli`.** The binary isn't installed to PATH outside pytest. The provider is registered but inert unless someone explicitly passes `--provider mock_cli`.
- **It doesn't validate provider correctness.** Real CLIs change between versions; the captured-fixture replay tests in `test/providers/fixtures/*_output.txt` are what catches regex drift for the real providers. `mock_cli` validates the *orchestrator's* behavior, not the providers'.
- **It still uses real tmux and a real subprocess.** This is intentional — the unit tests that just mock `tmux_client.get_history` already exist; `mock_cli` enables the next layer up (tests that exercise real tmux + real subprocess + the inbox/watchdog wiring) at zero auth cost.

## CI strategy summary

| Tier | Auth needed | Runs in fork PRs | Tooling |
|---|---|---|---|
| Unit | None | ✅ | `unittest.mock` of `tmux_client` + captured fixture `.txt` files |
| **Orchestration (this PR enables)** | **None** | **✅** | **`--provider mock_cli` + real tmux + real subprocess** |
| Integration | Real CLI | ❌ (main only) | Gated workflow job + `pytest.mark.integration` |
| E2E | Full stack | ❌ (manual) | Default `addopts = -m 'not e2e'` skip; `pytest -m e2e` to opt in |
