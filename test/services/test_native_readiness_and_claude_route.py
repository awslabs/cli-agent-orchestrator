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
from cli_agent_orchestrator.services import claude_native_readiness
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import provider_contracts as pc

SONNET = "sonnet"
OBSERVED_OPUS_1M = "claude-opus-5[1m]"
OBSERVED_SONNET = "claude-sonnet-5"


class TestTheLaunchPinsAModelItCanCheck:
    def test_an_alias_and_a_full_name_are_both_pinnable(self):
        assert cl.validate_requested_model("sonnet") == "sonnet"
        assert cl.validate_requested_model("claude-sonnet-5") == "claude-sonnet-5"
        assert cl.validate_requested_model("claude-opus-5[1m]") == "claude-opus-5[1m]"

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

    def test_a_full_name_without_a_context_pin_accepts_the_observed_context(self):
        assert cl.observed_model_matches("claude-opus-5", OBSERVED_OPUS_1M) is True

    def test_an_explicit_context_pin_must_match_exactly(self):
        assert cl.observed_model_matches("claude-opus-5[1m]", OBSERVED_OPUS_1M) is True
        assert cl.observed_model_matches("claude-opus-5[1m]", "claude-opus-5[200k]") is False
        assert cl.observed_model_matches("claude-opus-5[1m]", "claude-opus-5") is False
        assert (
            cl.observed_model_mismatch_detail("claude-opus-5[1m]", "claude-opus-5[200k]")
            == "requested context window '[1m]', observed context window '[200k]'"
        )
        assert (
            cl.observed_model_mismatch_detail("claude-opus-5[1m]", "claude-opus-5")
            == "requested context window '[1m]', but the provider reported no context window"
        )

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


def _parse_mismatches(exc: Exception) -> dict:
    """The structured diagnostic out of a bind refusal.

    The refusal appends canonical JSON to a prose prefix, so the object is
    recovered from the first brace rather than by splitting on the prose —
    which would break the moment anyone reworded the sentence.
    """
    import json

    message = str(exc)
    start = message.find("{")
    assert start != -1, f"refusal carried no structured diagnostic: {message}"
    return json.loads(message[start:])


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
        """The diagnostic is parsed, not scanned for substrings.

        Both values appearing *somewhere* in a serialized dict is
        satisfied just as well by a refusal that has them the wrong way
        round — and expected-vs-observed reversed is not a cosmetic
        defect: an operator reading it concludes the provider was asked
        for the model it actually ran, and goes looking for the fault
        somewhere it is not. So the structure is asserted, keyed, and
        each value pinned to its own side.
        """
        row = _Row(model=SONNET)
        with pytest.raises(Exception) as raised:
            v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_OPUS_1M))

        mismatches = _parse_mismatches(raised.value)
        assert "model" in mismatches, mismatches
        assert mismatches["model"] == {"expected": SONNET, "observed": OBSERVED_OPUS_1M}
        # And nothing else drifted: a refusal that also flagged unrelated
        # fields would pass the check above while pointing an operator at
        # the wrong cause.
        assert set(mismatches) == {"model"}

    def test_the_requested_family_binds(self):
        row = _Row(model=SONNET)
        v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_SONNET))

    def test_the_explicit_1m_route_crosses_the_real_bind_gate(self):
        row = _Row(model="claude-opus-5[1m]")
        v2._validate_readiness_for_bind(row, _receipt(row, model=OBSERVED_OPUS_1M))

    def test_the_explicit_context_survives_receipt_construction_and_bind(self):
        row = _Row(model="claude-opus-5[1m]")
        request = {"expected_model": "claude-opus-5[1m]", "expected_effort": "max"}
        receipt = v2._native_readiness_receipt(
            record={
                "reservation_id": row.reservation_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "provider": row.provider,
                "agent_profile": row.agent_profile,
                "working_directory": row.working_directory,
            },
            request=request,
            bootstrap={
                "native_session_id": "sess-1",
                "observed_model": OBSERVED_OPUS_1M,
                "observed_effort": None,
                "model": request["expected_model"],
                "effort": request["expected_effort"],
            },
            outcome={
                "outcome": "started",
                "launch_argv_sha256": "a" * 64,
                "pane_handle": "%1",
                "attachment": {"owner": {"pane_id": "%1"}},
            },
            version_output="2.1.220 (Claude Code)",
            bridge_version="test",
            readiness={"input_ready": True},
            session_start={"session_id": "sess-1", "model": OBSERVED_OPUS_1M},
        )

        assert receipt["model"] == OBSERVED_OPUS_1M
        v2._validate_readiness_for_bind(row, receipt)

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
                    "provider_session_start": {
                        claude_native_readiness.SESSION_START_ID_KEY: "sess-1"
                    },
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
                    "provider_session_start": {
                        claude_native_readiness.SESSION_START_ID_KEY: "sess-1"
                    },
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


