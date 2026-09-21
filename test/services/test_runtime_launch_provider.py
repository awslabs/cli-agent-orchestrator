"""Which CLI an agent runs on is the runtime's answer, not the caller's (#745).

The profile store that says "this agent runs on opencode_cli" is installed in
the runtime: its image, its ``cao install``, its CLIs on PATH. Every sender of a
runtime LAUNCH used to resolve the provider locally first and put the answer in
the payload, so the runtime's own ``provider:`` field was overridden by a client
laptop's default — and in the assign path by the empty string, which is not a
provider at all (review finding 8 on #802).

An absent provider now means "you decide"; a present one is an explicit override
the caller asked for. Tested at both ends: the senders omit what they were only
guessing at, and the bridge resolves from its own store when nothing is sent.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.runtime_channel.api import CreateRemoteTerminalBody
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandOutcome,
    CommandType,
)


@pytest.fixture()
def remote_env(monkeypatch):
    """`cao launch --runtime` addresses a shared server."""
    monkeypatch.setenv("CAO_API_BASE_URL", "http://cao-server.example:9889")


def _launch(payload):
    return CommandFrame(op_id="op-1", type=CommandType.LAUNCH, terminal_id=None, payload=payload)


def _terminal(**kwargs):
    return SimpleNamespace(
        id="abcd1234",
        name=f"{kwargs['agent_profile']}-abcd",
        provider=kwargs["provider"],
        session_name="cao-abcd1234",
        agent_profile=kwargs["agent_profile"],
        allowed_tools=None,
        shell_command=None,
        status="initializing",
    )


@pytest.fixture
def launched():
    """Run bridge LAUNCH against a recording ``create_terminal`` seam."""
    seen = {}

    async def fake_create_terminal(**kwargs):
        seen.update(kwargs)
        return _terminal(**kwargs)

    def run(payload, profile_provider="opencode_cli"):
        bridge = Bridge("ws://unused", "worker-x", "tok")
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.create_terminal",
                side_effect=fake_create_terminal,
            ),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
                return_value=profile_provider,
            ) as resolver,
        ):
            outcome, result, terminal_id = asyncio.run(bridge._execute(_launch(payload)))
        return SimpleNamespace(
            outcome=outcome,
            result=result,
            terminal_id=terminal_id,
            seen=seen,
            resolver=resolver,
        )

    return run


class TestTheRuntimeAnswersForItself:
    def test_no_provider_sent_means_the_runtimes_own_profile_decides(self, launched):
        """The pane that this really protects: an agent whose profile in the
        worker image says opencode_cli, launched from a client whose own default
        is something else. Before, the client's default won silently."""
        run = launched({"agent_profile": "researcher"})

        assert run.outcome == CommandOutcome.OK
        assert run.seen["provider"] == "opencode_cli"
        assert run.result["terminal"]["provider"] == "opencode_cli"

    def test_the_lookup_uses_the_profile_the_launch_named(self, launched):
        run = launched({"agent_profile": "researcher"})

        assert run.resolver.call_args.args[0] == "researcher"

    def test_an_explicit_provider_still_wins(self, launched):
        """``cao launch --provider X --runtime r`` is a deliberate override; the
        runtime must not quietly substitute its profile's choice."""
        run = launched({"agent_profile": "researcher", "provider": "claude_code"})

        assert run.seen["provider"] == "claude_code"
        run.resolver.assert_not_called()

    def test_an_empty_provider_is_not_a_provider(self, launched):
        """What the assign path used to send. Taken literally it reaches
        ProviderType as "" and the launch fails in the worker pod."""
        run = launched({"agent_profile": "researcher", "provider": ""})

        assert run.seen["provider"] == "opencode_cli"

    def test_the_central_row_records_what_the_runtime_actually_started(self, launched):
        """The reply is what ``launch_remote_terminal`` writes to the DB, so the
        server learns the runtime's choice rather than keeping its own guess."""
        run = launched({"agent_profile": "researcher"})

        assert run.result["terminal"]["provider"] == "opencode_cli"
        assert run.terminal_id == "abcd1234"


