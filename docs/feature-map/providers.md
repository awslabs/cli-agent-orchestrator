# providers

A provider is the code that drives one coding CLI in a terminal. It turns the CLI's
screen into a CAO status, and every handoff, assign and inbox delivery depends on that
status being right. Most provider bugs look like "the agent is stuck" or "the answer
never came", so this file is mostly about which signals to trust.

## What exists

- **`ProviderType`** (`models/provider.py:4-21`): 13 real providers and `mock_cli`.
  Each has one class under `src/cli_agent_orchestrator/providers/`.
- **Statuses** a provider reports: `IDLE`, `PROCESSING`, `COMPLETED`,
  `WAITING_USER_ANSWER`, `ERROR`. A detector can also return `UNKNOWN`, which means "no
  signal" and never replaces a known status (`services/status_monitor.py:515-519`).

| Provider | Binary | Doc |
|---|---|---|
| `claude_code` | `claude` | [claude-code.md](../claude-code.md) |
| `codex` | `codex` | [codex-cli.md](../codex-cli.md) |
| `kiro_cli` | `kiro-cli` | [kiro-cli.md](../kiro-cli.md) |
| `kimi_cli` | `kimi` | [kimi-cli.md](../kimi-cli.md) |
| `qwen_cli` | `qwen` | [qwen-cli.md](../qwen-cli.md) |
| `copilot_cli` | `copilot` | [copilot-cli.md](../copilot-cli.md) |
| `opencode_cli` | `opencode` | [opencode-cli.md](../opencode-cli.md) |
| `hermes` | `hermes` | [hermes.md](../hermes.md) |
| `cursor_cli` | `agent` | [cursor-cli.md](../cursor-cli.md) |
| `antigravity_cli` | `agy` | [antigravity-cli.md](../antigravity-cli.md) |
| `omp` | `omp` | [omp-cli.md](../omp-cli.md) |
| `grok_cli` | `grok` | [grok-cli.md](../grok-cli.md) |
| `mcode` | `mcode` | [minimax-code.md](../minimax-code.md) |
| `mock_cli` | `mock_cli` (test fixture) | [mock-cli-provider.md](../mock-cli-provider.md) |

Binary names come from `GET /agents/providers` (`api/main.py:4192-4218`).

## How a user reaches it

- `cao launch --agents <profile> --provider <name>`. Without `--provider`, the profile's
  `provider:` decides.
- `cao install <profile> --provider <name>` writes the CLI's own agent file where the
  CLI needs one (Kiro, for example).
- Supervisors pick a provider for workers through the profile, or `provider` on the
  `handoff` / `assign` MCP tools.

## How to drive it

Which provider CLIs can the server see?

```bash
curl -s http://localhost:9889/agents/providers | python3 -m json.tool
```
```json
[
    {"name": "kiro_cli", "binary": "kiro-cli", "installed": false},
    {"name": "claude_code", "binary": "claude", "installed": false},
    …
]
```

**With no credentials, use `mock_cli`.** Its binary ships only in the repo's test
fixtures, so you need a checkout:

```bash
export PATH="$PWD/test/providers/fixtures/bin:$PATH"   # in the repo root, before cao-server
cao-server &
cao launch --agents developer --provider mock_cli --headless "hello map"
```
```
Session created: cao-<id>
Terminal created: developer-<suffix>
hello map
```

```bash
cao session status cao-<id> --json
```
```json
{
  "session": "cao-<id>",
  "conductor": {
    "id": "<terminal-id>",
    "agent_profile": "developer",
    "provider": "mock_cli",
    "model": null,
    "model_honored": false,
    "status": "completed",
    "last_output": "hello map"
  }
}
```

What the provider read from the screen:

```bash
curl -s "http://localhost:9889/terminals/<terminal-id>/output?mode=last"
```
```json
{"output": "hello map", "mode": "last"}
```

`mode=full` returns the raw pane text (`> MOCK: hello map` then the `❯` prompt).
`GET /terminals/<terminal-id>` re-detects the status; `GET /sessions/cao-<id>/terminals`
lists every terminal with `provider`, `status` and `identity_verified`.

