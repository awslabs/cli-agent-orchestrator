"""Provider-agnostic transport conformance (#745).

The acceptance requirement: "No provider is special-cased in the transport,
and the proven path exercises the shared TerminalBackend/run_agent_step
contract rather than one provider's quirks." Two enforcement layers:

1. A static check that no runtime_channel module names or imports any shipped
   provider — a special case cannot exist in code that never mentions one.
2. A recorded contract fixture per shipped provider: the bridge LAUNCH arm
   forwards each provider identifier opaquely into the shared
   ``terminal_service.create_terminal`` seam and reports the terminal back
   unchanged. Paid providers are covered by this fixture without live
   credentials.

What this file does NOT do: run a provider for real. The fixture patches
``create_terminal``, so it proves the identifier travels opaquely, not that an
agent executed. The live evidence for claude_code over cao-bridge is a manual
run on EKS recorded in design.md; there is no automated non-mock gate in this
suite, and claiming one here would be claiming CI coverage that does not exist.
"""

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    CommandFrame,
    CommandOutcome,
    CommandType,
)

_CHANNEL_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "cli_agent_orchestrator"
    / "runtime_channel"
)


class TestTransportIsProviderAgnostic:
    def test_no_provider_is_named_in_the_transport(self):
        """The channel/bridge/registry source never mentions a provider id or
        imports the providers package — the transport cannot special-case what
        it cannot see."""
        offenders = []
        for path in sorted(_CHANNEL_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "from cli_agent_orchestrator.providers" in source or ("providers import" in source):
                offenders.append(f"{path.name}: imports providers")
            for provider in PROVIDERS:
                if f'"{provider}"' in source or f"'{provider}'" in source:
                    offenders.append(f"{path.name}: names provider {provider!r}")
        assert not offenders, offenders


class TestBridgeLaunchContractPerProvider:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_launch_forwards_the_provider_opaquely(self, provider):
        """One recorded fixture per shipped provider: LAUNCH carries the
        provider string through the shared create_terminal seam untouched and
        returns the terminal identity — same code path for every provider."""
        bridge = Bridge("ws://unused", "worker-x", "tok")
        seen = {}

        async def fake_create_terminal(**kwargs):
            seen.update(kwargs)
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

        frame = CommandFrame(
            op_id="op-1",
            type=CommandType.LAUNCH,
            terminal_id=None,
            payload={"provider": provider, "agent_profile": "default"},
        )
        with patch(
            "cli_agent_orchestrator.services.terminal_service.create_terminal",
            side_effect=fake_create_terminal,
        ):
            outcome, payload, terminal_id = asyncio.run(bridge._execute(frame))

        assert outcome == CommandOutcome.OK
        assert seen["provider"] == provider  # forwarded opaquely, not remapped
        assert payload["terminal"]["provider"] == provider
        assert terminal_id == "abcd1234"
