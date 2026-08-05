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

import base64
import hashlib
import json
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import kimi_native_bootstrap, kimi_route
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

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
        route_env = bridge._provider_route_environment(self._request(SENTINEL))
        # No effort variable: the sentinel selects none, so none is sent.
        assert "KIMI_MODEL_THINKING_EFFORT" not in route_env
        # The updater kill-switch is not an effort control — it fences
        # every managed Kimi child process against self-update mid-run
        # (cond-0315), the sentinel route included.
        assert route_env == {"KIMI_CODE_NO_AUTO_UPDATE": "1"}

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


# --------------------------------------------------------------------
# The consumer of the honest receipt: bind
# --------------------------------------------------------------------


@pytest.fixture
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _reserve(worktree, tmp_path, *, model, effort) -> dict:
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    record, _created = v2.reserve(
        ManagedLaunchV2ReserveRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=str(uuid.uuid4()),
            session_name="cao-test",
            provider="kimi_cli",
            agent_profile="reviewer",
            caller_id="deadbeef",
            working_directory=str(worktree),
            expected_model=model,
            expected_effort=effort,
            provider_executable=str(executable),
            provider_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            obligation_generation="obgen-7c2e4a1b",
            task_id="self-heal-demo-task",
            run_id="run-0001",
            delivery_id=str(uuid.uuid4()),
            launch_nonce="n" * 40,
            execution_mode="native_tui",
            worker_class="persistent",
        )
    )
    return record


def _native_receipt(record, *, model, effort) -> dict:
    """The receipt shape ``_native_readiness_receipt`` actually publishes.

    Built from that function's own key list rather than a convenient
    subset, so a test that passes here is asserting about the object bind
    really receives.
    """
    session_id = str(uuid.uuid4())
    return {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": model,
        "effort": effort,
        "working_directory": record["working_directory"],
        "receipt_id": session_id,
        "provider_session_id": session_id,
        "provider_version": "0.29.1",
        "provider_receipt_kind": "kimi-native-tui-attached",
        "model_input_ready": True,
    }


def _validate(reservation_id: str, receipt: dict) -> None:
    with database.SessionLocal() as db:
        row = v2._query(db, reservation_id)
        assert row is not None
        v2._validate_readiness_for_bind(row, receipt)


