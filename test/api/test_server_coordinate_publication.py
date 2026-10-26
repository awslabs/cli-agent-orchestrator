"""``cao-server`` publishes its EFFECTIVE coordinates into the environment.

``--host`` / ``--port`` used to reach only ``uvicorn.run``, while
``server_coordinate_env()`` -- the single source for what a launched agent's
bundled ``cao-mcp-server`` is told -- reads the environment. A server started
with ``--port 9777`` and no ``CAO_API_PORT`` therefore handed its children
nothing and they reported to ``127.0.0.1:9889`` (#1066, found by the
independent review of the first fix).

``main()`` now reconciles ``os.environ`` with the resolved host/port before the
lifespan runs, so what the children are told is what uvicorn binds. A default
server with no conflicting environment publishes nothing, which is what keeps
its launch commands byte-identical.

``uvicorn.run`` is patched throughout: nothing binds a socket and no terminal
is created.
"""

import os
from argparse import Namespace
from typing import Dict, NamedTuple
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    server_coordinate_env,
)


class Launch(NamedTuple):
    """What one ``cao-server`` start did."""

    uvicorn: Dict[str, object]
    """The kwargs ``uvicorn.run`` was called with -- what the server BINDS."""
    coordinates: Dict[str, str]
    """What a child launched during startup INHERITS.

    Read from inside the patched ``uvicorn.run``: it blocks in real execution
    and the lifespan that can create terminals runs inside it, so a coordinate
    published after that call would be unreachable to every child.
    """


def _run_main(**overrides) -> Launch:
    """Run ``main()`` with CLI args and report the bind and the child's view."""
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
    coordinates: Dict[str, str] = {}
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=args),
        patch(
            "uvicorn.run", side_effect=lambda *_a, **_k: coordinates.update(server_coordinate_env())
        ) as run,
    ):
        from cli_agent_orchestrator.api.main import main

        main()
    assert run.call_count == 1
    return Launch(uvicorn=run.call_args.kwargs, coordinates=coordinates)


class TestFlagsReachTheChildren:
    """A coordinate given as a FLAG is forwarded, not just one given as env."""

    def test_port_flag_is_published_and_forwarded(self, default_cao_server):
        launch = _run_main(port=9777)
        assert launch.uvicorn["port"] == 9777, "uvicorn still binds the flag's port"
        assert os.environ["CAO_API_PORT"] == "9777"
        assert launch.coordinates["CAO_API_PORT"] == "9777"

    def test_host_flag_is_published_and_forwarded(self, default_cao_server):
        launch = _run_main(host="10.2.3.4")
        assert launch.uvicorn["host"] == "10.2.3.4"
        assert os.environ["CAO_API_HOST"] == "10.2.3.4"
        assert launch.coordinates["CAO_API_HOST"] == "10.2.3.4"

    def test_both_flags_together(self, default_cao_server):
        launch = _run_main(host="10.2.3.4", port=9777)
        assert launch.coordinates == {
            "CAO_API_HOST": "10.2.3.4",
            "CAO_API_PORT": "9777",
        }

    def test_the_flag_wins_over_a_conflicting_env_var(self, monkeypatch, default_cao_server):
        """Precedence is flag > env > default, and the CHILD must see the winner.

        Otherwise the child would be told a port uvicorn is not listening on.
        """
        monkeypatch.setenv("CAO_API_PORT", "9888")
        launch = _run_main(port=9777)
        assert launch.uvicorn["port"] == 9777
        assert launch.coordinates["CAO_API_PORT"] == "9777"


