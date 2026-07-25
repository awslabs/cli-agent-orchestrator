"""Routes that select no effort, and the surfaces that must not invent one.

Reproduced against the installed Kimi 0.29.1: ``kimi-code/kimi-for-coding``
(the K2.7 route) advertises no ``support_efforts``, and both ``max`` and
``high`` come back ``Invalid params`` — from the zero-prompt ACP probe and
from a real managed launch alike. The conductor's policy routing pinned
that model while inheriting ``max`` from the base route, so every K2.7
launch and the breaker attestation were blocked at the provider.

The contract agreed with the conductor implementer is an *explicit*
sentinel, ``expected_effort == "provider-default"``, rather than a null or
an omitted field: the breaker's failure domain hashes effort as a string,
so a null would both weaken a deterministic domain key and read as
"unspecified" — a different claim from "this model has no effort to
specify". The sentinel is echoed back byte-identically so existing
``expected_effort`` identity comparisons keep matching.

What these tests pin is that the omission is real at *every* point an
effort is materialized, not just the one the acceptance gate happened to
exercise first. A gate inside a single probe would leave the others as
traps: the first launch down an ungated path silently reinstates the
override, and it surfaces as a provider protocol error nowhere near its
cause.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import kimi_native_bootstrap, kimi_route
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts

SENTINEL = "provider-default"
K27 = "kimi-code/kimi-for-coding"
K3 = "kimi-code/k3"


class TestTheSentinelIsTheAgreedWireValue:
    def test_the_spelling_is_exactly_what_the_conductor_emits(self):
        """Byte-identical or the two halves do not meet.

        Written as a literal rather than imported from the constant, so
        this asserts the agreement rather than agreeing with whatever the
        code currently says.
        """
        assert provider_contracts.EFFORT_PROVIDER_DEFAULT == SENTINEL

    def test_the_sentinel_selects_no_effort_and_a_real_effort_does(self):
        assert provider_contracts.route_selects_effort(SENTINEL) is False
        assert provider_contracts.route_selects_effort("max") is True

    def test_an_empty_or_missing_effort_is_not_the_sentinel(self):
        """Absent is not the same claim as explicitly-none.

        The sentinel says "this route has no effort to give". An empty
        string says only that nobody filled the field in, and the surfaces
        that require a pinned effort must keep rejecting it.
        """
        assert provider_contracts.route_selects_effort("") is False
        assert provider_contracts.route_selects_effort(None) is False
        assert provider_contracts.EFFORT_PROVIDER_DEFAULT not in ("", None)

    def test_the_effort_env_is_omitted_not_defaulted(self):
        """Omitted, never translated into some other value.

        Substituting a default here would be the same bug wearing a
        friendlier value: the provider would run at an effort this side
        chose, while the receipt said no effort was selected.
        """
        assert provider_contracts.kimi_effort_env(SENTINEL) == {}
        assert provider_contracts.kimi_effort_env("max") == {"KIMI_MODEL_THINKING_EFFORT": "max"}


class TestTheEffortlessModelRefusesAConcreteEffort:
    """Fail closed here rather than at the provider.

    ``Invalid params`` tells a caller only that *some* parameter was wrong,
    after a session already exists. The refusal names the model, the
    rejected effort, and the sentinel to use instead.
    """

    def test_a_concrete_effort_for_k27_is_refused_with_the_sentinel_named(self):
        with pytest.raises(provider_contracts.ProviderContractError) as raised:
            provider_contracts.validate_route_effort(K27, "max")
        message = str(raised.value)
        assert K27 in message
        assert "max" in message
        assert SENTINEL in message

    def test_the_sentinel_is_accepted_for_that_model(self):
        provider_contracts.validate_route_effort(K27, SENTINEL)

    def test_k3_keeps_its_effort(self):
        """The whole point of pinning by model rather than by provider."""
        provider_contracts.validate_route_effort(K3, "max")


class TestNoEffortReachesTheProviderChild:
    """``_provider_route_environment`` is the single materialization point.

    Both the ACP bridge child and the native TUI child take their effort
    environment from here, which is why the gate lives here and not inside
    either one.
    """

    def _request(self, effort, provider="kimi_cli"):
        return {"provider": provider, "model": K27, "effort": effort}

    def test_the_sentinel_contributes_no_environment_variable(self):
        assert bridge._provider_route_environment(self._request(SENTINEL)) == {}

    def test_the_native_child_environment_carries_no_effort_override(self):
        """The path the acceptance gate exercises with --execution-mode native_tui.

        Asserted through the composed child environment rather than the
        helper alone: that composition is what the provider process
        actually receives, and an override reintroduced anywhere in it
        would be invisible to a test of the helper.
        """
        composed = bridge._provider_child_environment(self._request(SENTINEL))
        assert "KIMI_MODEL_THINKING_EFFORT" not in composed

    def test_a_real_effort_still_reaches_the_child(self):
        composed = bridge._provider_child_environment(self._request("max"))
        assert composed["KIMI_MODEL_THINKING_EFFORT"] == "max"

    def test_an_absent_effort_is_still_refused(self):
        """The sentinel relaxes one specific claim, not the requirement.

        A managed launch with no effort field at all is still a launch
        nobody pinned a route for.
        """
        with pytest.raises(bridge.BridgeError):
            bridge._provider_route_environment(self._request(""))


class TestTheAttestationClaimsNoEffortItDidNotObserve:
    """The receipt is the artifact a breaker reads, so it must say so."""

    def _probe(self, monkeypatch, effort, *, model=K27, thinking="high"):
        """Drive the real probe against a scripted ACP peer.

        Records every request the probe made, so "it did not ask" is
        asserted directly rather than inferred from the result.
        """
        sent: list[tuple] = []
        seen_env: dict = {}

        class _Client:
            def __init__(self, argv, env, timeout):
                seen_env.update(env)

            def request(self, method, params):
                sent.append((method, params))
                options = [
                    {"id": "model", "category": "model", "currentValue": model},
                    {"id": "thinking", "category": "thought_level", "currentValue": thinking},
                ]
                if method == "initialize":
                    return {"protocolVersion": 1, "agentInfo": {"version": "0.29.1"}}
                return {"sessionId": "sess-1", "configOptions": options}

            def close(self):
                return 0, ""

        monkeypatch.setattr(kimi_route, "_AcpClient", _Client)
        monkeypatch.setattr(
            kimi_route.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "0.29.1"})(),
        )
        return sent, seen_env

    def test_no_thinking_option_is_ever_set_for_a_no_effort_route(
        self, monkeypatch, tmp_path, request
    ):
        sent, seen_env = self._probe(monkeypatch, SENTINEL)
        config = tmp_path / "config.toml"
        config.write_text("x = 1\n")

        receipt = kimi_route.attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model=K27,
            expected_effort=SENTINEL,
            user_config_path=config,
        )

        thinking_sets = [
            params
            for method, params in sent
            if method == "session/set_config_option" and params.get("configId") == "thinking"
        ]
        assert thinking_sets == []
        assert "KIMI_MODEL_THINKING_EFFORT" not in seen_env
        assert receipt["reasoning_effort"] is None
        assert receipt["effort_observed"] is False
        assert receipt["effort_mode"] == SENTINEL
        assert receipt["terminal_effort_env"] == {}
        assert K27 in receipt["effort_unsupported_reason"]

    def test_the_session_thought_level_is_not_passed_off_as_a_resolution(
        self, monkeypatch, tmp_path
    ):
        """The trap this closes.

        The scripted session reports ``high``. Reading that back would
        produce a receipt asserting an effort the probe never selected and
        the model does not support — the most convincing possible way to
        ship the bug.
        """
        self._probe(monkeypatch, SENTINEL, thinking="high")
        config = tmp_path / "config.toml"
        config.write_text("x = 1\n")

        receipt = kimi_route.attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model=K27,
            expected_effort=SENTINEL,
            user_config_path=config,
        )

        assert receipt["reasoning_effort"] != "high"
        assert receipt["reasoning_effort"] is None

    def test_a_k3_route_still_selects_and_verifies_its_effort(self, monkeypatch, tmp_path):
        """K3 behavior byte-identical: still set, still checked, still reported."""
        sent, seen_env = self._probe(monkeypatch, "max", model=K3, thinking="max")
        config = tmp_path / "config.toml"
        config.write_text("x = 1\n")

        receipt = kimi_route.attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model=K3,
            expected_effort="max",
            user_config_path=config,
        )

        assert seen_env["KIMI_MODEL_THINKING_EFFORT"] == "max"
        assert receipt["reasoning_effort"] == "max"
        assert receipt["effort_observed"] is True
        assert receipt["terminal_effort_env"] == {"KIMI_MODEL_THINKING_EFFORT": "max"}
        assert "effort_unsupported_reason" not in receipt

    def test_an_inherited_effort_env_does_not_leak_into_a_no_effort_probe(
        self, monkeypatch, tmp_path
    ):
        """A stale variable in the parent is not this route's request.

        Without the explicit pop, the probe inherits whatever the operator
        happened to export and the model rejects it — with nothing in the
        receipt to explain where it came from.
        """
        monkeypatch.setenv("KIMI_MODEL_THINKING_EFFORT", "max")
        _sent, seen_env = self._probe(monkeypatch, SENTINEL)
        config = tmp_path / "config.toml"
        config.write_text("x = 1\n")

        kimi_route.attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model=K27,
            expected_effort=SENTINEL,
            user_config_path=config,
        )

        assert "KIMI_MODEL_THINKING_EFFORT" not in seen_env

    def test_a_concrete_effort_for_k27_never_starts_the_binary(self, monkeypatch, tmp_path):
        """Refused before the probe, so no session exists to finalize."""
        started = []
        monkeypatch.setattr(
            kimi_route.subprocess,
            "run",
            lambda *a, **k: started.append(a) or type("R", (), {"returncode": 0, "stdout": ""})(),
        )

        with pytest.raises(kimi_route.KimiRouteProbeError, match=SENTINEL):
            kimi_route.attest_kimi_route(
                str(tmp_path.resolve()), expected_model=K27, expected_effort="max"
            )
        assert started == []


class TestTheNativeBootstrapSetsNoEffort:
    """The third leak point, exercised by ``--execution-mode native_tui``."""

    def _options(self, thinking="high", model=K27):
        return [
            {"id": "model", "category": "model", "currentValue": model},
            {"id": "thinking", "category": "thought_level", "currentValue": thinking},
        ]

    class _Transport:
        def __init__(self, options):
            self.sent: list[tuple] = []
            self._options = options

        def request(self, method, params):
            self.sent.append((method, params))
            return {"configOptions": self._options}

    def test_no_thinking_option_is_set_and_none_is_verified(self):
        transport = self._Transport(self._options())

        kimi_native_bootstrap._apply_route(
            transport,
            session_id="sess-1",
            options=self._options(),
            model=K27,
            effort=SENTINEL,
        )

        assert [p.get("configId") for _m, p in transport.sent] == []

    def test_the_model_half_is_still_exact(
        self,
    ):
        """Declining to pin the effort is not declining to pin the model."""
        transport = self._Transport(self._options(model="kimi-code/other"))

        with pytest.raises(kimi_native_bootstrap.KimiBootstrapProtocol):
            kimi_native_bootstrap._apply_route(
                transport,
                session_id="sess-1",
                options=self._options(model="kimi-code/other"),
                model=K27,
                effort=SENTINEL,
            )

    def test_a_k3_route_still_sets_and_verifies_thinking(self):
        transport = self._Transport(self._options(thinking="max", model=K3))

        kimi_native_bootstrap._apply_route(
            transport,
            session_id="sess-1",
            options=self._options(thinking="low", model=K3),
            model=K3,
            effort="max",
        )

        assert ("thinking", "max") in [
            (p.get("configId"), p.get("value")) for _m, p in transport.sent
        ]
