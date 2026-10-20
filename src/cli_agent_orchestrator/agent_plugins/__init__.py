"""Agent Plugins 1.0.0 support — the portable open specification.

This package implements CAO's **client side** of the `Agent Plugins 1.0.0
<https://agent-plugins.org/specification>`_ specification: a thin
resolve → validate → install → deliver pipeline over plugin directories that
carry a ``plugin.json`` manifest, an optional ``skills/`` tree, and (Increment 2)
an optional ``mcp.json``.

**This is not CAO's event-plugin system.** ``cli_agent_orchestrator.plugins``
(``PluginRegistry``, the ``cao.plugins`` entry-point group) is a separate,
unrelated subsystem for reacting to CAO lifecycle events, and this package
neither imports nor modifies it. The two are deliberately siblings rather than
nested so ``from cli_agent_orchestrator.plugins import ...`` stays unambiguous
at every import site.

Public surface
--------------
Importing this module is cheap: it re-exports the data models and the total
validator only. The installer, resolver, store, and projection engine are
imported from their own modules by the CLI/API layers that need them, keeping
the terminal-launch hot path free of plugin-management imports.
"""

from cli_agent_orchestrator.agent_plugins.models import (
    Author,
    DiscoveredSkill,
    Finding,
    InstallOutcome,
    MappedServer,
    PluginManifest,
    PluginRecord,
    PluginSource,
    PluginValidationReport,
    Severity,
    UninstallOutcome,
)
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

__all__ = [
    "Author",
    "DiscoveredSkill",
    "Finding",
    "InstallOutcome",
    "MappedServer",
    "PluginManifest",
    "PluginRecord",
    "PluginSource",
    "PluginValidationReport",
    "Severity",
    "UninstallOutcome",
    "validate_plugin",
]