For a real provider, run the same three steps with `--provider <name>`, using any
provider that shows `installed: true` above. Mock-free provider tests are gated: `CAO_RUN_LIVE_PROVIDER_TESTS=1` with the
`live_provider` marker, or `pytest -m e2e` (see each provider doc's end-to-end section).
Unit tests replay captured screens in `test/providers/fixtures/*_output.txt`.

## What usually lies (all providers)

- **`installed: true` only means a binary with that name is on the server's `PATH`.**
  It says nothing about login, version or whether the CLI starts
  (`api/main.py:4214-4217`). `cursor_cli` looks for a binary called `agent`, so any
  `agent` on `PATH` counts (`api/main.py:4208`). `mock_cli` is not listed at all.
- **`GET /sessions/{name}/terminals` shows the last status the monitor applied.** It
  doesn't re-detect, so a terminal nothing polls can still say `processing` after it went
  idle ([api.md](../api.md), line 226). `GET /terminals/{id}` re-detects.
- **`mode=full` is not the whole history.** It's the rolling buffer, 32 KB by default
  ([api.md](../api.md), lines 276-280). The full log of a live terminal is
  `logs/terminal/<terminal-id>.log` in the CAO home. After the terminal is deleted, its
  saved screen is `logs/terminal/<terminal-id>.scrollback`
  (`services/terminal_service.py:6931-6932`).
- **The inbox delivers one pending message per idle cycle.** Several corrections sent
  to a busy worker are acted on one by one, each after a full turn, and a "stop" lands
  only after the worker has acted. Send one consolidated message
  (`services/inbox_service.py:244-248`).
- **`mock_cli` is not on `PATH` in an installed CAO.** `cao launch --provider mock_cli`
  fails outside a repo checkout until you add `test/providers/fixtures/bin`
  ([mock-cli-provider.md](../mock-cli-provider.md#design)). It also writes no MCP
  config, so it can't test MCP delivery. The opening list in mock-cli-provider.md names 9
  providers; there are 13.

## claude_code

- **An answer that never arrives: look for `Not sent — no agent named '<id>' is
  reachable` in the pane.** The agent replied with Claude Code's own `SendMessage`, not
  CAO's `send_message`, and the reply went nowhere. CAO now removes `SendMessage` and 13
  other Claude Code coordination tools from every agent whose grant is not `"*"`
  (`utils/tool_mapping.py:170-186`;
  [tool-restrictions.md](../tool-restrictions.md#claude-code-built-ins-cao-always-removes)).
  An agent with `"*"`, or one running on a CAO older than that change, still has it. Read
  the pane with `GET /terminals/<terminal-id>/output?mode=full`.
- **Ephemeral Claude Code helpers are cut down further.** `Agent`, `Task`, `Monitor`,
  `Skill`, `AskUserQuestion`, `EnterPlanMode` and `ExitPlanMode` are removed whatever the
  grant (`utils/tool_mapping.py:208-218`). A helper that "can't use skills" is expected:
  CAO skills stay reachable through the `load_skill` MCP tool.
- **Unknown `--disallowedTools` names are not a reliable deny.** The code records
  `TeamCreate` and `TeamDelete` being ignored by Claude Code 2.1.318
  (`utils/tool_mapping.py:165-167`); verify the effective deny list on your build.
- **`WAITING_USER_ANSWER` means a dialog is open, and the inbox holds messages until it
  closes.** Pasting into Claude Code's choice widget would answer the dialog, so even eager
  delivery (`CAO_EAGER_INBOX_DELIVERY=true`) skips that state
  (`services/inbox_service.py:370-384`; [claude-code.md](../claude-code.md#eager-inbox-delivery)).
- **A slow start that "times out" may still be running.** CAO answers the bypass and
  folder-trust dialogs and declines the Bedrock model-upgrade nudge
  (`providers/claude_code.py:947-965`). A dialog it doesn't know waits out
  `provider_init_timeout` (60 s). A supervisor already working when the wait ends can
  still count as started ([claude-code.md](../claude-code.md#how-cao-knows-claude-code-has-started)).

## codex

- **`UNKNOWN` right after you send a message is normal.** When Codex shows the user
  message with nothing under it yet, CAO can't tell a redraw from a turn that hasn't drawn
  its first row, so it returns `UNKNOWN` and the last status stays
  (`providers/codex.py:2861-2871`; `services/status_monitor.py:515-519`). The same
  happens for a frame with no status bar under the composer
  (`providers/codex.py:2838-2848`). Wait for the next poll before calling it stuck.
- **Your Codex sandbox settings don't apply to a default CAO worker.** CAO passes
  `--yolo` (`--dangerously-bypass-approvals-and-sandbox`), so the
  `[sandbox_*]` tables in `~/.codex/config.toml`, network settings included, are inert.
  `codexProfile` drops `--yolo`, but `allowedTools: ["*"]` forces it back
  (`providers/codex.py:1718-1771`; [codex-cli.md](../codex-cli.md#launch-flags)).
- **The launch command shows the reasoning effort CAO requested.** CAO sends
  `model_reasoning_effort` as a `-c` override (`providers/codex.py:1469`).
  Unverified: reported from use, and nothing in this repo backs it.
- **A Codex release with a new folder-trust dialog stalls startup.** CAO knows three
  versions of the dialog, the newest from Codex 0.156 (`providers/codex.py:251-263`).
  If a new release shows different text, the start waits out `provider_init_timeout`.

## kiro_cli

- **A Kiro terminal can show a status but refuse input.** CAO types into a `kiro_cli`
  terminal only after its identity check passes (`identity_verified: true`). Until then,
  input is refused whatever `status` says: transiently while it's still starting, and
  permanently if the start failed (`services/terminal_service.py:4083-4128`;
  `providers/kiro_cli.py:588`, `821`, `888`). Check `identity_verified` on
  `GET /sessions/{name}/terminals`. Other providers show `null`.
- **Kiro v3 runs only on listed kiro-cli versions.** 2.35.1 is measured. The listed
  versions from 2.36.0 through 3.1.0 are pending and refused unless the operator
  opts in with `kiro.v3_allow_unverified_versions`. Any other version is refused with
  `kiro_v3_unsupported_version`, and v2 is unaffected
  (`providers/kiro_v3_schema.py:393-404`;
  [kiro-cli.md](../kiro-cli.md#supported-kiro-cli-versions)). A newer kiro-cli on the
  machine is the usual cause.
- **v3 refuses a non-default Kiro home.** `KIRO_HOME` other than `~/.kiro` fails with
  `kiro_v3_home_unsupported` (`services/kiro_v3_launch.py:190-200`), and
  `CAO_AGENTS_DIR` fails with `kiro_v3_agents_dir_unsupported`. A test with a scratch Kiro
  home therefore can't launch v3. The refusal table is in
  [kiro-cli.md](../kiro-cli.md#when-a-v3-launch-is-refused).
- **CAO never answers Kiro's approval prompt on v3.** The terminal reads as
  `WAITING_USER_ANSWER` until you or a supervisor answer it
  ([kiro-cli.md](../kiro-cli.md#faq)).
- **On a loaded machine, a v3 start can outlast the client's wait.** With
  `provider_init_timeout` above about 90 s, `cao launch` reports a failure while the
  server goes on to start the agent ([kiro-cli.md](../kiro-cli.md#known-limitations)).

## Other providers

No in-depth entries yet. Facts checked so far:

- `hermes`: CAO writes no MCP config for it. Configure the CAO MCP server in the Hermes
  profile yourself ([hermes.md](../hermes.md#mcp-configuration)).
- `opencode_cli`: inbox delivery is polled, logged as `trigger: opencode-poll`
  (`services/inbox_service.py:1016-1027`).
- `cursor_cli`: the binary is called `agent` (see above).

Entries for `kimi_cli`, `qwen_cli`, `copilot_cli`, `antigravity_cli`, `omp`, `grok_cli`
and `mcode`: to come. Start from each provider's doc page.
