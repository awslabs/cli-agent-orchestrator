#!/usr/bin/env python3
"""Ratchet ``mypy src/`` against a committed baseline so the type gate can block.

issue #1005. Runs in ``ci.yml``'s ``Code Quality`` job, replacing the
``continue-on-error: true`` mypy step that made a red type check cost nothing.

WHY A RATCHET AND NOT A CLEANUP SPRINT
--------------------------------------
Main carries 177 pre-existing errors in 22 files. Two options were available:

1. Fix all 177, then make ``mypy src/`` blocking. That is a multi-week cleanup touching
   ``services/agui/run_plane.py`` (69 errors), ``utils/agent_profiles.py`` (20) and 20 other
   modules, and until it lands the gate stays open — which is how the repo got here.
2. Freeze the current count, block anything that makes it worse, and let it fall.

This script is option 2. It makes the gate binding *today* without asking anyone to fix
someone else's module first, which is the only version of the gate that actually ships.

WHAT COUNTS AS A REGRESSION
---------------------------
The baseline is **per-file, per-error-code counts**, not a line-numbered error list and not a
single repo-wide total. Each of the alternatives fails in a specific way:

- A line-numbered error list goes stale on every unrelated edit: insert one import at the top
  of a file and every baselined error below it "moves", so a pure-reformat PR shows dozens of
  new-and-removed errors. Reviewers learn to run ``--update`` reflexively and the ratchet
  becomes a rubber stamp.
- A single repo-wide total lets a PR add an ``arg-type`` error in one module and delete an
  unrelated ``no-any-return`` in another and net to zero. That is exactly the regression the
  gate exists to catch.

Per-file-per-code is the coarsest key that still refuses both. It tolerates moving an error
around inside a file (a real non-event) and refuses adding a new *kind* of error to a file,
or more of an existing kind.

The check is one-directional. Counts that **fall** pass, and the script says so and prints
the ``--update`` command to bank the improvement. Counts that **rise**, new files, and new
error codes in a baselined file all fail.

FAILING CLOSED
--------------
A ratchet that cannot fail is worse than no ratchet, because it reads as green. Three
properties keep this one honest:

1. **mypy crashing is a FAILED check, not a passed one.** mypy exits 0 with no errors, 1 with
   errors, and 2 on an internal error or bad config. Exit 2, or a non-zero exit with zero
   parsed error lines, is treated as a hard failure rather than "no errors found".
2. **A missing or unparseable baseline fails with a clear error.** Silently treating it as
   empty would still fail -- every current error would compare as a regression against
   nothing -- but as a confusing wall of per-file regressions instead of a clear
   "baseline not found / malformed" diagnostic.
3. **The raw mypy output is always echoed** to the job log before any verdict, so the gate
   never hides the diagnostics it is gating on.

mypy's own version is recorded in the baseline. A different version reports different errors,
so a mismatch prints a warning naming both versions — the check still runs (a dependabot mypy
bump should be able to go red for real reasons), but the reason for unexpected churn is right
there in the log.

USAGE
-----
::

    python scripts/mypy_ratchet.py             # check against the committed baseline
    python scripts/mypy_ratchet.py --update    # rewrite the baseline from current mypy output

Standard library only: this runs in a job that has already done ``uv sync``, but the script
must not need anything the repo does not already install for mypy itself.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "mypy-baseline.json"
DEFAULT_TARGETS = ("src/",)

# ``path/to/file.py:12: error: Some message  [error-code]``
#
# Only ``error:`` lines are counted. ``note:`` lines are continuation detail attached to the
# error above them, so counting them would double-count a single diagnostic, and mypy's
# ``mypy.ini: note: unused section(s): ...`` is config hygiene rather than a type error.
_ERROR_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+)(?::\d+)?: error: (?P<message>.*?)(?:\s+\[(?P<code>[^\]]+)\])?$"
)

# mypy omits the bracketed code for a few diagnostics (and for output produced with
# ``--hide-error-codes``). They still need a stable bucket.
_NO_CODE = "<no-code>"

ErrorCounts = Dict[str, Dict[str, int]]


class RatchetError(RuntimeError):
    """A failure in the ratchet machinery itself, as opposed to a type-error regression."""


def run_mypy(targets: Sequence[str]) -> Tuple[str, int]:
    """Run mypy over ``targets`` and return its combined output and exit code.

    Invoked as ``python -m mypy`` through the *current* interpreter so the script type-checks
    with the same environment that is running it. In CI that is ``uv run``'s venv, which is
    the environment ``uv run mypy src/`` used before this script existed.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *targets],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr, proc.returncode


