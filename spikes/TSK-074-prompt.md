# TSK-074 — Phase 2 Task 6: pure-Python tail in inbox_service

You are executing Phase 2 Task 6 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1–2 + audit (TSK-071) committed. Audit `spikes/TSK-071-result.md` UNIX-TOOLING section confirms `inbox_service.py:51-52` is the only `tail -n` subprocess in `src/`.
- Plan binding spec: `docs/PLAN-phase2.md` §4 (last paragraph) and §6 ("Replace `tail` subprocess assumptions in `test/services/test_inbox_service.py` with pure-Python tailing so Windows CI is possible").

## Goal

Replace the `tail -n` subprocess call in `src/cli_agent_orchestrator/services/inbox_service.py` (`_get_log_tail`, currently around lines 42–55) with a pure-Python last-N-lines reader. Same return semantics, same edge cases. Windows-compatible. Tests dropped from subprocess-mock to file-content.

## Implementation

A correct backward-tail reader for log files:

```python
def _get_log_tail(log_path: Path, n: int = 100) -> list[str]:
    """Read the last N lines of a log file. Pure-Python; Windows-safe.

    Returns lines as decoded strings (utf-8, errors='replace') without
    trailing newlines. If the file has fewer than N lines, returns all.
    Returns [] if the file does not exist or is empty.
    """
    if not log_path.exists():
        return []
    block = 4096
    lines: list[bytes] = []
    with open(log_path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        if end == 0:
            return []
        position = end
        carry = b""
        while position > 0 and len(lines) <= n:
            read_size = min(block, position)
            position -= read_size
            fh.seek(position)
            chunk = fh.read(read_size) + carry
            split = chunk.split(b"\n")
            carry = split[0]
            lines = split[1:] + lines
        if position == 0 and carry:
            lines = [carry] + lines
    decoded = [line.decode("utf-8", errors="replace") for line in lines]
    decoded = [line.rstrip("\r") for line in decoded]
    while decoded and decoded[-1] == "":
        decoded.pop()
    return decoded[-n:]
```

Match the existing function's exact signature, return type, and call-site behavior. Read the current implementation FIRST and confirm: parameter names, type hints, return type, and how callers consume the result. Adjust the snippet above to fit the project's actual API.

## Tests

Update `test/services/test_inbox_service.py`:
- Drop `subprocess.run` mocks for tail.
- Use `tmp_path` to write real log files of various shapes:
  - empty file
  - 1 line, no trailing newline
  - 1 line, trailing newline
  - exactly N lines
  - more than N lines (assert returns last N)
  - lines longer than the 4 KiB block boundary (verify carry-over)
  - mixed `\r\n` line endings (Windows-format logs)
  - utf-8 multi-byte characters at the block boundary (defensive — `errors='replace'` should keep this safe)
- Confirm the missing-file case returns `[]` without raising.

## Constraints (HARD)

- DO NOT change any other function in `inbox_service.py`.
- DO NOT change the public interface `_get_log_tail` exposes — same name, same param order, same return semantics.
- DO NOT add a fallback to subprocess `tail` — pure-Python only.
- DO NOT install or upgrade dependencies.
- Use `.venv\Scripts\python.exe -m pytest` (project's `rtk pytest` shim collects nothing).
- DO NOT commit or push. Produce a clean working-tree change for the supervising Opus to commit.

## Verification command sequence

```
.venv/Scripts/python.exe -m pytest test/services/test_inbox_service.py -x --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

Second run failure count must not exceed the 43-failure baseline.

## Reporting

Write `spikes/TSK-074-result.md`:

```markdown
# TSK-074 — Task 6 result

## Files touched
<list>

## Implementation summary
<one paragraph: signature preserved, block size, edge cases handled>

## Tests
- inbox_service suite: <N pass / M fail>
- full (excl. e2e): <N pass / M fail> — must be ≤43 fail

## Deviations
<any>
```

Echo: `TSK-074: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 6.

Begin.
