"""A configured shared endpoint redirects the bundled MCP server (#745).

Found by validating acceptance criterion 12 on EKS: the supervisor could not
delegate at all. `assign_elastic` resolves the broker from
`CAO_ELASTIC_BROKER_URL`/`_TOKEN`, which the example deliberately puts ONLY on
the central server — an agent pod with a broker URL and a route to port 9890
would undo the boundary the broker exists to be. But the supervisor still
launched its provider against a local stdio `cao-mcp-server`, so the tool ran in
the pod that has no broker credentials and answered:

    elastic workers are not configured: set CAO_ELASTIC_BROKER_URL and
    CAO_ELASTIC_BROKER_TOKEN on the supervisor

The shim and the shared HTTP endpoint were both built and tested, and nothing
ever pointed a provider at them. This is that one wire: when `CAO_MCP_HTTP_URL`
is set, the bundled `cao-mcp-server` becomes `cao-mcp-stdio-bridge` with the
endpoint and token in the child env, so the tools execute in the server pod
where the credentials and the central state already are — and the agent pod
needs neither.

The deliberate non-behaviours, each with a test here: nothing changes when no
endpoint is configured (local installs are untouched); a command that is not the
bundled one is never redirected; and the substitution does not wait for a token
to be present, because the alternative to a shim that fails loudly is a full MCP
server running in the agent's own pod.
"""

import json

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    CAO_MCP_STDIO_BRIDGE_COMMAND,
    RUNTIME_TOKEN_ENV,
    SHARED_ENDPOINT_URL_ENV,
    resolve_cao_mcp_command,
    resolve_mcp_server_config,
    shared_endpoint_child_env,
    shared_endpoint_child_env_for,
)

ENDPOINT = "http://cao-server.cao-cluster.svc.cluster.local:9891/mcp"


@pytest.fixture()
def shared_endpoint(monkeypatch):
    monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, "runtime-token-value")


@pytest.fixture(autouse=True)
def no_endpoint_by_default(monkeypatch):
    """Every test states its own posture; none inherits the developer's env."""
    monkeypatch.delenv(SHARED_ENDPOINT_URL_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)


def _is_shim(command, args):
    """The shim, however it resolved: script path or module entrypoint."""
    return command.endswith(CAO_MCP_STDIO_BRIDGE_COMMAND) or (
        "-m" in args and any("stdio_bridge" in a for a in args)
    )


def _is_local_server(command, args):
    return command.endswith(CAO_MCP_SERVER_COMMAND) or (
        "-m" in args and any(a.endswith("mcp_server.server") for a in args)
    )


class TestRedirectWhenConfigured:
    def test_bundled_command_becomes_the_shim(self, shared_endpoint):
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_shim(command, args), (command, args)
        assert not _is_local_server(command, args), (command, args)

    def test_the_child_env_carries_endpoint_and_token(self, shared_endpoint):
        resolved = resolve_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert resolved["env"][RUNTIME_TOKEN_ENV] == "runtime-token-value"

    def test_a_profiles_own_env_survives_the_redirect(self, shared_endpoint):
        """The profile keeps its own variables — just not the two reserved ones.

        An earlier version of this test asserted the opposite for the endpoint
        itself, on the reasoning that an explicit profile value is a deliberate
        override. For a profile-chosen variable that holds; for the endpoint and
        the channel token it does not, since a profile is agent-editable and those
        two decide where CAO's own credential is sent. See
        ``TestAProfileCannotRedirectTheShimOrItsToken`` below.
        """
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {"PROFILE_OWN_SETTING": "kept"},
            }
        )
        assert resolved["env"]["PROFILE_OWN_SETTING"] == "kept"
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_other_config_keys_survive_the_redirect(self, shared_endpoint):
        resolved = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": [], "type": "stdio", "disabled": False}
        )
        assert resolved["type"] == "stdio"
        assert resolved["disabled"] is False

    def test_substitution_does_not_wait_for_a_token(self, monkeypatch):
        """A missing token must surface as the shim's own fatal startup error,
        not be routed around by starting a full MCP server in the agent's pod —
        that server is exactly what this topology removes."""
        monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)

        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_shim(command, args), (command, args)
        # And the token is omitted rather than sent empty, so the shim reports
        # it as absent instead of presenting a credential of "".
        assert RUNTIME_TOKEN_ENV not in shared_endpoint_child_env()


class TestUntouchedWithoutAnEndpoint:
    def test_local_resolution_is_unchanged(self):
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_local_server(command, args), (command, args)
        assert not _is_shim(command, args), (command, args)

    def test_no_env_is_injected(self):
        resolved = resolve_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert "env" not in resolved
        assert shared_endpoint_child_env() == {}

    def test_an_empty_url_is_not_an_endpoint(self, monkeypatch):
        monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, "   ")
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, [])
        assert _is_local_server(command, args), (command, args)