def mypy_version() -> str:
    """Return mypy's reported version, or ``"unknown"`` if it cannot be determined."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--version"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def parse_errors(output: str) -> ErrorCounts:
    """Group mypy's ``error:`` lines into ``{path: {error_code: count}}``.

    Paths are normalised to forward slashes relative to the repo root so a baseline produced
    on one platform compares equal on another.
    """
    counts: ErrorCounts = defaultdict(lambda: defaultdict(int))
    for raw_line in output.splitlines():
        match = _ERROR_RE.match(raw_line.strip())
        if match is None:
            continue
        path = _normalise_path(match.group("path"))
        code = match.group("code") or _NO_CODE
        counts[path][code] += 1
    return {path: dict(codes) for path, codes in counts.items()}


def _normalise_path(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(REPO_ROOT)
        except ValueError:
            pass
    return candidate.as_posix()


def total_errors(counts: ErrorCounts) -> int:
    return sum(sum(codes.values()) for codes in counts.values())


def compare(baseline: ErrorCounts, current: ErrorCounts) -> Tuple[List[str], List[str]]:
    """Diff ``current`` against ``baseline``.

    Returns ``(regressions, improvements)``, each a list of human-readable lines. A
    regression is any ``(file, code)`` bucket whose count rose above its baseline, including
    buckets absent from the baseline entirely. An improvement is any bucket whose count fell,
    including buckets that reached zero.
    """
    regressions: List[str] = []
    improvements: List[str] = []

    for path in sorted(set(baseline) | set(current)):
        base_codes = baseline.get(path, {})
        cur_codes = current.get(path, {})
        for code in sorted(set(base_codes) | set(cur_codes)):
            was = base_codes.get(code, 0)
            now = cur_codes.get(code, 0)
            if now > was:
                regressions.append(f"{path}: [{code}] {was} -> {now} (+{now - was})")
            elif now < was:
                improvements.append(f"{path}: [{code}] {was} -> {now} (-{was - now})")

    return regressions, improvements


def load_baseline(path: Path) -> ErrorCounts:
    """Read and validate the committed baseline.

    Raises :class:`RatchetError` rather than silently treating a missing or malformed
    baseline as empty. That would still fail every check -- every current error would
    compare as a regression against nothing -- but as a confusing wall of per-file
    regressions instead of a clear "baseline not found / malformed" diagnostic.
    """
    if not path.exists():
        raise RatchetError(
            f"baseline file not found: {path}\n"
            f"Generate it with: python {_rel(Path(__file__))} --update"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RatchetError(f"baseline file {path} is not readable JSON: {exc}") from exc

    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise RatchetError(f"baseline file {path} has no 'files' object; regenerate it")

    counts: ErrorCounts = {}
    for file_path, codes in files.items():
        if not isinstance(codes, dict) or not all(
            isinstance(value, int) for value in codes.values()
        ):
            raise RatchetError(
                f"baseline file {path} entry for {file_path!r} is malformed; regenerate it"
            )
        counts[str(file_path)] = {str(code): int(value) for code, value in codes.items()}
    return counts


def baseline_metadata(path: Path) -> Dict[str, object]:
    """Return the baseline's non-count fields, or an empty dict if unavailable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_baseline(path: Path, counts: ErrorCounts, targets: Sequence[str]) -> None:
    payload = {
        "_comment": (
            "Per-file, per-error-code mypy baseline for scripts/mypy_ratchet.py (issue #1005). "
            "This number may only go DOWN. Regenerate with "
            "`python scripts/mypy_ratchet.py --update`; raising any count needs a reviewer's "
            "explicit sign-off in the PR."
        ),
        "mypy_version": mypy_version(),
        "targets": list(targets),
        "total_errors": total_errors(counts),
        "files": {path_: dict(sorted(codes.items())) for path_, codes in sorted(counts.items())},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _echo(lines: Iterable[str]) -> None:
    for line in lines:
        print(line)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from the current mypy output instead of checking against it",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"baseline file (default: {_rel(DEFAULT_BASELINE)})",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help=f"paths to type-check (default: {' '.join(DEFAULT_TARGETS)})",
    )
    args = parser.parse_args(argv)
    targets = args.targets or list(DEFAULT_TARGETS)

    output, exit_code = run_mypy(targets)

    # Always show the diagnostics before judging them: the gate must not hide its own input.
    print(f"$ mypy {' '.join(targets)}")
    _echo(output.splitlines())
    print()

    current = parse_errors(output)

    # mypy: 0 = clean, 1 = type errors found, 2 = internal error / bad config / bad CLI usage.
    if exit_code not in (0, 1):
        print(f"FAIL: mypy exited {exit_code} (internal error or bad configuration).")
        return 1
    if exit_code == 1 and not current:
        print(
            "FAIL: mypy exited 1 but no 'error:' lines were parsed. Its output format may "
            "have changed; the ratchet refuses to pass on output it cannot read."
        )
        return 1

    if args.update:
        write_baseline(args.baseline, current, targets)
        print(
            f"Wrote {_rel(args.baseline)}: {total_errors(current)} errors in "
            f"{len(current)} files ({mypy_version()})."
        )
        return 0

    try:
        baseline = load_baseline(args.baseline)
    except RatchetError as exc:
        print(f"FAIL: {exc}")
        return 1

    metadata = baseline_metadata(args.baseline)
    recorded_version = metadata.get("mypy_version")
    running_version = mypy_version()
    if isinstance(recorded_version, str) and recorded_version not in ("", "unknown"):
        if recorded_version != running_version:
            print(
                f"WARNING: baseline was recorded with {recorded_version!r} but "
                f"{running_version!r} is running. Different mypy versions report different "
                "errors; unexpected churn below is probably the version bump."
            )

    regressions, improvements = compare(baseline, current)
    base_total = total_errors(baseline)
    cur_total = total_errors(current)

    if improvements:
        print(f"Improvements ({len(improvements)}):")
        _echo(f"  {line}" for line in improvements)
        print()

    if regressions:
        print(f"Regressions ({len(regressions)}):")
        _echo(f"  {line}" for line in regressions)
        print()
        print(
            f"FAIL: mypy regressed. Baseline {base_total} errors, now {cur_total}.\n"
            "Fix the errors above, or -- if raising the baseline is genuinely the right call "
            "-- run `python scripts/mypy_ratchet.py --update`, commit the baseline, and say in "
            "the PR why it had to go up. A reviewer must sign off on that explicitly."
        )
        return 1

    if improvements:
        print(
            f"PASS: no new mypy errors, and {base_total - cur_total} fewer than the baseline.\n"
            "Bank the win: run `python scripts/mypy_ratchet.py --update` and commit "
            f"{_rel(args.baseline)}."
        )
        return 0

    print(f"PASS: no new mypy errors ({cur_total} errors, matching the baseline).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
