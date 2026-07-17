"""Managed-subprocess pytest fixtures.

The fixtures here spin up real ``cao-server`` subprocesses (with optional
Auth0 enforcement via an in-process JWKS HTTP server) and tear them down on
session exit. Other integration and end-to-end tests (WebSocket integration
smoke, Playwright browser e2e, MCP Apps iframe smoke) consume the same
harness.

Exports live in ``cao_server.py`` and are auto-discovered via the
``pytest_plugins`` entry in ``test/conftest.py``.
"""
