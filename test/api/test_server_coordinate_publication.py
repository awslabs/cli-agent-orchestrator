"""``cao-server`` publishes its EFFECTIVE coordinates into the environment.

``--host`` / ``--port`` used to reach only ``uvicorn.run``, while
``server_coordinate_env()`` -- the single source for what a launched agent's
bundled ``cao-mcp-server`` is told -- reads the environment. A server started
with ``--port 9777`` and no ``CAO_API_PORT`` therefore handed its children
nothing and they reported to ``127.0.0.1:9889`` (#1066, found by the
independent review of the first fix).

``main()`` now writes the resolved non-default host/port into ``os.environ``
before the lifespan runs, so what the children are told is what uvicorn binds.
A default server publishes nothing, which is what keeps its launch commands
byte-identical.

``uvicorn.run`` is patched throughout: nothing binds a socket and no terminal
is created.
"""

import os
from argparse import Namespace
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    server_coordinate_env,
)


def _run_main(**overrides):
    """Run ``main()`` with CLI args, returning the ``uvicorn.run`` call kwargs."""
    args = Namespace(
        **{
            "agents_dir": None,
            "host": None,
            "port": None,
            "terminal": None,
            "decision": [],
            **overrides,
        }
    )
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=args),
        patch("uvicorn.run") as run,
    ):
        from cli_agent_orchestrator.api.main import main

        main()
    assert run.call_count == 1
    return run.call_args.kwargs


class TestFlagsReachTheChildren:
    """A coordinate given as a FLAG is forwarded, not just one given as env."""

    def test_port_flag_is_published_and_forwarded(self, default_cao_server):
        kwargs = _run_main(port=9777)
        assert kwargs["port"] == 9777, "uvicorn still binds the flag's port"
        assert os.environ["CAO_API_PORT"] == "9777"
        assert server_coordinate_env()["CAO_API_PORT"] == "9777"

    def test_host_flag_is_published_and_forwarded(self, default_cao_server):
        kwargs = _run_main(host="10.2.3.4")
        assert kwargs["host"] == "10.2.3.4"
        assert os.environ["CAO_API_HOST"] == "10.2.3.4"
        assert server_coordinate_env()["CAO_API_HOST"] == "10.2.3.4"

    def test_both_flags_together(self, default_cao_server):
        _run_main(host="10.2.3.4", port=9777)
        assert server_coordinate_env() == {
            "CAO_API_HOST": "10.2.3.4",
            "CAO_API_PORT": "9777",
        }

    def test_the_flag_wins_over_a_conflicting_env_var(self, monkeypatch, default_cao_server):
        """Precedence is flag > env > default, and the CHILD must see the winner.

        Otherwise the child would be told a port uvicorn is not listening on.
        """
        monkeypatch.setenv("CAO_API_PORT", "9888")
        kwargs = _run_main(port=9777)
        assert kwargs["port"] == 9777
        assert server_coordinate_env()["CAO_API_PORT"] == "9777"


class TestDefaultServerPublishesNothing:
    """The default CAO's environment is left exactly as it was."""

    def test_no_flags_publishes_neither_coordinate(self, default_cao_server):
        kwargs = _run_main()
        assert kwargs["host"] == DEFAULT_SERVER_HOST
        assert kwargs["port"] == DEFAULT_SERVER_PORT
        assert server_coordinate_env() == {}

    def test_flags_spelling_out_the_defaults_publish_nothing(self, default_cao_server):
        """``--port 9889`` is still the default server, not an override."""
        _run_main(host=DEFAULT_SERVER_HOST, port=DEFAULT_SERVER_PORT)
        assert server_coordinate_env() == {}


class TestPublicationHappensBeforeAnythingCanLaunch:
    """Order matters: a terminal created during startup must see the values."""

    def test_published_before_uvicorn_starts(self, default_cao_server):
        """``uvicorn.run`` blocks in real execution, and the lifespan (which can
        create terminals) runs inside it. Anything published after that call
        would be unreachable, so the assertion is made from inside the patched
        ``uvicorn.run`` itself."""
        seen = {}

        def record(*_args, **_kwargs):
            seen.update(server_coordinate_env())

        args = Namespace(agents_dir=None, host=None, port=9777, terminal=None, decision=[])
        with (
            patch("argparse.ArgumentParser.parse_args", return_value=args),
            patch("uvicorn.run", side_effect=record),
        ):
            from cli_agent_orchestrator.api.main import main

            main()
        assert seen == {"CAO_API_PORT": "9777"}


@pytest.mark.parametrize("port", [9777, DEFAULT_SERVER_PORT])
def test_the_lifespan_always_records_the_resolved_port(port, default_cao_server):
    """Unchanged behaviour, re-pinned alongside the new publication (#406)."""
    from cli_agent_orchestrator.api.main import app

    _run_main(port=port)
    assert app.state.server_port == port
