"""``cao tui`` entry command — launches the terminal UI front door (U5).

This is the single wiring point that makes the ``cao tui`` subcommand real
(RD-b=A). Bare ``cao`` is left untouched: it still prints the group help. Only
``cao tui`` launches the prompt_toolkit shell.

The import of the TUI application is **lazy** (performed inside the callback,
not at module top) so that merely loading the CLI group — which happens on
every ``cao`` invocation — never pays the prompt_toolkit import cost. The shell
is constructed and run only when the user explicitly types ``cao tui``.
"""

import click


@click.command()
def tui() -> None:
    """Launch the cao terminal UI front door."""

    # Lazy import: keep bare-``cao`` startup cheap by deferring the
    # prompt_toolkit-backed shell import until the command actually runs.
    from cli_agent_orchestrator.tui.app import main as tui_main

    # Propagate the shell's integer exit code as the process exit status.
    raise SystemExit(tui_main())
