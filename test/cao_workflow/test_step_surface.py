"""Unit tests for ``cao_workflow.step`` and the truthful ``replayed`` flag.

Covers this unit's BR-1..BR-10, SR-1..SR-4 and TD-2..TD-7 (issue #583, unit
``shim-step-surface``). Every test mocks the transport
(``cao_workflow._post``) — no real socket, no running server, mirroring
``test_run_step.py``.

Five of these tests exist because the NAIVE implementation passes a weaker
version of them:

1. ``replayed`` is asserted on ``run_step`` as well as ``step`` — four
   assertions, replayed and executed x both surfaces. Fewer passes with a
   constant, and populating it on ``step`` only is WRONG rather than
   incomplete: ``run_step`` already reaches the server's replay gate.
2. The surface-name tests assert BOTH directions, and the ``step`` direction
   asserts ``run_step`` is ABSENT — note ``"step()" in "run_step()"`` is True,
   so the positive assertion alone would pass for a hardcoded swap.
3. The SR-2 test asserts the two PRESENT env values are absent from the
   message, not merely that the missing one is named.
4. The closed-set test asserts ``ShimError`` AND that no HTTP was attempted —
   the raise alone would pass for a path that posted first.
5. BR-8 is asserted at the wire level as the ABSENCE of the ``recovery`` key,
   not as a falsy value, which would accept ``recovery: None``.
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
from pathlib import Path

import pytest

import cao_workflow
from cao_workflow._transport import URLError, _Response

_ENV = {
    "CAO_WORKFLOW_RUN_ID": "run-1",
    "CAO_WORKFLOW_GENERATION": "1",
    "CAO_API_BASE_URL": "http://localhost:9889",
}

_SHIM_INIT_SOURCE = Path(__file__).resolve().parents[2] / "src" / "cao_workflow" / "__init__.py"


@pytest.fixture(autouse=True)
def _reset_counter(monkeypatch):
    """Each test gets a fresh call-order counter (module-global, BR-3)."""
    import cao_workflow._counter as counter_mod

    monkeypatch.setattr(counter_mod, "_counter", 0)


@pytest.fixture
def full_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _response(
    *,
    replayed: bool,
    terminal_id: str = "term-1",
    last_message: str = "hi",
    status: str = "COMPLETED",
) -> _Response:
    """A 200 body shaped exactly like ``RunStepResponse`` — ``replayed`` always
    present, because the field is non-optional with a default so FastAPI always
    serialises it."""
    return _Response(
        status=200,
        body=json.dumps(
            {
                "terminal_id": terminal_id,
                "last_message": last_message,
                "status": status,
                "replayed": replayed,
            }
        ),
    )


def _capture(monkeypatch, response: _Response) -> "list[dict]":
    """Patch the transport and return the list the posted bodies land in."""
    bodies: "list[dict]" = []

    def fake_post(url, body, timeout=None):
        bodies.append(dict(body))
        return response

    monkeypatch.setattr(cao_workflow, "_post", fake_post)
    return bodies


class TestReplayedIsTruthfulOnBothSurfaces:
    """BR-2/SR-1 — all four assertions. ``replayed`` is the ONLY thing that
    stops a caller operating on the dead ``terminal_id`` a replayed response
    carries, so dropping it on either surface discards a shipped mitigation."""

    def test_step_reports_a_replayed_result_as_replayed(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=True))

        handle = cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        assert handle.replayed is True

    def test_step_reports_an_executed_result_as_not_replayed(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        handle = cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        assert handle.replayed is False

    def test_run_step_reports_a_replayed_result_as_replayed(self, full_env, monkeypatch):
        """The half that matters: ``run_step`` sends both env vars the replay
        gate keys on, and an UNDECLARED policy replays — so an existing
        ``run_step``-only script, resumed, gets replayed results from shipped
        code today."""
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=True))

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert handle.replayed is True

    def test_run_step_reports_an_executed_result_as_not_replayed(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert handle.replayed is False

    def test_a_replayed_handle_still_carries_the_original_terminal_id(self, full_env, monkeypatch):
        """The entity fact ``replayed`` exists to qualify: the id is
        syntactically valid and semantically DEAD."""
        monkeypatch.setattr(
            cao_workflow,
            "_post",
            lambda *a, **k: _response(replayed=True, terminal_id="term-from-the-first-run"),
        )

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert handle.terminal_id == "term-from-the-first-run"
        assert handle.replayed is True


class TestReplayedIsNeverDefaulted:
    """BR-3/TD-7 — read by direct indexing. A defaulting ``.get`` would
    silently re-manufacture the exact false ``replayed=False`` that BR-2 exists
    to prevent; a ``KeyError`` is the honest failure."""

    def _body_without_replayed(self) -> _Response:
        return _Response(
            status=200,
            body=json.dumps({"terminal_id": "term-1", "last_message": "hi", "status": "COMPLETED"}),
        )

    def test_run_step_raises_when_the_server_omits_replayed(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: self._body_without_replayed())

        with pytest.raises(KeyError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert "replayed" in str(exc_info.value)

    def test_step_raises_when_the_server_omits_replayed(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: self._body_without_replayed())

        with pytest.raises(KeyError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="manual")

        assert "replayed" in str(exc_info.value)

    def test_the_shim_source_reads_replayed_by_indexing_not_by_get(self):
        """An AST walk, not a substring scan: the rule is about the CODE, and a
        substring scan matches the comment that explains the rule (it did)."""
        tree = ast.parse(_SHIM_INIT_SOURCE.read_text(encoding="utf-8"))
        subscripts, defaulting_gets = [], []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "replayed"
            ):
                subscripts.append(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "replayed"
            ):
                defaulting_gets.append(node)

        assert subscripts, 'expected the replay flag to be read as data["replayed"]'
        assert not defaulting_gets, (
            "replayed must never be read with .get (BR-3/TD-7): a defaulting read "
            "re-manufactures the false replayed=False and hands the author a dead "
            f"terminal_id labelled live; found {len(defaulting_gets)}"
        )


class TestRecoveryIsRequiredByTheSignature:
    """BR-4 — the signature IS the enforcement. Absence never enters the body,
    and is deliberately NOT catchable as a ``ShimError``."""

    def test_omitting_recovery_is_a_type_error_naming_it(self, full_env, monkeypatch):
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(TypeError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi")

        assert "recovery" in str(exc_info.value)
        assert calls == []

    def test_the_type_error_is_not_a_shim_error(self):
        """An author must not be able to swallow a programming error with
        ``except ShimError``."""
        assert not issubclass(TypeError, cao_workflow.ShimError)

        with pytest.raises(TypeError):
            try:
                cao_workflow.step("kiro_cli", "reviewer", "hi")
            except cao_workflow.ShimError:  # pragma: no cover - must not fire
                pytest.fail("a missing recovery must not be catchable as ShimError")

    def test_recovery_is_keyword_only_with_no_default(self):
        param = inspect.signature(cao_workflow.step).parameters["recovery"]

        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty
        assert param.annotation == "str"


class TestRecoveryClosedSet:
    """BR-5/SR-4 — checked against the shim's OWN literals, before any HTTP.
    Both halves: the raise AND the absence of a request."""

    @pytest.mark.parametrize("value", ["idempotent", "reconcile", "manual"])
    def test_each_member_is_accepted_and_posted_verbatim(self, value, full_env, monkeypatch):
        bodies = _capture(monkeypatch, _response(replayed=False))

        cao_workflow.step("kiro_cli", "reviewer", "hi", recovery=value)

        assert bodies[0]["recovery"] == value

    @pytest.mark.parametrize(
        "value",
        [
            "Idempotent",  # case-folding would accept this
            "IDEMPOTENT",
            " idempotent",  # stripping would accept this
            "idempotent ",
            "idem",  # aliasing/prefix matching would accept this
            "retry",  # a plausible-but-wrong policy name
            "",
        ],
    )
    def test_a_value_outside_the_set_raises_and_never_reaches_the_network(
        self, value, full_env, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(cao_workflow.ShimError):
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery=value)

        assert calls == []

    def test_the_rejection_message_echoes_the_received_value(self, full_env, monkeypatch):
        """Deliberate (SR-4): the server's own ``RecoveryPolicy(value)`` echoes
        it, so withholding would make the client-side check less informative
        than its server-side twin. A policy name is not a secret."""
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="Idempotent")

        message = str(exc_info.value)
        assert "Idempotent" in message
        for member in ("idempotent", "reconcile", "manual"):
            assert member in message

    def test_the_check_runs_before_identity_resolution(self, monkeypatch):
        """A typo in the author's own source is diagnosed as a typo, not as a
        missing environment, so the message does not depend on where the script
        happens to be running."""
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="nope")

        assert not isinstance(exc_info.value, cao_workflow.ShimIdentityError)
        assert "nope" in str(exc_info.value)


class TestClosedSetIsPinnedToRecoveryPolicy:
    """BR-6/TD-3 — the drift guard. C-2 binds ``src/cao_workflow/``, not
    ``test/``: the enforcing AST test walks ``src/cao_workflow`` only, so this
    module may import both packages. A fourth policy member becomes a test
    failure here rather than a runtime 422 much later."""

    def test_shim_literals_equal_the_recovery_policy_members(self):
        from cli_agent_orchestrator.models.workflow import RecoveryPolicy

        assert cao_workflow._RECOVERY_POLICIES == {member.value for member in RecoveryPolicy}

    def test_the_closed_set_is_immutable(self):
        assert isinstance(cao_workflow._RECOVERY_POLICIES, frozenset)


class TestNoMessageNamesTheWrongFunction:
    """BR-7/INV-6 — both directions, always. ``"step()" in "run_step()"`` is
    True, so each ``step`` case also asserts ``run_step`` is ABSENT; without
    that, a hardcoded swap would pass."""

    def test_step_identity_failure_names_step(self, monkeypatch):
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        message = str(exc_info.value)
        assert "step() must be called" in message
        assert "run_step" not in message

    def test_run_step_identity_failure_names_run_step(self, monkeypatch):
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert "run_step() must be called" in str(exc_info.value)

    def test_step_reuse_terminal_id_rejection_names_step(self, full_env, monkeypatch):
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.step(
                "kiro_cli", "reviewer", "hi", recovery="manual", reuse_terminal_id="term-1"
            )

        message = str(exc_info.value)
        assert "not supported by step()" in message
        assert "run_step" not in message
        assert calls == []

    def test_run_step_reuse_terminal_id_rejection_names_run_step(self, full_env, monkeypatch):
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi", reuse_terminal_id="term-1")

        assert "not supported by run_step()" in str(exc_info.value)
        assert calls == []


class TestIdentityMessageStillLeaksNothing:
    """SR-2 — the refactor may change the function name in that sentence and
    NOTHING else. The three variables include ``CAO_API_BASE_URL``, which is an
    internal hostname in a real deployment, so the negative half is the half
    that matters: asserting the missing name alone would pass while leaking."""

    _PRESENT = {
        "CAO_WORKFLOW_RUN_ID": "run-id-must-not-leak",
        "CAO_WORKFLOW_GENERATION": "generation-must-not-leak",
    }

    def _set_two_of_three(self, monkeypatch):
        for name, value in self._PRESENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("CAO_API_BASE_URL", raising=False)

    def test_step_message_names_only_the_missing_var(self, monkeypatch):
        self._set_two_of_three(monkeypatch)

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        message = str(exc_info.value)
        assert "CAO_API_BASE_URL" in message
        for name, value in self._PRESENT.items():
            assert value not in message
            assert name not in message

    def test_run_step_message_names_only_the_missing_var(self, monkeypatch):
        self._set_two_of_three(monkeypatch)

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        message = str(exc_info.value)
        assert "CAO_API_BASE_URL" in message
        for name, value in self._PRESENT.items():
            assert value not in message
            assert name not in message


class TestRecoveryOnTheWire:
    """BR-8 — sent ONLY when declared. ``run_step`` omits the KEY, rather than
    sending an explicit null that would misrepresent it as declaring absence."""

    def test_run_step_posts_no_recovery_key_at_all(self, full_env, monkeypatch):
        bodies = _capture(monkeypatch, _response(replayed=False))

        cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert "recovery" not in bodies[0]

    def test_step_posts_the_declared_policy_as_a_body_field(self, full_env, monkeypatch):
        bodies = _capture(monkeypatch, _response(replayed=False))

        cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="reconcile")

        assert bodies[0]["recovery"] == "reconcile"


class TestTheTwoSurfacesAreOtherwiseIdentical:
    """BR-9/INV-1 — ``step_id``, ``timeout``, ``**opts``, the counter and the
    error taxonomy are surface-independent."""

    def test_the_posted_bodies_differ_by_exactly_the_recovery_key(self, full_env, monkeypatch):
        bodies = _capture(monkeypatch, _response(replayed=False))
        kwargs = dict(step_id="shard-7", timeout=12.5, session_name="sess", teardown=False)

        cao_workflow.run_step("kiro_cli", "reviewer", "review this", **kwargs)
        cao_workflow.step("kiro_cli", "reviewer", "review this", recovery="idempotent", **kwargs)

        run_step_body, step_body = bodies
        assert step_body == {**run_step_body, "recovery": "idempotent"}
        assert run_step_body["timeout"] == 12.5
        assert run_step_body["session_name"] == "sess"
        assert run_step_body["teardown"] is False
        assert run_step_body["env_vars"]["CAO_WORKFLOW_STEP_ID"] == "shard-7"

    def test_an_opts_key_named_surface_still_reaches_the_body(self, full_env, monkeypatch):
        """INV-1 regression. ``_execute_step``'s leading parameters are
        POSITIONAL-ONLY, so the private core's own parameter names can never
        collide with an author's ``**opts`` key.

        Without the ``/`` this raised ``TypeError: _execute_step() got multiple
        values for keyword argument 'surface'`` — leaking a private function's
        name and breaking a key that was a harmless pass-through body field
        before this unit existed. The name of the core's parameter must not be
        able to steal a name out of the author's namespace.
        """
        bodies = _capture(monkeypatch, _response(replayed=False))

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "x", **{"surface": "evil"})

        assert bodies[0]["surface"] == "evil"
        assert handle.step_id == "call-1"

    def test_an_opts_key_named_surface_does_not_hijack_the_message_surface(
        self, full_env, monkeypatch
    ):
        """The other half: an opts key reaching the BODY must not also reach the
        diagnostics. ``run_step`` still names itself, whatever the author posts."""
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.run_step(
                "kiro_cli", "reviewer", "x", reuse_terminal_id="t-1", **{"surface": "evil"}
            )

        message = str(exc_info.value)
        assert "not supported by run_step()" in message
        assert "evil" not in message

    def test_step_uses_the_shared_call_n_counter(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        first = cao_workflow.step("kiro_cli", "reviewer", "one", recovery="idempotent")
        second = cao_workflow.run_step("kiro_cli", "reviewer", "two")

        assert (first.step_id, second.step_id) == ("call-1", "call-2")

    def test_step_explicit_step_id_is_used_verbatim(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))

        handle = cao_workflow.step("kiro_cli", "reviewer", "x", recovery="manual", step_id="s-1")

        assert handle.step_id == "s-1"

    def test_step_concurrent_calls_get_distinct_keys(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _response(replayed=False))
        n = 25
        results: "list[str]" = []
        lock = threading.Lock()

        def worker():
            handle = cao_workflow.step("kiro_cli", "reviewer", "x", recovery="idempotent")
            with lock:
                results.append(handle.step_id)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert set(results) == {f"call-{i}" for i in range(1, n + 1)}

    def test_step_wraps_a_transport_failure_without_retrying(self, full_env, monkeypatch):
        attempts = []

        def fake_post(*args, **kwargs):
            attempts.append(1)
            raise URLError("connection refused")

        monkeypatch.setattr(cao_workflow, "_post", fake_post)

        with pytest.raises(cao_workflow.ShimTransportError):
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        assert len(attempts) == 1

    def test_step_surfaces_a_non_200_as_shim_http_error(self, full_env, monkeypatch):
        monkeypatch.setattr(
            cao_workflow,
            "_post",
            lambda *a, **k: _Response(status=409, body='{"detail":"DIVERGED"}'),
        )

        with pytest.raises(cao_workflow.ShimHTTPError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="idempotent")

        assert exc_info.value.status == 409
        assert exc_info.value.body == '{"detail":"DIVERGED"}'


class TestNoSecondPassOverTheServersPayload:
    """SR-3 — the envelope was redacted THEN bounded before storage, so a
    second pass in the shim could match its own ``[REDACTED:<name>]`` marker
    and corrupt the first. The shim transforms nothing."""

    def test_last_message_reaches_the_author_byte_identical(self, full_env, monkeypatch):
        payload = "token=[REDACTED:api_key] then a \\n literal and a real\nnewline … [truncated]"
        monkeypatch.setattr(
            cao_workflow,
            "_post",
            lambda *a, **k: _response(replayed=True, last_message=payload),
        )

        handle = cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="reconcile")

        assert handle.output == payload

    def test_a_non_200_body_reaches_the_author_verbatim(self, full_env, monkeypatch):
        body = '{"detail":"replay diverged for step \'call-1\' [REDACTED:secret]"}'
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _Response(status=409, body=body))

        with pytest.raises(cao_workflow.ShimHTTPError) as exc_info:
            cao_workflow.step("kiro_cli", "reviewer", "hi", recovery="manual")

        assert exc_info.value.body == body


class TestStepHandleShape:
    """BR-10 — frozen, and ``replayed`` defaults to ``False`` so an author's own
    hand-constructed handle keeps working."""

    def test_constructs_without_replayed_and_defaults_to_false(self):
        handle = cao_workflow.StepHandle(
            step_id="call-1", terminal_id="term-1", output="hi", status="COMPLETED"
        )

        assert handle.replayed is False

    def test_stays_frozen(self):
        handle = cao_workflow.StepHandle(
            step_id="call-1", terminal_id="term-1", output="hi", status="COMPLETED"
        )

        with pytest.raises(Exception) as exc_info:
            handle.replayed = True  # type: ignore[misc]

        assert exc_info.type.__name__ == "FrozenInstanceError"


class TestPublicSurface:
    """TD-5 and BR-1 — ``step`` joins the declared surface, the private core
    does not, and ``run_step``'s parameter list is unchanged."""

    def test_step_is_exported_and_the_private_core_is_not(self):
        assert "step" in cao_workflow.__all__
        assert "_execute_step" not in cao_workflow.__all__

    def test_the_private_cores_leading_parameters_are_positional_only(self):
        """The structural half of the ``**opts`` collision fix: any parameter of
        the core that is reachable BY KEYWORD is a name an author can no longer
        use as a body field, because both surfaces forward ``**opts`` here."""
        params = list(inspect.signature(cao_workflow._execute_step).parameters.values())
        leading = [p for p in params if p.kind is inspect.Parameter.POSITIONAL_ONLY]

        assert [p.name for p in leading] == ["surface", "provider", "agent", "prompt"]
        assert "surface" not in {p.name for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY}

    def test_the_module_docstring_names_the_new_surface(self):
        assert "``step``" in (cao_workflow.__doc__ or "")

    def test_run_step_signature_is_unchanged(self):
        """BR-1/ADR-583-7: a bare ``recovery=`` here would be optional by
        construction, which would make the declare-a-policy lint rule
        undecidable. This pins the whole parameter list, not just its absence."""
        params = inspect.signature(cao_workflow.run_step).parameters

        assert "recovery" not in params
        assert [(p.name, p.kind, p.default) for p in params.values()] == [
            ("provider", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("agent", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("prompt", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("step_id", inspect.Parameter.KEYWORD_ONLY, None),
            ("timeout", inspect.Parameter.KEYWORD_ONLY, None),
            ("opts", inspect.Parameter.VAR_KEYWORD, inspect.Parameter.empty),
        ]

    def test_step_signature_mirrors_run_step_plus_required_recovery(self):
        params = inspect.signature(cao_workflow.step).parameters

        assert [(p.name, p.kind, p.default) for p in params.values()] == [
            ("provider", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("agent", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("prompt", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
            ("recovery", inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.empty),
            ("step_id", inspect.Parameter.KEYWORD_ONLY, None),
            ("timeout", inspect.Parameter.KEYWORD_ONLY, None),
            ("opts", inspect.Parameter.VAR_KEYWORD, inspect.Parameter.empty),
        ]
