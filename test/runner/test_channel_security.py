"""The runner channel's transport and credentials (#745).

The hub port stays inside the cluster, runners refuse cleartext to anything
but loopback unless the operator opts in, the runner token is never an API
credential (nor the reverse), and no runner secret reaches a terminal's
environment.
"""

import jwt
import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.runner import agent as agent_mod
from cli_agent_orchestrator.runner.agent import hub_url_refusal
from cli_agent_orchestrator.security import auth


class TestHubUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "wss://cao-server:9891",
            "wss://10.0.0.5:9891",
            "ws://127.0.0.1:9891",
            "ws://127.9.9.9:9891",
            "ws://localhost:9891",
            "ws://[::1]:9891",
        ],
    )
    def test_tls_or_loopback_may_be_dialled(self, url):
        assert hub_url_refusal(url, allow_insecure=False) is None

    @pytest.mark.parametrize(
        "url",
        [
            "ws://cao-server:9891",
            "ws://cao-server.cao-remote.svc.cluster.local:9891",  # looks internal: not trusted
            "ws://10.0.0.5:9891",
            "ws://example.com:9891",
        ],
    )
    def test_cleartext_elsewhere_is_refused_without_the_opt_in(self, url):
        refusal = hub_url_refusal(url, allow_insecure=False)
        assert refusal is not None and "CAO_RUNNER_ALLOW_INSECURE" in refusal
        assert hub_url_refusal(url, allow_insecure=True) is None

    def test_another_scheme_is_refused_even_with_the_opt_in(self):
        assert hub_url_refusal("http://cao-server:9891", allow_insecure=True) is not None

    def test_the_cli_refuses_cleartext_before_connecting(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "runner_token", lambda: "t" * 32)
        monkeypatch.delenv("CAO_RUNNER_ALLOW_INSECURE", raising=False)
        started = []
        monkeypatch.setattr(agent_mod.Runner, "run_forever", lambda self: started.append(1))
        with pytest.raises(SystemExit) as exc:
            agent_mod.main(["--server-url", "ws://cao-server:9891", "--ready-file", ""])
        assert exc.value.code == 2 and started == []

    def test_the_cli_honours_the_opt_in(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "runner_token", lambda: "t" * 32)
        monkeypatch.setenv("CAO_RUNNER_ALLOW_INSECURE", "1")

        async def run_forever(self):
            return None

        monkeypatch.setattr(agent_mod.Runner, "run_forever", run_forever)
        assert not agent_mod.main(["--server-url", "ws://cao-server:9891", "--ready-file", ""])


class TestCredentials:
    def test_the_runner_token_is_not_an_api_credential(self, monkeypatch):
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "api-" + "a" * 32)
        monkeypatch.setenv("CAO_RUNNER_TOKEN", "runner-" + "r" * 32)
        with pytest.raises(jwt.InvalidTokenError):
            auth._scopes_for_local_token("runner-" + "r" * 32)
        assert auth._scopes_for_local_token("api-" + "a" * 32)

    def test_the_api_token_does_not_open_the_hub(self):
        from cli_agent_orchestrator.runner.hub import RunnerHub

        hub = RunnerHub(token="runner-" + "r" * 32, host="127.0.0.1", port=0)

        class Request:
            def __init__(self, token):
                self.headers = {"Authorization": f"Bearer {token}"}

        class Connection:
            def respond(self, status, body):
                return int(status)

        assert hub._authenticate(Connection(), Request("api-" + "a" * 32)) == 401
        assert hub._authenticate(Connection(), Request("runner-" + "r" * 32)) is None


class TestTerminalEnvironment:
    SECRETS = ("CAO_RUNNER_TOKEN", "CAO_RUNNER_TOKEN_FILE", "CAO_RUNNER_TLS_KEY")

    def test_runner_secrets_are_never_forwarded_into_a_pane(self, monkeypatch):
        for name in self.SECRETS:
            monkeypatch.setenv(name, "secret")
        monkeypatch.setenv("CAO_RUNNER_SERVER_URL", "wss://cao-server:9891")
        forwarded = TmuxClient._forwarded_server_env()
        assert not set(self.SECRETS) & set(forwarded)
        assert forwarded["CAO_RUNNER_SERVER_URL"] == "wss://cao-server:9891"  # other CAO_ keys pass

    def test_runner_secrets_are_dropped_from_operator_env(self):
        environment = {}
        TmuxClient._merge_extra_env(environment, {name: "secret" for name in self.SECRETS})
        assert environment == {}
