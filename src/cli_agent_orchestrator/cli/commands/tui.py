"""`cao tui` — launch the bundled Rust terminal UI (issue #321).

The TUI is a compiled Rust binary that ships inside the wheel, built from the root
sibling ``tui/`` crate by ``scripts/build_tui.py`` and bundled via the fourth
``[tool.hatch.build] artifacts`` glob. This command locates it and execs it.

Three things here are load-bearing:

1. **The binary is located via ``importlib.resources``**, not by a path relative to
   ``__file__``. Same reason ``api/main.py:3697`` does it for ``web_ui/``: it resolves
   correctly for an editable install (``uv sync``) and a wheel install
   (``uv tool install``, ``pip install``) alike.

2. **The child is invoked with an argv VECTOR — never an interpolated shell string.**
   No ``shell=True``, no f-string command. The TUI reaches CAO over a process boundary
   and nothing else (no FFI, no embedded interpreter), and a shell in that path would
   turn any operator-supplied argument into an injection surface. (T-10)

3. **Failures surface as ``click.ClickException``** so the operator gets one styled line
   and never a traceback — the boundary contract that ``launch.py``, ``session.py``,
   ``shutdown.py``, ``terminal.py`` and ``workflow.py`` all follow.
"""

import os
import subprocess
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import List, Optional

import click

# Must match `[[bin]] name` in tui/Cargo.toml and the artifacts glob in pyproject.toml.
# scripts/build_tui.py cross-checks all three and fails the build if they diverge.
_BINARY_STEM = "cao-tui"


def _binary_filename() -> str:
    """Platform-correct filename for the bundled binary."""
    return f"{_BINARY_STEM}.exe" if os.name == "nt" else _BINARY_STEM


def _locate_binary() -> Optional[Path]:
    """Path to the bundled TUI binary, or None if it is not in this install.

    Anchored to the package via ``importlib.resources`` so it works for both editable
    and wheel installs — the ``api/main.py:3697`` pattern. Returns None rather than
    raising so the caller owns the operator-facing message; a bare
    ``FileNotFoundError`` from here would reach the operator as a traceback.
    """
    try:
        candidate = Path(str(_pkg_files("cli_agent_orchestrator") / _binary_filename()))
    except (ModuleNotFoundError, TypeError) as exc:  # pragma: no cover - defensive
        raise click.ClickException(
            f"could not locate the cli_agent_orchestrator package to find the TUI binary: {exc}"
        )
    return candidate if candidate.is_file() else None


def _missing_binary_message(expected: Path) -> str:
    """The operator-facing explanation for an absent binary: which, where, what to do.

    A platform-mismatched or partially built wheel is the likely cause, and both are
    invisible from the error alone — hatchling silently omits an artifacts glob that
    matched nothing at build time (defect D1's failure mode), so the wheel installs
    cleanly and only fails here. The message therefore names the exact missing file and
    both remedies rather than leaving the operator to guess.

    THE MOST LIKELY CAUSE CHANGED WITH #560. The build now compiles the binary itself when
    cargo is available, so "installed from source and forgot to build it" is no longer the
    common case — a source install without a Rust toolchain is. The old wording sent an
    operator to `python scripts/build_tui.py build`, which is unavailable from an INSTALLED
    package (scripts/ ships in the sdist, not the wheel), so the first advice they got could
    not be followed. Reinstalling with cargo present is the actionable fix. (#560)
    """
    return (
        f"the TUI binary '{_binary_filename()}' is not present in this installation "
        f"(expected at: {expected}).\n"
        "This usually means one of two things:\n"
        "  1. You installed from source without a Rust toolchain, so the binary could not\n"
        "     be compiled. Install Rust (https://rustup.rs), then reinstall CAO — the\n"
        "     build compiles and bundles the TUI automatically when cargo is present.\n"
        "     From a source checkout you can also build it directly:\n"
        "         python scripts/build_tui.py build\n"
        "  2. The installed wheel was built without the Rust binary, or for a different\n"
        "     platform. Reinstall a wheel built for this platform.\n"
        "Everything else in CAO works without it — only `cao tui` needs this binary."
    )


@click.command(
    context_settings={
        # Pass unrecognised flags straight through to the Rust binary instead of having
        # Click reject them. The TUI owns its own argument surface; mirroring it here
        # would be two sources of truth that drift.
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.argument("tui_args", nargs=-1, type=click.UNPROCESSED)
def tui(tui_args):
    """Launch the terminal UI (bundled Rust binary)."""
    try:
        binary = _locate_binary()
        if binary is None:
            expected = Path(str(_pkg_files("cli_agent_orchestrator") / _binary_filename()))
            raise click.ClickException(_missing_binary_message(expected))

        if not os.access(binary, os.X_OK):
            raise click.ClickException(
                f"the TUI binary at {binary} is not executable. Rebuild it with "
                "`python scripts/build_tui.py build`, or fix its mode with "
                f"`chmod +x {binary}`"
            )

        # argv VECTOR — never a shell string. No shell=True, no f-string command, so
        # operator-supplied arguments in `tui_args` are passed as literal argv entries
        # and cannot be reinterpreted by a shell. (T-10, issue #321)
        command: List[str] = [str(binary), *tui_args]

        try:
            result = subprocess.run(command)
        except OSError as exc:
            # Covers the exec-time failures a stat cannot predict: a binary built for
            # the wrong architecture, a corrupt file, a missing loader.
            raise click.ClickException(
                f"failed to execute the TUI binary at {binary}: {exc}. If this wheel was "
                "built for a different platform, reinstall one built for this one"
            )

        # Propagate the child's exit code so scripts wrapping `cao tui` see the truth.
        # ClickException always exits 1, so a non-zero child code goes through Exit.
        if result.returncode != 0:
            raise click.exceptions.Exit(result.returncode)

    except (click.ClickException, click.exceptions.Exit, click.Abort):
        raise
    except Exception as e:
        raise click.ClickException(str(e))