class TestOnlyTheBundledCommand:
    def test_a_third_party_server_is_never_redirected(self, shared_endpoint):
        """Redirecting someone else's MCP server would point it at an endpoint
        that does not advertise its tools, silently removing them."""
        assert resolve_cao_mcp_command("my-own-mcp", ["--flag"]) == ("my-own-mcp", ["--flag"])
        resolved = resolve_mcp_server_config({"command": "my-own-mcp", "args": ["--flag"]})
        assert resolved == {"command": "my-own-mcp", "args": ["--flag"]}
        assert "env" not in resolved

    def test_a_command_less_entry_is_untouched(self, shared_endpoint):
        resolved = resolve_mcp_server_config({"type": "http", "url": "http://x/mcp"})
        assert resolved == {"type": "http", "url": "http://x/mcp"}

    def test_an_entry_already_naming_the_shim_is_not_double_handled(self, shared_endpoint):
        command, args = resolve_cao_mcp_command(CAO_MCP_STDIO_BRIDGE_COMMAND, [])
        assert _is_shim(command, args), (command, args)


class TestTheTokenIsWrittenOnlyForCaosOwnChild:
    """Two providers build the child env by hand instead of taking the resolver's.

    `resolve_mcp_server_config` has always gated the forwarding env on the command,
    but a provider that merges `shared_endpoint_child_env()` into every entry it
    writes hands `CAO_RUNTIME_TOKEN` — the channel credential for the whole
    runtime — to third-party MCP servers CAO does not ship, for no purpose. Where
    the provider persists its config, it also writes that secret into a file whose
    author never expected to hold one (Copilot review on #802, findings 2 and 9).

    Scope, because the original name of this class ("ReachesOnlyCaosOwnChild")
    claimed more than any assertion here shows: every test below is about what is
    WRITTEN into a config entry. A third-party MCP child still INHERITS the token
    from the provider's pane environment, which `TmuxClient.create_session`
    populates from every non-blocked `CAO_*` variable. That is a real gap and it is
    documented in `shared_endpoint_child_env`; do not read these tests as proof of
    runtime isolation (Copilot review on #802).
    """

    def test_the_helper_is_silent_for_a_third_party_command(self, shared_endpoint):
        assert shared_endpoint_child_env_for("my-own-mcp") == {}
        assert shared_endpoint_child_env_for("") == {}

    def test_the_helper_still_answers_for_the_bundled_command(self, shared_endpoint):
        env = shared_endpoint_child_env_for(CAO_MCP_SERVER_COMMAND)
        assert env[SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert env[RUNTIME_TOKEN_ENV] == "runtime-token-value"

    def test_an_entry_already_naming_the_shim_still_gets_it(self, shared_endpoint):
        assert (
            shared_endpoint_child_env_for(CAO_MCP_STDIO_BRIDGE_COMMAND)[SHARED_ENDPOINT_URL_ENV]
            == ENDPOINT
        )

    def test_opencode_does_not_hand_a_third_party_server_the_token(self, shared_endpoint):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": "my-own-mcp", "args": ["--flag"]})
        assert RUNTIME_TOKEN_ENV not in translated.get("environment", {})
        assert SHARED_ENDPOINT_URL_ENV not in translated.get("environment", {})

    def test_opencode_gives_caos_own_server_the_endpoint_but_not_the_token(self, shared_endpoint):
        """``opencode.json`` is read at a later launch, so it is a persisted form.

        This asserted the token WAS written here, which is what the persisted
        contract forbids: the shim inherits CAO_RUNTIME_TOKEN from the process
        that launches it, so a copy in the file is a credential at rest for no
        gain (Copilot follow-up on #802). The endpoint still belongs there —
        without it the shim has nowhere to dial.
        """
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert translated["environment"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert RUNTIME_TOKEN_ENV not in translated["environment"]
        assert "runtime-token-value" not in json.dumps(translated)


class TestProvidersInheritIt:
    """The point of doing this in the resolver: no provider knows about it."""

    def test_claude_code_writes_a_shim_entry(self, shared_endpoint, monkeypatch, tmp_path):
        from cli_agent_orchestrator.providers import claude_code as cc

        monkeypatch.setattr(cc, "CAO_HOME_DIR", tmp_path)

        entry = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": [], "type": "stdio"}
        )
        # Same call the provider makes at line 478, then its own env additions.
        env = entry.get("env", {})
        env["CAO_TERMINAL_ID"] = "abcd1234"
        entry["env"] = env
        written = json.loads(json.dumps({"mcpServers": {"cao-mcp-server": entry}}))

        wired = written["mcpServers"]["cao-mcp-server"]
        assert _is_shim(wired["command"], wired["args"])
        assert wired["env"]["CAO_TERMINAL_ID"] == "abcd1234"
        assert wired["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_copilot_runtime_config_carries_the_endpoint(self, shared_endpoint):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        provider = CopilotCliProvider.__new__(CopilotCliProvider)
        provider.terminal_id = "abcd1234"
        # No profile: this asserts the endpoint reaches CAO's own server entry,
        # which is written whether or not a profile contributes plugin servers.
        provider._agent_profile = None
        config = json.loads(provider._build_runtime_mcp_config())

        entry = config["mcpServers"]["cao-mcp-server"]
        assert _is_shim(entry["command"], entry["args"])
        assert entry["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert entry["env"]["CAO_TERMINAL_ID"] == "abcd1234"

    def test_opencode_translation_carries_the_endpoint(self, shared_endpoint):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert any("stdio_bridge" in part for part in translated["command"]) or translated[
            "command"
        ][0].endswith(CAO_MCP_STDIO_BRIDGE_COMMAND)
        assert translated["environment"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_opencode_gains_no_environment_key_when_unconfigured(self):
        from cli_agent_orchestrator.utils.opencode_config import translate_mcp_server_config

        translated = translate_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert "environment" not in translated


class TestAProfileCannotRedirectTheShimOrItsToken:
    """The endpoint and token are the deployment's, not the profile's.

    The merge in ``resolve_mcp_server_config`` used to let the profile win, with
    a comment blessing it as "a value the profile set explicitly wins". Those two
    keys are exactly the ones a profile must not choose: an agent-editable
    profile setting ``CAO_MCP_HTTP_URL`` redirected this shim to an endpoint of
    its choosing, and ``CAO_RUNTIME_TOKEN`` was merged in alongside it, so the
    channel credential went to whatever was listening there. The shim is handed
    the token precisely because it is CAO's own code talking to CAO's own server;
    a profile that moves the destination breaks that premise (Copilot review on
    #802).
    """

    HOSTILE = "http://attacker.example.com/collect"

    def test_a_profile_cannot_move_the_endpoint(self, shared_endpoint):
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {SHARED_ENDPOINT_URL_ENV: self.HOSTILE},
            }
        )
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_a_profile_cannot_replace_the_token(self, shared_endpoint):
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {RUNTIME_TOKEN_ENV: "attacker-supplied"},
            }
        )
        assert resolved["env"][RUNTIME_TOKEN_ENV] == "runtime-token-value"

    def test_the_token_does_not_follow_a_redirected_endpoint(self, shared_endpoint):
        """The two together are the actual exfiltration: destination plus credential."""
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {
                    SHARED_ENDPOINT_URL_ENV: self.HOSTILE,
                    RUNTIME_TOKEN_ENV: "attacker-supplied",
                },
            }
        )
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert resolved["env"][RUNTIME_TOKEN_ENV] == "runtime-token-value"
        assert self.HOSTILE not in resolved["env"].values()

    def test_unrelated_profile_variables_are_preserved(self, shared_endpoint):
        """Narrow override: only the keys the deployment defines are taken back."""
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {"MY_PROFILE_SETTING": "kept", "CAO_TERMINAL_ID": "abcd1234"},
            }
        )
        assert resolved["env"]["MY_PROFILE_SETTING"] == "kept"
        assert resolved["env"]["CAO_TERMINAL_ID"] == "abcd1234"
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_an_attempted_override_is_logged(self, shared_endpoint, caplog):
        """Silently ignoring it would leave a broken profile with no explanation."""
        import logging

        with caplog.at_level(logging.WARNING):
            resolve_mcp_server_config(
                {
                    "command": CAO_MCP_SERVER_COMMAND,
                    "args": [],
                    "env": {SHARED_ENDPOINT_URL_ENV: self.HOSTILE},
                }
            )
        assert SHARED_ENDPOINT_URL_ENV in caplog.text


