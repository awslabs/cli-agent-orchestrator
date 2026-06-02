"""Built-in CAO plugins bundled with the package.

These plugins ship in-tree so a stock ``pip install cli-agent-orchestrator``
gets memory-context injection for Claude Code and Kiro CLI out of the box.
They are registered via the ``cao.plugins`` entry point in the top-level
``pyproject.toml`` so discovery uses the same path as third-party plugins.
"""
