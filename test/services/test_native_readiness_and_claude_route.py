"""The bound response's readiness proof, and an honest Claude route.

Two reproduced failures, one theme: a surface said something it had not
established.

*The readiness sibling.* Readiness was durably published and the row
reached ``bound``, but the row projection dropped the proof, so the
consumer refused an exact binding for want of a receipt the fork was
holding. The key is now always present — an absent key and a null one
mean opposite things ("this peer cannot answer" versus "this peer
answered and there is nothing"), and a consumer waits through the second
while refusing the first outright, so omitting it would turn every
ordinary not-yet-ready moment into a permanent refusal.

*The Claude route.* The launch carried a session id and a settings hook
and no model, so a session requested as sonnet came up as a 1M Opus
route; the receipt then filled model and effort from the reservation
request, which certifies a route by comparing a claim with itself. The
model is now pinned on the launch argv and checked against the
provider's own session-start proof before admission, and the effort —
which Claude exposes no way to read before the first turn — is recorded
as requested with an explicitly null observation.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import claude_native_launch as cl
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import provider_contracts as pc

SONNET = "sonnet"
OBSERVED_OPUS_1M = "claude-opus-5[1m]"
OBSERVED_SONNET = "claude-sonnet-5"


class TestTheLaunchPinsAModelItCanCheck:
    def test_an_alias_and_a_full_name_are_both_pinnable(self):
        assert cl.validate_requested_model("sonnet") == "sonnet"
        assert cl.validate_requested_model("claude-sonnet-5") == "claude-sonnet-5"

    def test_an_unpinnable_model_is_refused_before_any_launch(self):
        """A value this side cannot check the observation against.

        Passing it through would put the route back in the state where
        nobody could say what was running — which is the whole defect.
        """
        with pytest.raises(cl.ClaudeNativeModelError):
            cl.validate_requested_model("gpt-5")

    def test_an_absent_model_is_refused_rather_than_defaulted(self):
        """There is no default this side may choose for a caller.

        A launch with no model runs on whatever the provider prefers,
        which is exactly how the requested sonnet route came up as Opus.
        """
        with pytest.raises(cl.ClaudeNativeModelError):
            cl.validate_requested_model(None)

    def test_the_model_rides_on_the_launch_argv(self):
        """There is no later moment that could apply it.

        By the time anything could send a slash command the session is
        running and the first turn has already gone somewhere.
        """
        argv = cl.build_launch_argv_with_model(
            session_id="11111111-1111-4111-8111-111111111111", model=SONNET
        )
        assert argv[:2] == ["claude", "--session-id"]
        assert "--model" in argv and argv[argv.index("--model") + 1] == SONNET


class TestTheObservedModelIsCheckedAgainstTheRequest:
    def test_the_reproduced_failure_is_refused(self):
        """Requested sonnet, provider started a 1M Opus session."""
        assert cl.observed_model_matches(SONNET, OBSERVED_OPUS_1M) is False

    def test_the_context_window_suffix_is_not_a_different_model(self):
        """``[1m]`` says how much context, not which model.

        Treating it as part of the identity would refuse a correctly
        routed session — a refusal that looks exactly like the real one.
        """
        assert cl.observed_model_matches("opus", OBSERVED_OPUS_1M) is True

    def test_an_alias_means_the_latest_in_its_family(self):
        """The provider documents an alias that way, so pinning an alias
        to one resolved id would encode a "latest" it changes silently."""
        assert cl.observed_model_matches(SONNET, OBSERVED_SONNET) is True
        assert cl.observed_model_matches(SONNET, "claude-sonnet-4-6") is True

    def test_a_full_name_is_satisfied_by_exactly_itself(self):
        assert cl.observed_model_matches("claude-sonnet-5", "claude-sonnet-5") is True
        assert cl.observed_model_matches("claude-sonnet-5", "claude-sonnet-4-6") is False

    def test_a_missing_observation_is_not_a_match(self):
        """Fail closed: nothing observed is not evidence of agreement."""
        assert cl.observed_model_matches(SONNET, None) is False
        assert cl.observed_model_matches(SONNET, "") is False

    def test_a_family_token_inside_a_longer_word_is_not_that_family(self):
        """Matched on hyphen-delimited segments, not substrings."""
        assert cl.observed_model_matches("opus", "claude-opusculum-1") is False


class TestEffortObservabilityIsDeclaredPerPair:
    def test_the_three_declarations(self):
        assert pc.effort_observability("claude_code", SONNET) == pc.EFFORT_UNOBSERVED_PRE_TURN
        assert (
            pc.effort_observability("kimi_cli", "kimi-code/kimi-for-coding")
            == pc.EFFORT_OBSERVABILITY_NONE
        )
        assert pc.effort_observability("kimi_cli", "kimi-code/k3") == pc.EFFORT_OBSERVABLE
        assert pc.effort_observability("codex", "gpt-5.6-sol") == pc.EFFORT_OBSERVABLE

    def test_an_undeclared_pair_keeps_the_strict_comparison(self):
        """Adding a provider must not silently weaken an existing check.

        The weaker classes are opt-in and each one is written down.
        """
        assert pc.effort_observability("something_new", "whatever") == pc.EFFORT_OBSERVABLE

    def test_no_observed_effort_is_accepted_for_an_unobservable_pair(self):
        """A claim nothing could have produced is refused, not welcomed."""
        matches = pc.effort_receipt_matches
        assert matches("max", None, observability=pc.EFFORT_UNOBSERVED_PRE_TURN) is True
        assert matches("max", "max", observability=pc.EFFORT_UNOBSERVED_PRE_TURN) is False
        assert matches("max", "low", observability=pc.EFFORT_UNOBSERVED_PRE_TURN) is False

    def test_the_no_surface_class_is_not_the_same_as_unobservable(self):
        """Load-bearing, and must not be elided.

        A model with no effort surface and a model whose effort cannot yet
        be *seen* are different facts. Routing the second through the
        sentinel would silently discard a real requested effort.
        """
        assert pc.EFFORT_OBSERVABILITY_NONE != pc.EFFORT_UNOBSERVED_PRE_TURN
        # The unobservable pair keeps its concrete requested effort.
        assert pc.route_selects_effort("max") is True

    def test_observable_pairs_keep_strict_equality(self):
        """Codex and K3, byte-for-byte: every existing comparison reduces
        to its current expression."""
        matches = pc.effort_receipt_matches
        assert matches("max", "max", observability=pc.EFFORT_OBSERVABLE) is True
        assert matches("max", "low", observability=pc.EFFORT_OBSERVABLE) is False
        assert matches("max", None, observability=pc.EFFORT_OBSERVABLE) is False


class _Row:
    """The reservation fields ``_validate_readiness_for_bind`` reads.

    A stand-in rather than a live row because this asserts about one
    comparison, and a real reservation would drag in a launch it does not
    need. Every field a real row supplies is supplied here.
    """

    def __init__(self, *, provider="claude_code", model=SONNET, effort="max"):
        self.reservation_id = "res-1"
        self.terminal_id = "abcd1234"
        self.generation = "gen-1"
        self.provider = provider
        self.agent_profile = "reviewer"
        self.working_directory = "/tmp/wt"
        self.execution_mode = "native_tui"
        self.execution_mode_source = "request"
        self.request_json = '{"expected_model": "%s", "expected_effort": "%s"}' % (model, effort)


def _receipt(row, *, model, effort=None):
    return {
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "working_directory": row.working_directory,
        "model": model,
        "effort": effort,
        "receipt_id": "sess-1",
        "provider_session_id": "sess-1",
        "provider_version": "2.1.220",
        "provider_receipt_kind": "claude-native-session-start",
        "model_input_ready": True,
    }


class TestBindRefusesAWrongFamilyBeforeAdmission:
    """The refusal must actually survive to the caller.

    An earlier revision assigned the model mismatch into the mismatch
    dictionary *before* that dictionary was built by a later
    comprehension. On every correctly-routed launch nothing noticed,
    because the branch was not taken — the one path it broke was the
    refusal itself, which is the only path that matters here. Ordering is
    therefore pinned by a test rather than by reading.
    """

    def test_a_wrong_family_is_refused_and_names_both_values(self):
        row = _Row(model=SONNET)
        with pytest.raises(Exception) as raised:
            v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_OPUS_1M))
        message = str(raised.value)
        assert "model" in message
        assert SONNET in message
        assert "opus" in message

    def test_the_requested_family_binds(self):
        row = _Row(model=SONNET)
        v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_SONNET))

    def test_an_unobservable_effort_claim_is_refused_at_bind(self):
        """A receipt naming an effort Claude cannot expose pre-turn."""
        row = _Row(model=SONNET, effort="max")
        with pytest.raises(Exception) as raised:
            v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_SONNET, effort="max"))
        assert "effort" in str(raised.value)


class TestTheReadinessSiblingHasThreeStates:
    """Always present; null only for not-yet; two object forms otherwise.

    A shape test in the style of the capability-shape suite, so the
    discipline is pinned rather than described. The states are not
    interchangeable: a consumer waits through null and refuses an absent
    key outright, so collapsing "durable readiness with no provider proof"
    into null would make it poll a condition that can never clear.
    """

    def _row(self, provider, mode="native_tui"):
        row = _Row(provider=provider)
        row.execution_mode = mode
        return row

    def test_null_when_nothing_is_durably_published(self, monkeypatch):
        monkeypatch.setattr(v2, "_native_readiness_sibling", v2._native_readiness_sibling)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: None,
        )
        assert v2._native_readiness_sibling(self._row("kimi_cli")) is None
        assert v2._native_readiness_sibling(self._row("claude_code")) is None

    def test_the_no_proof_form_for_a_provider_that_authors_none(self, monkeypatch):
        observation = {"pane_id": "%3", "provider_status": "idle", "observed_at": "2026-07-25Z"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {
                "state": "ready",
                "readiness": {
                    "model_input_ready": True,
                    "model_input_ready_observation": observation,
                },
            },
        )

        sibling = v2._native_readiness_sibling(self._row("kimi_cli"))

        assert sibling is not None
        assert sibling["schema"] is None
        assert sibling["proof_absent_reason"] == "provider-authors-no-readiness-proof"
        assert sibling["provider_receipt_kind"] == "kimi-native-tui-attached"
        assert sibling["input_ready"] is True
        assert sibling["input_ready_observation"] == observation
        # Exactly these keys and no others.
        assert set(sibling) == {
            "schema",
            "proof_absent_reason",
            "provider_receipt_kind",
            "provider",
            "terminal_id",
            "generation",
            "execution_mode",
            "input_ready",
            "input_ready_observation",
        }

    def test_the_no_proof_form_carries_no_provider_authored_key(self, monkeypatch):
        """``session_start_hook_id`` is absent, not null and not empty.

        A key whose only possible source would be invention must not exist
        in the object at all: a reader that finds it, even empty, has to
        decide whether it was attempted and failed.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {
                "state": "ready",
                "readiness": {"model_input_ready": True, "model_input_ready_observation": {}},
            },
        )

        sibling = v2._native_readiness_sibling(self._row("kimi_cli"))

        assert "session_start_hook_id" not in sibling
        assert "composer_state" not in sibling
        assert "provider_process_id" not in sibling

    def test_the_proof_bearing_form_requires_every_provider_authored_field(self, monkeypatch):
        """An incomplete proof is an absent one, not a weaker one.

        Publishing it with holes would satisfy the "is it there?" half of
        a consumer's check while failing the half that matters, and the
        refusal would name a field instead of the real state.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {
                "state": "ready",
                "readiness": {
                    "model_input_ready": True,
                    "provider_session_id": "sess-1",
                    "provider_session_start": {"session_id": "sess-1"},
                    "model_input_ready_observation": {"pane_id": "%3"},
                    "process_identity": {"pid": 42, "start_marker": "m"},
                },
            },
        )

        assert v2._native_readiness_sibling(self._row("claude_code")) is None

    def test_the_proof_bearing_form_when_every_field_is_present(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {
                "state": "ready",
                "readiness": {
                    "model_input_ready": True,
                    "provider_session_id": "sess-1",
                    "provider_session_start": {"session_id": "sess-1"},
                    "model_input_ready_observation": {
                        "pane_id": "%3",
                        "provider_status": "idle",
                        "observed_at": "2026-07-25Z",
                    },
                    "process_identity": {"pid": 42, "start_marker": "mark"},
                },
            },
        )

        sibling = v2._native_readiness_sibling(self._row("claude_code"))

        assert sibling["schema"] == "cao-claude-native-readiness-v1"
        assert sibling["session_start_hook_id"] == "sess-1"
        assert sibling["input_ready"] is True
        # A bare pid is forgeable — pids are recycled — so the published
        # process identity carries the start marker with it.
        assert sibling["provider_process_id"] == "42@mark"
        assert "proof_absent_reason" not in sibling