class TestAnExplicitDefaultFlagStillBeatsTheEnvironment:
    """The child coordinate must equal the effective bind -- always.

    Publishing only non-default values was not enough: a flag that spells out
    the default wins the precedence contest for uvicorn, yet published nothing,
    so a conflicting environment value survived and was handed to the children.
    They then reported to a port the server is not listening on, which is #1066
    again by a third route. Found by the independent re-review of round two.
    """

    def test_a_default_port_flag_clears_a_conflicting_env_var(
        self, monkeypatch, default_cao_server
    ):
        """The reviewer's exact probe: ``CAO_API_PORT=9777 cao-server --port 9889``."""
        monkeypatch.setenv("CAO_API_PORT", "9777")
        launch = _run_main(port=DEFAULT_SERVER_PORT)
        assert launch.uvicorn["port"] == DEFAULT_SERVER_PORT, "the flag still wins the bind"
        assert "CAO_API_PORT" not in launch.coordinates, (
            "the stale 9777 must not reach the children; with the variable gone "
            "they fall back to the same default the server bound"
        )
        assert "CAO_API_PORT" not in os.environ

    def test_a_default_host_flag_clears_a_conflicting_env_var(
        self, monkeypatch, default_cao_server
    ):
        monkeypatch.setenv("CAO_API_HOST", "10.2.3.4")
        launch = _run_main(host=DEFAULT_SERVER_HOST)
        assert launch.uvicorn["host"] == DEFAULT_SERVER_HOST
        assert "CAO_API_HOST" not in launch.coordinates
        assert "CAO_API_HOST" not in os.environ

    def test_both_defaults_clear_both_conflicts(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_HOST", "10.2.3.4")
        monkeypatch.setenv("CAO_API_PORT", "9777")
        launch = _run_main(host=DEFAULT_SERVER_HOST, port=DEFAULT_SERVER_PORT)
        assert launch.coordinates == {}

    def test_an_env_var_that_already_matches_the_default_flag_is_left_alone(
        self, monkeypatch, default_cao_server
    ):
        """Nothing to reconcile: the variable is the operator's and already true."""
        monkeypatch.setenv("CAO_API_PORT", str(DEFAULT_SERVER_PORT))
        launch = _run_main(port=DEFAULT_SERVER_PORT)
        assert launch.coordinates == {"CAO_API_PORT": str(DEFAULT_SERVER_PORT)}
        assert os.environ["CAO_API_PORT"] == str(DEFAULT_SERVER_PORT)


class TestDefaultServerPublishesNothing:
    """The default CAO's environment is left exactly as it was."""

    def test_no_flags_publishes_neither_coordinate(self, default_cao_server):
        launch = _run_main()
        assert launch.uvicorn["host"] == DEFAULT_SERVER_HOST
        assert launch.uvicorn["port"] == DEFAULT_SERVER_PORT
        assert launch.coordinates == {}
        assert "CAO_API_HOST" not in os.environ
        assert "CAO_API_PORT" not in os.environ

    def test_flags_spelling_out_the_defaults_publish_nothing(self, default_cao_server):
        """``--port 9889`` is still the default server, not an override."""
        launch = _run_main(host=DEFAULT_SERVER_HOST, port=DEFAULT_SERVER_PORT)
        assert launch.coordinates == {}
        assert "CAO_API_HOST" not in os.environ
        assert "CAO_API_PORT" not in os.environ


class TestPublicationHappensBeforeAnythingCanLaunch:
    """Order matters: a terminal created during startup must see the values."""

    def test_published_before_uvicorn_starts(self, default_cao_server):
        """Every assertion on ``launch.coordinates`` is made from inside the
        patched ``uvicorn.run`` (see ``Launch``); this one pins that the reading
        point is the one that matters, for the whole file."""
        assert _run_main(port=9777).coordinates == {"CAO_API_PORT": "9777"}


@pytest.mark.parametrize("port", [9777, DEFAULT_SERVER_PORT])
def test_the_lifespan_always_records_the_resolved_port(port, default_cao_server):
    """Unchanged behaviour, re-pinned alongside the new publication (#406)."""
    from cli_agent_orchestrator.api.main import app

    _run_main(port=port)
    assert app.state.server_port == port
