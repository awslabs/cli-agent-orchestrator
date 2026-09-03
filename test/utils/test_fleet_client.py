"""Tests for the broker client behind `cao fleet` and `cao worker`.

``utils/fleet.py`` is the whole distance between two command groups and a remote
cluster, and every property pinned here is one where a wrong answer costs an
operator time rather than merely returning the wrong value:

- **The token rides one header name.** The broker answers `Authorization:
  Bearer` with a 401, so renaming the header reads at the other end as "my
  token is wrong" and sends the reader to the wrong problem entirely.
- **`release` is idempotent.** `cao fleet shutdown` walks a list the broker's
  own reaper is concurrently settling, so a 404 part-way through the walk is
  the ordinary case, not a failure.
- **The broker's `detail` survives.** Its 409 names which lease settled and
  why; its 404 on a proxied path means "that route is not allowlisted" rather
  than "no such worker". A bare `raise_for_status` replaces both with a number.
- **A session name is quoted into the path.** Session names come back from the
  worker, not from the caller, so an unquoted one addresses another route.
- **`follow_logs` sets no read timeout**, because a quiet log is not a stalled
  one -- and a 60s read timeout on a followed log looks exactly like a worker
  that died.

There is no live broker anywhere in this file, and no `kubernetes` import. That
is the module's design claim -- four HTTP routes and one token -- so stubbing
`requests` is what makes the claim checkable at all.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import requests

from cli_agent_orchestrator.constants import (
    ELASTIC_BROKER_TOKEN_ENV,
    ELASTIC_BROKER_TOKEN_HEADER,
    ELASTIC_BROKER_URL_ENV,
)
from cli_agent_orchestrator.utils.fleet import LIVE_STATES, FleetClient


@pytest.fixture(autouse=True)
def _no_broker_in_env(monkeypatch):
    """A developer with a port-forward open must not change these results.

    Anyone working on a fleet has both of these exported; without this the
    `from_env` failure cases pass on CI and fail on their machine.
    """
    monkeypatch.delenv(ELASTIC_BROKER_URL_ENV, raising=False)
    monkeypatch.delenv(ELASTIC_BROKER_TOKEN_ENV, raising=False)


@pytest.fixture
def client():
    return FleetClient("http://broker:9890", "tok")


def _response(status=200, payload=None, text="", json_error=False):
    resp = MagicMock(status_code=status, text=text)
    if json_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = payload
    return resp


class TestFromEnv:
    def test_reads_both_variables(self, monkeypatch):
        monkeypatch.setenv(ELASTIC_BROKER_URL_ENV, "http://127.0.0.1:9890")
        monkeypatch.setenv(ELASTIC_BROKER_TOKEN_ENV, "s3cret")

        built = FleetClient.from_env()

        assert built.url == "http://127.0.0.1:9890"
        assert built._headers == {ELASTIC_BROKER_TOKEN_HEADER: "s3cret"}

    def test_strips_whitespace_and_one_trailing_slash(self, monkeypatch):
        """A pasted port-forward URL routinely arrives with both."""
        monkeypatch.setenv(ELASTIC_BROKER_URL_ENV, "  http://127.0.0.1:9890/  ")
        monkeypatch.setenv(ELASTIC_BROKER_TOKEN_ENV, "  s3cret  ")

        built = FleetClient.from_env()

        assert built.url == "http://127.0.0.1:9890"
        assert built._headers[ELASTIC_BROKER_TOKEN_HEADER] == "s3cret"

    @pytest.mark.parametrize(
        "url,token",
        [
            (None, "s3cret"),
            ("http://127.0.0.1:9890", None),
            ("", "s3cret"),
            ("http://127.0.0.1:9890", "   "),
        ],
        ids=["no-url", "no-token", "blank-url", "blank-token"],
    )
    def test_missing_or_blank_either_var_names_both_and_the_port_forward(
        self, monkeypatch, url, token
    ):
        if url is not None:
            monkeypatch.setenv(ELASTIC_BROKER_URL_ENV, url)
        if token is not None:
            monkeypatch.setenv(ELASTIC_BROKER_TOKEN_ENV, token)

        with pytest.raises(click.ClickException) as exc:
            FleetClient.from_env()

        message = exc.value.format_message()
        # Both names, because the reader does not know which one they forgot,
        # and the port-forward, because that is the step nothing else hints at.
        assert ELASTIC_BROKER_URL_ENV in message
        assert ELASTIC_BROKER_TOKEN_ENV in message
        assert "port-forward" in message


class TestTransport:
    def test_the_token_travels_in_the_broker_header_and_nowhere_else(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(payload=[]),
        ) as request:
            client.workers()

        headers = request.call_args.kwargs["headers"]
        assert headers == {ELASTIC_BROKER_TOKEN_HEADER: "tok"}
        # The broker 401s a bearer token, so sending one as well would turn a
        # working call into an authentication mystery.
        assert "Authorization" not in headers

    def test_url_method_and_split_timeouts(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(payload=[]),
        ) as request:
            client.workers()

        assert request.call_args.args[:2] == ("GET", "http://broker:9890/workers")
        connect, read = request.call_args.kwargs["timeout"]
        # Connect fast, read patiently: a slow connect means no broker, a slow
        # read means a worker is thinking.
        assert connect < read

    def test_a_connection_error_names_the_broker_it_could_not_reach(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            side_effect=requests.ConnectionError("refused"),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        message = exc.value.format_message()
        assert "http://broker:9890" in message
        assert "refused" in message

    def test_logs_passes_the_tail_through(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(text="line\n"),
        ) as request:
            assert client.logs("w1", tail_lines=6) == "line\n"

        assert request.call_args.args[1].endswith("/workers/w1/logs")
        assert request.call_args.kwargs["params"] == {"tail_lines": 6}


class TestRelease:
    def test_a_404_counts_as_released(self, client):
        """The reaper settles leases while `shutdown` is walking the list."""
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=404, payload={"detail": "no such worker"}),
        ):
            assert client.release("gone") is True

    def test_success_returns_true(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=200, payload={"released": True}),
        ):
            assert client.release("w1") is True

    def test_any_other_error_still_raises(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=500, payload={"detail": "broker is unwell"}),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.release("w1")

        assert "broker is unwell" in exc.value.format_message()


class TestErrorsKeepTheBrokersSentence:
    def test_detail_is_surfaced_verbatim(self, client):
        detail = "lease 76372746 already settled: expired (no completion within 900s)"
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=409, payload={"detail": detail}),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        assert exc.value.format_message() == detail

    def test_401_without_a_body_points_at_the_token_variable(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=401, payload={}),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        assert ELASTIC_BROKER_TOKEN_ENV in exc.value.format_message()

    def test_a_non_json_body_falls_back_to_its_text(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(
                status=502, text="  <html>bad gateway</html>  ", json_error=True
            ),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        assert exc.value.format_message() == "<html>bad gateway</html>"

    def test_an_empty_body_falls_back_to_the_status_code(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=503, text="", json_error=True),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        assert "503" in exc.value.format_message()

    def test_a_json_body_that_is_not_an_object_is_shown_as_json(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=422, payload=[{"loc": ["body"], "msg": "bad"}]),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.workers()

        assert json.loads(exc.value.format_message()) == [{"loc": ["body"], "msg": "bad"}]


class TestProxiedRoutes:
    """The `/workers/{id}/api/{path}` hop is the only way to a worker's cao-server."""

    def _capture(self, client, call):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(payload=[]),
        ) as request:
            call(client)
        return request.call_args

    def test_a_worker_scoped_path_is_built_under_api(self, client):
        args = self._capture(client, lambda c: c.sessions("w1"))
        assert args.args[1] == "http://broker:9890/workers/w1/api/sessions"

    def test_a_leading_slash_does_not_double_up(self, client):
        args = self._capture(client, lambda c: c.node_get("w1", "/sessions"))
        assert args.args[1] == "http://broker:9890/workers/w1/api/sessions"

    def test_a_session_name_is_quoted_into_the_path(self, client):
        """The name comes from the worker, so it is not the caller's to trust."""
        args = self._capture(client, lambda c: c.terminals("w1", "cao/worker 1"))
        assert args.args[1] == (
            "http://broker:9890/workers/w1/api/sessions/cao%2Fworker%201/terminals"
        )

    def test_send_input_puts_the_message_in_the_query_string(self, client):
        """Pinned because it is a documented hazard, not because it is nice.

        The message becomes a query string, so it lands in the worker
        container's access log. OPN310's Appendix C tells readers never to send
        a secret this way; if this ever becomes a body, that warning is stale.
        """
        args = self._capture(client, lambda c: c.send_input("w1", "t1", "hello"))
        assert args.args[0] == "POST"
        assert args.args[1] == "http://broker:9890/workers/w1/api/terminals/t1/input"
        assert args.kwargs["params"] == {"message": "hello"}

    def test_node_post_returns_none_when_the_body_is_not_json(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(status=204, json_error=True),
        ):
            assert client.node_post("w1", "terminals/t1/input") is None

    def test_terminal_output_reads_only_the_last_response(self, client):
        args = self._capture(client, lambda c: c.terminal_output("w1", "t1"))
        assert args.kwargs["params"] == {"mode": "last"}

    def test_terminal_output_is_none_when_the_payload_is_not_an_object(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(payload=["not", "a", "dict"]),
        ):
            assert client.terminal_output("w1", "t1") is None

    def test_terminal_status_reads_the_status_field(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.request",
            return_value=_response(payload={"id": "t1", "status": "completed"}),
        ):
            assert client.terminal_status("w1", "t1") == "completed"


