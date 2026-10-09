"""``utils.api_http``: the CLI's HTTP calls carry the local bearer to this node only."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.utils import api_http
from cli_agent_orchestrator.utils.orchestration import _is_local_api

LOCAL = f"{API_BASE_URL}/sessions"
REMOTE = "http://other-node:9889/sessions"


@pytest.fixture
def auth_on(monkeypatch):
    """A token is configured in the CLI's environment."""
    monkeypatch.setattr(api_http, "get_local_bearer", lambda: "tok-123")


@pytest.fixture
def auth_on_no_token(monkeypatch):
    """Nothing to send: auth is off in this process, or on with no token."""
    monkeypatch.setattr(api_http, "get_local_bearer", lambda: None)


auth_off = auth_on_no_token


class TestIsLocalApi:
    @pytest.mark.parametrize(
        "base",
        [
            API_BASE_URL,
            f"{API_BASE_URL}/",
            "http://other-node:9889",
            "http://localhost:9889",
            f"{API_BASE_URL}.evil.example",
            f"{API_BASE_URL}1",
        ],
    )
    def test_agrees_with_the_orchestration_predicate(self, base):
        """One spelling of "this node" for every egress site: for a bare base URL
        the CLI helper and ``utils.orchestration._is_local_api`` must agree."""
        assert api_http.is_local_api(base) == _is_local_api(base), base

    def test_a_request_url_under_the_api_root_is_local(self):
        assert api_http.is_local_api(f"{API_BASE_URL}/sessions")
        assert api_http.is_local_api(f"{API_BASE_URL}/terminals/abc?x=1")

    def test_prefix_tricks_are_not_local(self):
        assert not api_http.is_local_api(f"{API_BASE_URL}1/sessions")
        assert not api_http.is_local_api(f"{API_BASE_URL}.evil.example/x")
        assert not api_http.is_local_api("http://localhost:9889/sessions")


class TestHeaders:
    def test_local_url_gets_the_bearer_when_auth_is_on(self, auth_on):
        assert api_http.auth_headers_for(LOCAL) == {"Authorization": "Bearer tok-123"}

    def test_remote_url_never_gets_the_bearer(self, auth_on):
        assert api_http.auth_headers_for(REMOTE) == {}

    def test_no_header_when_auth_is_off(self, auth_off):
        assert api_http.auth_headers_for(LOCAL) == {}

    def test_no_header_when_auth_is_on_but_no_token(self, auth_on_no_token):
        assert api_http.auth_headers_for(LOCAL) == {}


class TestVerbs:
    @pytest.mark.parametrize("verb", ["get", "post", "delete"])
    def test_verb_attaches_the_header_and_passes_kwargs_through(self, auth_on, verb):
        with patch.object(requests, verb) as real:
            real.return_value = MagicMock(status_code=200)
            getattr(api_http, verb)(LOCAL, params={"a": 1}, timeout=5.0)
        real.assert_called_once_with(
            LOCAL, headers={"Authorization": "Bearer tok-123"}, params={"a": 1}, timeout=5.0
        )

    def test_caller_headers_are_kept_and_the_caller_wins_on_authorization(self, auth_on):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=200)
            api_http.get(LOCAL, headers={"Accept": "text/plain", "Authorization": "Bearer mine"})
        real.assert_called_once_with(
            LOCAL, headers={"Accept": "text/plain", "Authorization": "Bearer mine"}
        )

    def test_auth_off_sends_exactly_what_it_always_did(self, auth_off):
        with patch.object(requests, "post") as real:
            real.return_value = MagicMock(status_code=200)
            api_http.post(LOCAL, params={"m": "hi"}, timeout=2.0)
        real.assert_called_once_with(LOCAL, headers=None, params={"m": "hi"}, timeout=2.0)

    def test_remote_url_is_sent_without_the_bearer(self, auth_on):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=200)
            api_http.get(REMOTE)
        real.assert_called_once_with(REMOTE, headers=None)


class TestUnauthorized:
    """The CLI cannot see the server's auth mode in its own environment, so the
    signal is the response: a 401 from THIS node on a request that carried no
    bearer is the "you have no token configured" case, and gets the message.
    Everything else is the caller's."""

    def test_401_with_no_bearer_sent_becomes_an_actionable_error(self, auth_on_no_token):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=401)
            with pytest.raises(api_http.AuthNotConfiguredError) as exc:
                api_http.get(LOCAL)
        assert "CAO_AUTH_LOCAL_TOKEN" in str(exc.value)
        assert "401" in str(exc.value)
        assert isinstance(exc.value, requests.exceptions.RequestException)

    def test_401_with_a_token_sent_is_returned_to_the_caller(self, auth_on):
        """A rejected token is the caller's business (wrong token, wrong scopes,
        expired); the helper only speaks up when there was nothing to send."""
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=401)
            assert api_http.get(LOCAL).status_code == 401

    def test_401_with_a_caller_supplied_bearer_is_returned_to_the_caller(self, auth_on_no_token):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=401)
            resp = api_http.get(LOCAL, headers={"Authorization": "Bearer mine"})
        assert resp.status_code == 401

    def test_401_from_a_remote_url_is_returned_to_the_caller(self, auth_on_no_token):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=401)
            assert api_http.get(REMOTE).status_code == 401

    def test_other_statuses_pass_through_untouched(self, auth_on_no_token):
        for status in (200, 403, 404, 500):
            with patch.object(requests, "get") as real:
                real.return_value = MagicMock(status_code=status)
                assert api_http.get(LOCAL).status_code == status


class TestSurface:
    def test_reexports_what_the_commands_use(self):
        assert api_http.exceptions is requests.exceptions
        assert api_http.Response is requests.Response
        assert issubclass(api_http.AuthNotConfiguredError, requests.exceptions.RequestException)


class TestCallerHeaderCaseInsensitivity:
    """HTTP header names are case-insensitive and Requests folds them. A caller's
    ``authorization`` in ANY spelling must win over the node credential, and must
    count as "a bearer was sent" for the 401 rule."""

    @pytest.mark.parametrize("spelling", ["Authorization", "authorization", "AUTHORIZATION"])
    def test_caller_credential_wins_in_every_spelling(self, auth_on, spelling):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=200)
            api_http.get(LOCAL, headers={spelling: "Bearer mine"})
        sent = real.call_args.kwargs["headers"]
        assert list(sent.keys()) == [spelling]
        assert sent[spelling] == "Bearer mine"

    @pytest.mark.parametrize("spelling", ["authorization", "AUTHORIZATION"])
    def test_a_caller_bearer_in_any_spelling_suppresses_the_401_message(
        self, auth_on_no_token, spelling
    ):
        with patch.object(requests, "get") as real:
            real.return_value = MagicMock(status_code=401)
            assert api_http.get(LOCAL, headers={spelling: "Bearer mine"}).status_code == 401