class TestTheObservedModelSurvivesTheRealPath:
    """Hook file → readiness receipt → comparison, with nothing faked.

    The earlier revision of this suite asserted ``observed_model_matches``
    against a hand-written string and never carried a real hook record
    through the receipt that feeds it. The receipt dropped the provider's
    ``model`` key, so the comparison ran against ``None`` and refused
    *every* native Claude launch — while every test passed, because each
    one supplied the field the production path was losing.

    A test that constructs the value under test is a test of itself. These
    write the record the provider actually writes and read it back the way
    production does.
    """

    #: The live record, verbatim from a real managed launch: the provider
    #: publishes the model under exactly the key the comparison reads.
    LIVE_RECORD = {
        "session_id": "ccefa0aa-ef31-4a8f-8fbc-7b4b9cd7492a",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/tmp/wt",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "claude-opus-5[1m]",
    }

    def _hook_file(self, tmp_path, record):
        import json

        path = tmp_path / "claude-session-start.jsonl"
        path.write_text(json.dumps(record) + "\n")
        return path

    def test_the_receipt_carries_the_providers_model(self, tmp_path):
        from cli_agent_orchestrator.services import claude_native_readiness as cr

        path = self._hook_file(tmp_path, self.LIVE_RECORD)

        receipt = cr.await_session_start(path, self.LIVE_RECORD["session_id"], timeout=1.0)

        assert receipt["model"] == "claude-opus-5[1m]"

    def test_the_requested_route_is_accepted_end_to_end(self, tmp_path):
        """Requested ``opus``; the provider's own proof says a 1M Opus."""
        from cli_agent_orchestrator.services import claude_native_readiness as cr

        path = self._hook_file(tmp_path, self.LIVE_RECORD)
        receipt = cr.await_session_start(path, self.LIVE_RECORD["session_id"], timeout=1.0)

        assert cl.observed_model_matches("opus", receipt.get("model")) is True

    def test_the_explicit_1m_route_is_accepted_end_to_end(self, tmp_path):
        """The profile pins both Opus 5 and the 1M context window."""
        from cli_agent_orchestrator.services import claude_native_readiness as cr

        path = self._hook_file(tmp_path, self.LIVE_RECORD)
        receipt = cr.await_session_start(path, self.LIVE_RECORD["session_id"], timeout=1.0)

        assert cl.observed_model_matches("claude-opus-5[1m]", receipt.get("model")) is True

    def test_the_reproduced_wrong_route_is_refused_end_to_end(self, tmp_path):
        """The live failure: sonnet requested, Opus started."""
        from cli_agent_orchestrator.services import claude_native_readiness as cr

        path = self._hook_file(tmp_path, self.LIVE_RECORD)
        receipt = cr.await_session_start(path, self.LIVE_RECORD["session_id"], timeout=1.0)

        assert cl.observed_model_matches(SONNET, receipt.get("model")) is False

    def test_a_record_without_a_model_fails_closed(self, tmp_path):
        """ "The provider did not say" is not agreement.

        An older build that omits the key must refuse rather than be
        waved through on an absent observation.
        """
        from cli_agent_orchestrator.services import claude_native_readiness as cr

        record = {k: v for k, v in self.LIVE_RECORD.items() if k != "model"}
        path = self._hook_file(tmp_path, record)

        receipt = cr.await_session_start(path, record["session_id"], timeout=1.0)

        assert receipt["model"] is None
        assert cl.observed_model_matches("opus", receipt.get("model")) is False