class TestSoleTerminal:
    def test_returns_the_workers_one_terminal(self, client):
        with patch.object(client, "sessions", return_value=[{"name": "cao-worker-w1"}]):
            with patch.object(client, "terminals", return_value=[{"id": "t1"}]):
                assert client.sole_terminal("w1") == {"id": "t1"}

    def test_no_terminal_yet_sends_the_reader_to_the_log(self, client):
        """A booting worker is the common case here, and `logs` is the answer."""
        with patch.object(client, "sessions", return_value=[{"name": "cao-worker-w1"}]):
            with patch.object(client, "terminals", return_value=[]):
                with pytest.raises(click.ClickException) as exc:
                    client.sole_terminal("w1")

        assert "cao worker logs w1" in exc.value.format_message()

    def test_no_session_at_all_is_the_same_answer(self, client):
        with patch.object(client, "sessions", return_value=[]):
            with pytest.raises(click.ClickException) as exc:
                client.sole_terminal("w1")

        assert "cao worker logs w1" in exc.value.format_message()


class TestFollowLogs:
    def test_no_read_timeout_because_a_quiet_log_is_not_a_stalled_one(self, client):
        response = _response(text="")
        response.iter_lines.return_value = iter(["a", "b"])
        with patch("cli_agent_orchestrator.utils.fleet.requests.get", return_value=response) as get:
            assert list(client.follow_logs("w1", tail_lines=5)) == ["a", "b"]

        connect, read = get.call_args.kwargs["timeout"]
        assert connect is not None
        assert read is None
        assert get.call_args.kwargs["stream"] is True
        assert get.call_args.kwargs["params"] == {"tail_lines": 5, "follow": "true"}

    def test_an_unreachable_broker_is_reported_before_streaming(self, client):
        with patch(
            "cli_agent_orchestrator.utils.fleet.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ):
            with pytest.raises(click.ClickException) as exc:
                client.follow_logs("w1")

        assert "refused" in exc.value.format_message()

    def test_an_error_status_is_raised_rather_than_streamed(self, client):
        response = _response(status=404, payload={"detail": "no such worker"})
        with patch("cli_agent_orchestrator.utils.fleet.requests.get", return_value=response):
            with pytest.raises(click.ClickException) as exc:
                client.follow_logs("w1")

        assert "no such worker" in exc.value.format_message()


class TestLiveStatesMatchTheBroker:
    """A cross-boundary guard, in the spirit of the Click/Rust catalog check.

    `LIVE_STATES` decides which rows `cao worker list` shows and which workers
    `cao fleet shutdown` tries to release. The broker has its own copy in
    `_LIVE_LEASE_STATES` and the docstring says they are kept in sync -- but
    nothing kept them. Drift is silent in the direction that matters: add a
    live state on the broker and `shutdown` quietly leaves those workers
    running, holding a node's worth of memory, with no error anywhere.
    """

    BROKER = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "cao-clusters"
        / "kubernetes"
        / "eks"
        / "broker.py"
    )

    def test_the_broker_source_is_where_this_guard_thinks_it_is(self):
        assert self.BROKER.is_file(), f"broker moved; update this guard: {self.BROKER}"

    def test_the_two_sets_are_identical(self):
        match = re.search(
            r"^_LIVE_LEASE_STATES\s*=\s*frozenset\(\{([^}]*)\}\)",
            self.BROKER.read_text(),
            re.MULTILINE,
        )
        assert match, "could not find _LIVE_LEASE_STATES in the broker"
        broker_states = set(re.findall(r'"([^"]+)"', match.group(1)))

        assert broker_states == set(LIVE_STATES)
