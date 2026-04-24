# TSK-078 — Task 8 result

## Files touched
- `src/cli_agent_orchestrator/multiplexers/launch.py`
- `src/cli_agent_orchestrator/providers/codex.py`
- `test/providers/test_codex_provider_unit.py`

## build_launch_spec behavior verified
| provider | platform | argv[0] | extra flags |
|---|---|---|---|
| `codex` | `windows` | resolved `codex.cmd` shim when available via `CAO_CODEX_BIN` / `shutil.which("codex.cmd")` / Scoop path scan | Windows Codex command builder adds `-c hooks=[]` plus existing `--yolo --no-alt-screen --disable shell_snapshot` |
| `codex` | `windows` (no shim found) | bare `codex` | same Codex Windows flags remain in provider command builder |
| `codex` | `unix` | bare `codex` | no `hooks=[]`; existing `--yolo --no-alt-screen --disable shell_snapshot` only |
| non-`codex` providers | `windows` / `unix` | bare command name unchanged | none |

## Codex initialize() warm-up branching
| multiplexer | direct_spawned | warm-up runs? |
|---|---:|---|
| `WezTermMultiplexer` | `True` | No |
| `WezTermMultiplexer` | `False` | Yes |
| `TmuxMultiplexer` | `False` | Yes |

## Tests
- targeted (codex+claude unit + multiplexers): `185 pass / 0 fail`

## Deviations
- The backend launch-template helper and Codex provider branching are in place, but the actual session-creation handoff of `launch_spec` into `create_session(..., launch_spec=...)` was not wired here because `src/cli_agent_orchestrator/services/terminal_service.py` was explicitly out of scope for this task. `CodexProvider.initialize()` now honors `_direct_spawned` when supplied, so Task 9 can connect the service-layer spawn path without reworking the provider again.