class TestTheWireAllowsTheQuestionToBeLeftOpen:
    def test_the_body_accepts_a_launch_that_names_no_provider(self):
        body = CreateRemoteTerminalBody(agent_profile="researcher")
        assert body.provider is None

    def test_an_unset_provider_is_not_put_on_the_wire_at_all(self):
        """``exclude_none`` is what turns "unset" into an absent key rather than
        a null the bridge would have to special-case."""
        body = CreateRemoteTerminalBody(agent_profile="researcher")
        assert "provider" not in body.model_dump(exclude_none=True)

    def test_an_explicit_provider_is(self):
        body = CreateRemoteTerminalBody(agent_profile="researcher", provider="claude_code")
        assert body.model_dump(exclude_none=True)["provider"] == "claude_code"


class TestTheSendersDoNotGuess:
    """Each sender resolved the provider against a profile store that is not the
    one the agent will run from. They now forward only what the caller said."""

    def _assign_body(self, provider):
        from cli_agent_orchestrator.utils import orchestration

        posted = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            posted["url"] = url
            posted["body"] = json
            return MagicMock(
                status_code=201,
                json=lambda: {"id": "abcd1234", "session_name": "cao-abcd1234"},
            )

        with patch.object(orchestration.requests, "post", side_effect=fake_post):
            orchestration._assign_bridge(
                agent_profile="researcher",
                worker_message="go",
                current_terminal_id="15c1e8e0",
                runtime_id="worker-1",
                provider=provider,
                working_directory=None,
                engine=None,
                model=None,
                use_worktree=False,
            )
        return posted["body"]

    def test_assign_omits_a_provider_it_was_not_given(self):
        assert "provider" not in self._assign_body(None)

    def test_assign_forwards_one_it_was_given(self):
        assert self._assign_body("claude_code")["provider"] == "claude_code"

    def _launch_body(self, argv):
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "researcher-def6",
            "session_name": "cao-abc",
        }
        with patch.object(launch_mod.requests, "post", return_value=created) as post:
            result = CliRunner().invoke(
                launch_mod.launch,
                ["--agents", "researcher", "--headless", "--runtime", "worker-1", "--yolo", *argv],
            )
        assert result.exit_code == 0, result.output
        return post.call_args.kwargs["json"]

    def test_cao_launch_leaves_the_provider_to_the_runtime_by_default(self, remote_env):
        assert self._launch_body([]) == {"agent_profile": "researcher"}

    def test_cao_launch_forwards_an_explicit_provider_flag(self, remote_env):
        body = self._launch_body(["--provider", "mock_cli"])
        assert body == {"provider": "mock_cli", "agent_profile": "researcher"}


class TestTheSessionEndpointForwardsTheCallersProvider:
    """``POST /sessions/{name}/terminals`` resolves a provider for its local
    branch, from this container's profile store. On the remote branch — an agent
    executing in a runtime asking for a worker beside it — that value describes
    the server container, not the pod the worker will run in."""

    def _body_for(self, provider):
        from cli_agent_orchestrator.api import main

        captured = {}

        async def fake_launch(runtime_id, body, owner_id):
            captured["runtime_id"] = runtime_id
            captured["body"] = body
            return MagicMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.runtime_registry.runtime_for_terminal",
                return_value="worker-1",
            ),
            patch(
                "cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal",
                side_effect=fake_launch,
            ),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
                return_value="server_side_default",
            ),
        ):
            asyncio.run(
                main.create_terminal_in_session(
                    request=MagicMock(),
                    session_name="cao-abc",
                    agent_profile="researcher",
                    provider=provider,
                    caller_id="15c1e8e0",
                    principal=SimpleNamespace(id="user:someone"),
                )
            )
        return captured["body"]

    def test_an_unset_provider_is_not_filled_in_from_this_containers_profiles(self):
        assert self._body_for(None).provider is None

    def test_an_explicit_provider_travels_unchanged(self):
        assert self._body_for("claude_code").provider == "claude_code"