class TestBindAcceptsTheHonestNoEffortReceipt:
    """Where cond-0109 reappeared after being fixed everywhere else.

    The native mint reports ``effort: None`` for a provider-default route
    — deliberately, because a read-back value there is an artifact of the
    session rather than a route fact. Bind then compared that ``None`` to
    the literal ``"provider-default"`` by string equality and refused
    every such launch, *after* the pane existed and the reservation was
    open. The symptom moved from attestation to bind instead of being
    removed: fixed on one path, sibling left behind.

    The bridged path never showed it, because the bridge echoes the
    request rather than reading the session back, so its receipt carries
    the sentinel and matched. Only the native receipt is honest enough to
    fail.
    """

    def test_a_null_effort_binds_for_a_provider_default_route(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        _validate(record["reservation_id"], _native_receipt(record, model=K27, effort=None))

    def test_the_route_is_still_exact_on_everything_else(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """Declining to pin the effort does not loosen the model check."""
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        with pytest.raises(ManagedLaunchConflict, match="exact v2 reservation"):
            _validate(
                record["reservation_id"],
                _native_receipt(record, model="kimi-code/something-else", effort=None),
            )

    def test_a_concrete_effort_for_a_provider_default_route_is_refused(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """The check is not relaxed, it is translated.

        A receipt naming an effort here means the session settled on one
        nobody selected — the drift the exact-route check exists to catch.
        Accepting it would certify a route this side never chose, which is
        worse than the refusal it replaces.
        """
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        with pytest.raises(ManagedLaunchConflict) as raised:
            _validate(record["reservation_id"], _native_receipt(record, model=K27, effort="high"))
        assert "effort" in str(raised.value)

    def test_the_sentinel_itself_is_not_accepted_as_an_observation(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """Echoing the sentinel back is not evidence of anything.

        Accepting it would make the receipt's effort field unfalsifiable
        for exactly the routes where nothing was measured.
        """
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        with pytest.raises(ManagedLaunchConflict):
            _validate(record["reservation_id"], _native_receipt(record, model=K27, effort=SENTINEL))


class TestBindStillEnforcesASelectedEffort:
    """K3 behavior at the bind seam, unchanged in both directions."""

    def test_a_matching_effort_binds(self, isolated_memory_db, _companion, worktree, tmp_path):
        record = _reserve(worktree, tmp_path, model=K3, effort="max")

        _validate(record["reservation_id"], _native_receipt(record, model=K3, effort="max"))

    def test_a_different_effort_is_refused(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path, model=K3, effort="max")

        with pytest.raises(ManagedLaunchConflict, match="effort"):
            _validate(record["reservation_id"], _native_receipt(record, model=K3, effort="low"))

    def test_a_null_effort_is_refused_for_a_selecting_route(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """The mirror of the fix, and the reason it is a helper not a skip.

        A selecting route whose receipt reports nothing has not been
        proven; treating null as universally acceptable would have fixed
        the K2.7 refusal by making every route's effort unverifiable.
        """
        record = _reserve(worktree, tmp_path, model=K3, effort="max")

        with pytest.raises(ManagedLaunchConflict, match="effort"):
            _validate(record["reservation_id"], _native_receipt(record, model=K3, effort=None))


class TestTheHelperIsTheOneComparison:
    def test_it_answers_both_alphabets(self):
        assert provider_contracts.effort_receipt_matches("max", "max") is True
        assert provider_contracts.effort_receipt_matches("max", "low") is False
        assert provider_contracts.effort_receipt_matches("max", None) is False
        assert provider_contracts.effort_receipt_matches(SENTINEL, None) is True
        assert provider_contracts.effort_receipt_matches(SENTINEL, "high") is False
        assert provider_contracts.effort_receipt_matches(SENTINEL, SENTINEL) is False


class TestBindSurvivesAProviderDefaultRoute:
    """The live cond-0112 Kimi failure, reproduced at the bind seam.

    A Kimi native generation reached durable readiness -- input_ready
    true, exact pane, native session present -- and its very first bind
    returned HTTP 503, "native bind failed: assigned_effort must be a
    non-empty string". Nothing was wrong with the readiness. The bind
    intent fed the receipt's *observed* effort, which a truthful
    provider-default receipt reports as null, into the *assigned* route
    fact, which is a statement about what was asked for and is never
    null.

    Every earlier test of this route stopped at the receipt or at
    readiness validation, so the one consumer that actually rejected the
    null was never exercised with it.
    """

    def _ready_state(self, record):
        session_id = "session_bf43ec1e-793f-4d5e-80dd-39a03e6d3d82"
        return {
            "state": "ready",
            "readiness": {
                "bridge_version": bridge.BRIDGE_VERSION,
                "reservation_id": record["reservation_id"],
                "terminal_id": record["terminal_id"],
                "generation": record["generation"],
                "provider": "kimi_cli",
                "agent_profile": record["agent_profile"],
                "model": K27,
                # Honest: this route selected no effort, so none was read.
                "effort": None,
                "working_directory": record["working_directory"],
                "receipt_id": session_id,
                "provider_session_id": session_id,
                "provider_version": "0.29.1",
                "provider_receipt_kind": "kimi-native-tui-attached",
                "model_input_ready": True,
                "model_input_ready_observation": {
                    "authority": "observe_kimi_turn_state",
                    "observed_at": "2026-07-25T21:02:26Z",
                    "pane_id": "%30",
                    "provider_status": "idle",
                    "input_ready": True,
                    "detail": None,
                },
                "process_identity": {"pid": 14744, "start_marker": "Sat Jul 25 17:02:24 2026"},
                "provider_session_start": None,
            },
        }

    def test_a_provider_default_route_binds(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        import uuid as _uuid

        from cli_agent_orchestrator.models.managed_launch_v2 import ManagedLaunchV2BindRequest

        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)
        v2.claim_launch(record["reservation_id"])
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
            lambda _rid: self._ready_state(record),
            raising=False,
        )
        monkeypatch.setattr(v2, "_assert_session_not_foreign_held", lambda *a, **k: None)

        bound = v2.bind_native(
            record["reservation_id"],
            ManagedLaunchV2BindRequest(
                protocol_version=PROTOCOL_VERSION_V2,
                terminal_id=record["terminal_id"],
                generation=record["generation"],
                attempt_id=str(_uuid.uuid4()),
            ),
        )

        assert bound["state"] == "bound"
        assert bound["binding"] is not None


class TestTheRouteDigestIsOverTheAssignedRoute:
    """The digest binds a failure domain, so both peers must compute it alike.

    ``assigned_route_digest`` rides in the binding payload and is compared
    against a value the conductor derives from the route it reserved. It
    was hashed from the receipt -- the *observation* -- which is a
    different fact and, for the two classes that cannot be observed,
    literally a different value: a provider-default Kimi route observes
    null effort, and a Claude route observes a resolved model rather than
    the alias that was requested.

    So the digest changed with the provider rather than with the route,
    and two peers comparing it would disagree for a reason neither could
    see from its own side. Nothing asserted this value anywhere, which is
    how it stayed wrong through the receipt fix.
    """

    def _digest_for(self, record, receipt_model, receipt_effort):
        request = ManagedLaunchV2BindRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=record["reservation_id"],
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            caller_id="deadbeef",
            attempt_id=str(uuid.uuid4()),
        )
        receipt = _native_receipt(record, model=receipt_model, effort=receipt_effort)
        with database.SessionLocal() as db:
            row = v2._query(db, record["reservation_id"])
            intent = v2._build_bind_intent(
                db, row, record["reservation_id"], request, receipt, "native_tui"
            )
        return intent

    def _route_digest(self, intent):
        # The exact canonical bytes the peer receives, decoded rather than
        # read from a convenient in-memory field, so this asserts about
        # what actually crosses the wire.
        payload = base64.b64decode(intent["binding_payload_b64"])
        return json.loads(payload.decode())["assigned_route_digest"]

    def test_the_observation_does_not_move_the_digest(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """Two truthful receipts, one route: one domain key.

        The observed effort is null for this route and the observed model
        is whatever the session resolved. Neither is the route, so neither
        may change the value the two peers compare.
        """
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        from_null = self._route_digest(self._digest_for(record, K27, None))
        from_echo = self._route_digest(self._digest_for(record, K27, SENTINEL))

        assert from_null == from_echo

    def test_the_digest_is_the_hash_of_the_reserved_route(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """Recomputed independently from the reservation, not from the code.

        A peer holding only the reservation must arrive at this exact
        value, so the test derives it the way that peer would rather than
        calling the same helper and comparing it to itself.
        """
        record = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)

        expected = hashlib.sha256(
            v2._canonical_json(
                {
                    "model": K27,
                    "effort": SENTINEL,
                    "agent_profile": record["agent_profile"],
                }
            ).encode()
        ).hexdigest()

        assert self._route_digest(self._digest_for(record, K27, None)) == expected

    def test_a_different_route_is_a_different_domain(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """The digest still discriminates; it is pinned, not neutralized."""
        k27 = _reserve(worktree, tmp_path, model=K27, effort=SENTINEL)
        k3 = _reserve(worktree, tmp_path, model=K3, effort="max")

        assert self._route_digest(self._digest_for(k27, K27, None)) != self._route_digest(
            self._digest_for(k3, K3, "max")
        )