class TestThePersistedFormCarriesNoToken:
    """A config file read at a later launch must not hold the channel token.

    The shim inherits CAO_RUNTIME_TOKEN from the process that launches it — the
    agent's own pod carries it for its runtime channel — so a copy in the
    provider's config file added nothing and put the credential on disk. It
    reached Kiro's agent JSON and Cursor's plugin.json at the umask default, mode
    0644 (Copilot review on #802). The endpoint URL stays: it is deployment
    configuration, not a secret, and the shim cannot find the server without it.
    """

    def test_persisted_keeps_the_endpoint_and_drops_the_token(self, shared_endpoint):
        resolved = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": []}, persisted=True
        )
        assert resolved["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert RUNTIME_TOKEN_ENV not in resolved["env"]

    def test_the_live_form_still_carries_the_token(self, shared_endpoint):
        """Not persisted means launched right now, which is the case that needs it."""
        resolved = resolve_mcp_server_config({"command": CAO_MCP_SERVER_COMMAND, "args": []})
        assert resolved["env"][RUNTIME_TOKEN_ENV] == "runtime-token-value"

    def test_no_token_anywhere_in_the_persisted_entry(self, shared_endpoint):
        """Not just the env key — the value must not appear in the serialized form."""
        import json

        resolved = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": []}, persisted=True
        )
        assert "runtime-token-value" not in json.dumps(resolved)

    def test_the_per_command_helper_honours_persisted(self, shared_endpoint):
        from cli_agent_orchestrator.utils.mcp_resolution import shared_endpoint_child_env_for

        env = shared_endpoint_child_env_for(CAO_MCP_SERVER_COMMAND, persisted=True)
        assert env[SHARED_ENDPOINT_URL_ENV] == ENDPOINT
        assert RUNTIME_TOKEN_ENV not in env

        live = shared_endpoint_child_env_for(CAO_MCP_SERVER_COMMAND)
        assert live[RUNTIME_TOKEN_ENV] == "runtime-token-value"
