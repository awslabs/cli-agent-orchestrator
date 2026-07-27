"""``cao tui`` — the thin-shell front door for CLI Agent Orchestrator.

This package is a *thin shell*: a prompt_toolkit terminal UI that composes the
existing ``cao`` CLI surface and reads live state over HTTP. It reaches
Backplane state exclusively through the FastAPI REST surface (``requests``) and
must never import the heavy in-process layers (``services``, ``clients``,
``backends``, ``providers``, ``models``) or the ``cli`` command modules /
``cli`` object. That invariant (NFR-2 / SC-2) is locked by the AST guard in
``test/tui/test_thin_shell_boundary.py``.

Permitted imports for every module under this package:
    - the Python standard library
    - ``prompt_toolkit`` (the UI toolkit; ADR-004)
    - ``requests`` (HTTP to cao-server)
    - ``cli_agent_orchestrator.constants``
    - ``cli_agent_orchestrator.utils.path_validation``

``main()`` is the ``cao tui`` entry callable. Wiring it into ``cli/main.py`` is
deferred to U5 (RD-b=A); U1 ships the callable but does not register it.
"""

from __future__ import annotations

from cli_agent_orchestrator.tui.app import App, main

__all__ = ["App", "main"]