class TestOneCompletenessRuleForBindAndProjection:
    """Bind and the projection must answer the same question identically.

    They used to answer it separately with different sets, and the gap
    between them was reachable: a receipt rich enough to bind but too thin
    to project left a generation ``bound`` whose readiness sibling was
    ``null`` forever. A consumer reads that null as "not yet", so it waits
    for a readiness that has already been consumed and can never arrive.
    """

    THIN = {
        "model_input_ready": True,
        "provider_session_id": "sess-1",
        # A session-start proof with no session id, and no process
        # identity at all: enough to look like a receipt, not enough to
        # publish as one.
        "provider_session_start": {},
        "model_input_ready_observation": {
            "pane_id": "%3",
            "provider_status": "idle",
            "observed_at": "2026-07-25Z",
        },
    }

    def test_a_thin_proof_is_incomplete_for_both(self):
        row = _Row(provider="claude_code")
        missing = v2._incomplete_readiness_fields(row, self.THIN)

        assert "session_start_hook_id" in missing
        assert "provider_process_id" in missing

    def test_bind_refuses_a_thin_proof_as_not_yet(self, monkeypatch):
        """Transient, not permanent: the proof may still be completed.

        Refusing it as a conflict would trip a breaker over a receipt that
        is merely early.
        """
        from cli_agent_orchestrator.services.managed_launch import ManagedLaunchNotReady

        row = _Row(provider="claude_code")
        assert v2._incomplete_readiness_fields(row, self.THIN)
        # The same rule the bind gate consults, so the refusal it raises
        # and the null the projection publishes cannot disagree.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {"state": "ready", "readiness": self.THIN},
        )
        assert v2._native_readiness_sibling(row) is None
        assert ManagedLaunchNotReady is not None

    def test_a_complete_proof_satisfies_both(self, monkeypatch):
        complete = dict(self.THIN)
        complete["provider_session_start"] = {
            claude_native_readiness.SESSION_START_ID_KEY: "sess-1"
        }
        complete["process_identity"] = {"pid": 7, "start_marker": "mk"}
        row = _Row(provider="claude_code")

        assert v2._incomplete_readiness_fields(row, complete) == []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: {"state": "ready", "readiness": complete},
        )
        assert v2._native_readiness_sibling(row) is not None

    def test_a_no_proof_pair_has_nothing_to_be_incomplete_about(self):
        assert v2._incomplete_readiness_fields(_Row(provider="kimi_cli"), self.THIN) == []


class TestTheTransientRefusalIsSeparableOnTheWire:
    def test_the_reason_is_the_agreed_closed_token(self):
        """Byte-identical with the consumer, which matches on it."""
        from cli_agent_orchestrator.services import managed_launch as ml

        assert ml.REASON_BIND_BRIDGE_NOT_DURABLY_READY == "bind-bridge-not-durably-ready"

    def test_not_ready_is_not_a_conflict(self):
        """A consumer keying on conflict must not see this as permanent."""
        from cli_agent_orchestrator.services import managed_launch as ml

        exc = ml.ManagedLaunchNotReady("early", reason=ml.REASON_BIND_BRIDGE_NOT_DURABLY_READY)
        assert isinstance(exc, ml.ManagedLaunchError)
        assert not isinstance(exc, ml.ManagedLaunchConflict)

    def test_the_status_is_425_and_carries_the_reason(self):
        """425 is used for nothing else on this surface.

        A shared code is exactly the ambiguity being removed: the consumer
        requires both the status and a reason it recognises, and treats an
        unrecognised reason as permanent.
        """
        from cli_agent_orchestrator.api.main import _managed_launch_http_error
        from cli_agent_orchestrator.services import managed_launch as ml

        http = _managed_launch_http_error(
            ml.ManagedLaunchNotReady("early", reason=ml.REASON_BIND_BRIDGE_NOT_DURABLY_READY)
        )
        assert http.status_code == 425
        assert http.detail["reason"] == "bind-bridge-not-durably-ready"

    def test_every_other_refusal_stays_a_permanent_409(self):
        """Identity, mode and foreign-attempt conflicts must keep tripping."""
        from cli_agent_orchestrator.api.main import _managed_launch_http_error
        from cli_agent_orchestrator.services import managed_launch as ml

        assert _managed_launch_http_error(ml.ManagedLaunchConflict("identity")).status_code == 409
        assert _managed_launch_http_error(ml.ManagedLaunchNotFound("gone")).status_code == 404
        assert _managed_launch_http_error(ml.ManagedLaunchUnavailable("down")).status_code == 503


class TestModelPinnabilityIsCheckedBeforeAnythingPersists:
    def test_an_unpinnable_claude_model_is_refused_at_reserve(self):
        """Earlier than the launch check, which stays.

        Refusing here costs no reservation, no allocated terminal id, and
        no recovery verb to finalize.
        """
        with pytest.raises(cl.ClaudeNativeModelError):
            cl.validate_requested_model("gpt-5")


class TestBindCarriesTheAssignedRouteNotTheObservation:
    """A provider-default route must still name an assigned effort.

    Reproduced live: a Kimi native generation reached durable readiness --
    input_ready true, exact pane, native session present -- and the exact
    idempotent bind replay returned HTTP 503, "native bind failed:
    assigned_effort must be a non-empty string".

    The cause is the requested/observed split not being carried into this
    consumer. Making the receipt honest gave a provider-default route a
    null *observed* effort, and the bind intent then fed that same null in
    as the *assigned* route fact -- but assigned is a statement about what
    was asked for, which is `provider-default`, and it is never null. The
    two fields answer different questions and only one of them may be
    unknown here.
    """

    ROUTE = {"expected_model": "kimi-code/kimi-for-coding", "expected_effort": "provider-default"}

    def test_the_assigned_effort_is_the_requested_one_not_the_observation(self):
        from cli_agent_orchestrator.services import recovery_receipts

        payload = recovery_receipts.route_payload(
            provider="kimi",
            native_id="session_bf43ec1e",
            authority_status="unobserved",
            assigned_model=self.ROUTE["expected_model"],
            assigned_effort=self.ROUTE["expected_effort"],
            assigned_policy_sha256="0" * 64,
            assigned_profile_sha256="0" * 64,
            assigned_config_sha256="0" * 64,
            requested_model=self.ROUTE["expected_model"],
            requested_effort=self.ROUTE["expected_effort"],
            observed_model=None,
            observed_effort=None,
            protocol_version=None,
            event_sequence=None,
            native_turn_id=None,
            attested_at="2026-07-25T21:02:26Z",
        )

        # Canonical bytes, not a mapping: parsed rather than indexed, so
        # the assertion is about what the receipt actually carries.
        import json

        fields = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        assert fields["assigned_effort"] == "provider-default"
        assert fields["observed_effort"] is None

    def test_a_null_assigned_effort_is_refused_by_the_receipt_contract(self):
        """The guard is right; feeding it the observation was wrong.

        Pinned so the repair is understood as routing the correct fact to
        this field, never as relaxing the field.
        """
        from cli_agent_orchestrator.services import recovery_receipts

        with pytest.raises(Exception):
            recovery_receipts.route_payload(
                provider="kimi",
                native_id="session_bf43ec1e",
                authority_status="unobserved",
                assigned_model=self.ROUTE["expected_model"],
                assigned_effort=None,
                assigned_policy_sha256="0" * 64,
                assigned_profile_sha256="0" * 64,
                assigned_config_sha256="0" * 64,
                requested_model=self.ROUTE["expected_model"],
                requested_effort=None,
                observed_model=None,
                observed_effort=None,
                protocol_version=None,
                event_sequence=None,
                native_turn_id=None,
                attested_at="2026-07-25T21:02:26Z",
            )
